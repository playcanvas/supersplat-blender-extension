"""Decode SuperSplat scenes (SOG and SSOG), and stream the ZIPs SuperSplat prepares for download.

SOG is a `meta.json` (version 2) plus lossless WebP textures, one texel per
splat in row-major order:

    means_l, means_u  16-bit position per axis (low byte, high byte), lerped
                      between means.mins/maxs, then n -> sign(n) * (exp|n| - 1)
    quats             smallest-three quaternion: RGB are the other components
                      scaled by sqrt(2); A is 252 + the index (w, x, y, z) of the
                      largest component, which is rebuilt as positive
    scales            RGB index scales.codebook (log scale)
    sh0               RGB index sh0.codebook (f_dc); A is sigmoid(opacity) * 255
    shN_labels        RG is a 16-bit index into the SH palette
    shN_centroids     the palette: 64 entries per row, each `coefficients`
                      texels wide; RGB of texel k index shN.codebook

SSOG is a `lod-meta.json` whose `tree` maps each level of detail to SOG files
(`filenames`). Level 0 is the finest and every level is a complete scene; a
level is the concatenation of its files in `filenames` order. `environment`,
when present, names a separate SOG for the far background.

Output columns use Blender's Gaussian splat attribute layout (see ply.py), so a
decoded scene matches Blender's import of the same scene converted to PLY by
splat-transform. Legacy (version 1) SOG files are not supported.
"""

from __future__ import annotations

import gzip
import http.client
import json
import math
import zlib
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Sequence

import numpy as np

from . import USER_AGENT
from .api import _RejectRedirects, is_secure_url
from .errors import DecodeError, DownloadHttpError, ResponseTooLarge, UploadCancelled
from .ply import SplatData, coefficients_for_degree

META = "meta.json"
LOD_META = "lod-meta.json"

# The largest published texture seen is 36 MB (a 10M-splat SOG).
MAX_FILE_BYTES = 256 * 1024**2
_READ_CHUNK = 1024 * 1024

# Reads one texture as uint8 RGBA pixels of shape (height, width, 4).
ImageReader = Callable[[Path], np.ndarray]

_QUAT_OTHERS = np.array([[1, 2, 3], [0, 2, 3], [0, 1, 3], [0, 1, 2]])
_PALETTE_ROW = 64


@dataclass
class Scene:
    splats: SplatData
    environment: SplatData | None = None
    level: int = 0
    # Splats per level of detail, finest first; one entry for a plain SOG.
    level_counts: list[int] = field(default_factory=list)


# -- WebP ---------------------------------------------------------------------------


def read_webp(path: Path) -> np.ndarray:
    """Decode a WebP texture to straight-alpha uint8 RGBA with OpenImageIO.

    OpenImageIO premultiplies alpha by default, which corrupts every texel
    whose alpha is below 255 (opacity in sh0, the quaternion tag in quats).
    """
    import OpenImageIO as oiio

    config = oiio.ImageSpec()
    config.attribute("oiio:UnassociatedAlpha", 1)
    image = oiio.ImageInput.open(str(path), config)
    if image is None:
        raise DecodeError(f"Could not open {path.name}: {oiio.geterror()}")
    try:
        spec = image.spec()
        pixels = image.read_image(0, 0, 0, spec.nchannels, "uint8")
        if pixels is None:
            raise DecodeError(f"Could not decode {path.name}: {image.geterror()}")
    finally:
        image.close()
    pixels = np.asarray(pixels, dtype=np.uint8).reshape(spec.height, spec.width, spec.nchannels)
    if spec.nchannels == 4:
        return pixels
    rgba = np.full((spec.height, spec.width, 4), 255, dtype=np.uint8)
    rgba[..., : min(spec.nchannels, 3)] = pixels[..., :3]
    return rgba


# -- Manifests ----------------------------------------------------------------------


def _json(data: bytes, name: str) -> dict[str, Any]:
    # Published manifests are stored gzip-compressed.
    try:
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        value = json.loads(data)
    except (UnicodeDecodeError, ValueError, EOFError, OSError, zlib.error) as error:
        raise DecodeError(f"{name} is not valid JSON: {error}") from error
    if not isinstance(value, dict):
        raise DecodeError(f"{name} is not a JSON object.")
    return value


def _relative(name: Any, manifest: str) -> PurePosixPath:
    """A manifest file reference, refused if it could escape the scene folder."""
    if not isinstance(name, str) or not name:
        raise DecodeError(f"{manifest} lists an invalid file name: {name!r}")
    path = PurePosixPath(name)
    # `%` too, so no name becomes `..` once percent-decoded.
    if path.is_absolute() or any(part in ("", ".", "..") for part in name.split("/")) or any(
        char in name for char in "\\:?#%"
    ):
        raise DecodeError(f"{manifest} lists an unsafe file name: {name!r}")
    return path


def _check_sog(meta: dict[str, Any], name: str) -> None:
    version = meta.get("version")
    if version is None:
        raise DecodeError(f"{name} is a legacy (version 1) SOG, which is not supported.")
    if version != 2:
        raise DecodeError(f"{name} has unsupported SOG version {version!r}.")
    count = meta.get("count")
    if not isinstance(count, int) or count < 0:
        raise DecodeError(f"{name} has an invalid splat count: {count!r}")


def _sog_files(meta: dict[str, Any], name: str) -> list[PurePosixPath]:
    sections = ["means", "quats", "scales", "sh0"] + (["shN"] if meta.get("shN") else [])
    files = []
    for section in sections:
        entries = (meta.get(section) or {}).get("files")
        expected = 2 if section in ("means", "shN") else 1
        if not isinstance(entries, list) or len(entries) < expected:
            raise DecodeError(f"{name} is missing the {section} textures.")
        files += [_relative(entry, name) for entry in entries[:expected]]
    return files


def _check_lod(meta: dict[str, Any], name: str) -> None:
    version = meta.get("version")
    if version not in (None, 1):
        raise DecodeError(f"{name} has unsupported version {version!r}.")
    levels = meta.get("lodLevels")
    if not isinstance(levels, int) or levels <= 0:
        raise DecodeError(f"{name} has an invalid lodLevels: {levels!r}")
    if not isinstance(meta.get("filenames"), list) or not isinstance(meta.get("tree"), dict):
        raise DecodeError(f"{name} is missing filenames or tree.")


def _level_files(meta: dict[str, Any], level: int, name: str) -> list[tuple[PurePosixPath, int]]:
    """(SOG meta path, splat count) for each file of `level`, in `filenames` order."""
    levels = meta["lodLevels"]
    if not 0 <= level < levels:
        raise DecodeError(f"Level {level} is out of range; the scene has levels 0-{levels - 1}.")
    filenames = meta["filenames"]
    counts: dict[int, int] = {}
    stack = [meta["tree"]]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            raise DecodeError(f"{name} has an invalid tree.")
        ref = (node.get("lods") or {}).get(str(level))
        if ref is not None:
            file, count = ref.get("file"), ref.get("count")
            if not isinstance(file, int) or not 0 <= file < len(filenames) or not isinstance(count, int):
                raise DecodeError(f"{name} has an invalid reference for level {level}.")
            counts[file] = counts.get(file, 0) + count
        stack.extend(node.get("children") or [])
    total = sum(counts.values())
    expected = (meta.get("counts") or [None] * levels)[level]
    if expected is not None and expected != total:
        raise DecodeError(f"{name} level {level} lists {expected} splats but its tree has {total}.")
    if not counts:
        raise DecodeError(f"{name} has no files for level {level}.")
    return [(_relative(filenames[file], name), counts[file]) for file in sorted(counts)]


def _environment(meta: dict[str, Any], name: str) -> PurePosixPath | None:
    environment = meta.get("environment")
    return None if environment is None else _relative(environment, name)


def _manifest(path: Path) -> Path:
    """The meta.json or lod-meta.json of a scene folder (or the file itself)."""
    if path.is_dir():
        for candidate in (LOD_META, META):
            if (path / candidate).is_file():
                return path / candidate
        raise DecodeError(f"{path} contains neither {LOD_META} nor {META}.")
    if not path.is_file():
        raise DecodeError(f"{path} does not exist.")
    return path


# -- Decoding -----------------------------------------------------------------------


class _Columns:
    """Preallocated output columns, filled one SOG at a time."""

    def __init__(self, count: int, sh_coefficients: int) -> None:
        self.count = count
        self.position = np.empty((count, 3), dtype=np.float32)
        self.base = np.empty((count, 4), dtype=np.float32)
        self.scale = np.empty((count, 3), dtype=np.float32)
        self.rotation = np.empty((count, 4), dtype=np.float32)
        self.sh = [np.empty((count, 3), dtype=np.float32) for _ in range(sh_coefficients)]

    def splat_data(self) -> SplatData:
        return SplatData(
            count=self.count,
            position=self.position.reshape(-1),
            base=self.base.reshape(-1),
            scale=self.scale.reshape(-1),
            rotation=self.rotation.reshape(-1),
            sh=[column.reshape(-1) for column in self.sh],
        )


def _sh_coefficients(meta: dict[str, Any]) -> int:
    shn = meta.get("shN")
    if not shn:
        return 0
    bands = shn.get("bands")
    if bands not in (1, 2, 3):
        raise DecodeError(f"Unsupported spherical-harmonics band count {bands!r}.")
    return coefficients_for_degree(bands)


def _texels(read_image: ImageReader, path: Path, count: int) -> np.ndarray:
    image = read_image(path)
    texels = image.reshape(-1, 4)
    if len(texels) < count:
        raise DecodeError(f"{path.name} has {len(texels)} texels; expected at least {count}.")
    return texels[:count]


def _codebook(meta: dict[str, Any], section: str) -> np.ndarray:
    codebook = np.asarray((meta.get(section) or {}).get("codebook", []), dtype=np.float32)
    if codebook.shape != (256,):
        raise DecodeError(f"The {section} codebook must have 256 entries.")
    return codebook


def _decode_into(
    columns: _Columns, start: int, meta_path: Path, meta: dict[str, Any], read_image: ImageReader
) -> None:
    name = meta_path.name
    count = meta["count"]
    rows = slice(start, start + count)
    files = [meta_path.parent / Path(*relative.parts) for relative in _sog_files(meta, name)]
    texture = iter(files)

    def texels() -> np.ndarray:
        return _texels(read_image, next(texture), count)

    # Positions: float64 like splat-transform, so the float32 results agree exactly.
    low, high = texels()[:, :3].astype(np.uint32), texels()[:, :3].astype(np.uint32)
    mins = np.asarray(meta["means"]["mins"], dtype=np.float64)
    span = np.asarray(meta["means"]["maxs"], dtype=np.float64) - mins
    span[span == 0] = 1.0
    n = mins + span * ((low | (high << 8)) / 65535.0)
    columns.position[rows] = np.sign(n) * (np.exp(np.abs(n)) - 1.0)

    quats = texels()
    largest = quats[:, 3].astype(np.int64) - 252
    valid = (largest >= 0) & (largest <= 3)
    largest[~valid] = 0
    others = (quats[:, :3].astype(np.float32) * np.float32(2 / 255) - 1) * np.float32(math.sqrt(0.5))
    rotation = np.zeros((count, 4), dtype=np.float32)
    index = np.arange(count)[:, None]
    rotation[index, _QUAT_OTHERS[largest]] = others
    rotation[index[:, 0], largest] = np.sqrt(np.maximum(0.0, 1.0 - np.sum(others * others, axis=1)))
    rotation[~valid] = (1.0, 0.0, 0.0, 0.0)
    rotation /= np.linalg.norm(rotation, axis=1, keepdims=True)
    columns.rotation[rows] = rotation

    columns.scale[rows] = np.exp(_codebook(meta, "scales"))[texels()[:, :3]]

    sh0 = texels()
    columns.base[rows, :3] = _codebook(meta, "sh0")[sh0[:, :3]]
    columns.base[rows, 3] = sh0[:, 3] / np.float32(255)

    coefficients = len(columns.sh)
    if not coefficients:
        return
    centroids = read_image(next(texture))
    labels = _texels(read_image, next(texture), count)
    palette_size = meta["shN"].get("count")
    height, width = centroids.shape[:2]
    if width != _PALETTE_ROW * coefficients:
        raise DecodeError(
            f"{name}: the SH palette is {width} texels wide; expected {_PALETTE_ROW * coefficients}."
        )
    if not isinstance(palette_size, int) or not 0 < palette_size <= height * _PALETTE_ROW:
        raise DecodeError(f"{name}: invalid SH palette size {palette_size!r}.")
    entries = centroids[..., :3].reshape(-1, coefficients, 3)[:palette_size]
    # One extra all-zero entry for out-of-range labels, as splat-transform does.
    palette = np.zeros((palette_size + 1, coefficients, 3), dtype=np.float32)
    palette[:palette_size] = _codebook(meta, "shN")[entries]
    label = labels[:, 0].astype(np.int64) | (labels[:, 1].astype(np.int64) << 8)
    label[label >= palette_size] = palette_size
    for k, column in enumerate(columns.sh):
        column[rows] = np.ascontiguousarray(palette[:, k]).take(label, axis=0)


def _read_sog_meta(path: Path) -> dict[str, Any]:
    meta = _json(path.read_bytes(), path.name)
    _check_sog(meta, path.name)
    return meta


def decode_sog(path: str | Path, read_image: ImageReader = read_webp) -> SplatData:
    """Decode one SOG (a meta.json, or a folder containing one)."""
    meta_path = _manifest(Path(path))
    meta = _read_sog_meta(meta_path)
    columns = _Columns(meta["count"], _sh_coefficients(meta))
    _decode_into(columns, 0, meta_path, meta, read_image)
    return columns.splat_data()


def decode_level(
    path: str | Path, level: int = 0, read_image: ImageReader = read_webp, *, workers: int = 4
) -> SplatData:
    """Decode one level of an SSOG (a lod-meta.json, or a folder containing one)."""
    lod_path = _manifest(Path(path))
    lod = _json(lod_path.read_bytes(), lod_path.name)
    _check_lod(lod, lod_path.name)
    parts = []
    for relative, count in _level_files(lod, level, lod_path.name):
        meta_path = lod_path.parent / Path(*relative.parts)
        meta = _read_sog_meta(meta_path)
        if meta["count"] != count:
            raise DecodeError(f"{relative} holds {meta['count']} splats; {lod_path.name} lists {count}.")
        parts.append((meta_path, meta))
    coefficients = {_sh_coefficients(meta) for _, meta in parts}
    if len(coefficients) != 1:
        raise DecodeError(f"Level {level} mixes spherical-harmonics band counts.")
    columns = _Columns(sum(meta["count"] for _, meta in parts), coefficients.pop())
    starts = np.cumsum([0] + [meta["count"] for _, meta in parts[:-1]])
    # Files fill disjoint rows; OpenImageIO and numpy release the GIL for the heavy work.
    with ThreadPoolExecutor(max(1, workers)) as pool:
        for future in [
            pool.submit(_decode_into, columns, int(start), meta_path, meta, read_image)
            for start, (meta_path, meta) in zip(starts, parts)
        ]:
            future.result()
    return columns.splat_data()


def load_scene(
    path: str | Path,
    level: int = 0,
    *,
    environment: bool = True,
    read_image: ImageReader = read_webp,
    workers: int = 4,
) -> Scene:
    """Decode a local SOG or SSOG scene: a folder, meta.json or lod-meta.json.

    For SSOG, `level` picks the level of detail (0 is the finest) and the
    environment SOG is decoded too when `environment` is set. A plain SOG has
    only level 0 and no environment.
    """
    manifest = _manifest(Path(path))
    if manifest.name != LOD_META:
        if level != 0:
            raise DecodeError(f"Level {level} is out of range; the scene has only level 0.")
        splats = decode_sog(manifest, read_image)
        return Scene(splats, None, 0, [splats.count])
    lod = _json(manifest.read_bytes(), manifest.name)
    _check_lod(lod, manifest.name)
    splats = decode_level(manifest, level, read_image, workers=workers)
    env_path = _environment(lod, manifest.name) if environment else None
    env = decode_sog(manifest.parent / Path(*env_path.parts), read_image) if env_path else None
    counts = lod.get("counts") or [
        sum(count for _, count in _level_files(lod, index, manifest.name)) for index in range(lod["lodLevels"])
    ]
    return Scene(splats, env, level, list(counts))


# -- Downloading --------------------------------------------------------------------


def _without_query(url: str) -> str:
    """A URL for messages: signed URLs carry their signature in the query."""
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def http_download(
    url: str,
    path: str | Path,
    limit: int,
    *,
    cancel_event: threading.Event | None = None,
    on_bytes: Callable[[int], None] | None = None,
    timeout: float = 60.0,
    handlers: Sequence[urllib.request.BaseHandler] = (),
) -> int:
    """Stream `url` into `path` and return the byte count; refuse bodies over `limit` bytes.

    Meant for large signed URLs: nothing but a User-Agent is sent, redirects are
    refused, and messages leave out the query string that holds the signature.
    Raises UploadCancelled when `cancel_event` is set. `handlers` are extra
    urllib handlers; tests use them in place of the network.
    """
    shown = _without_query(url)
    if not is_secure_url(url):
        raise DecodeError(f"Files can only be downloaded over https: {shown}")
    opener = urllib.request.build_opener(_RejectRedirects(), *handlers)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    received = 0
    try:
        with opener.open(request, timeout=timeout) as response, Path(path).open("wb") as out:
            length = response.headers.get("Content-Length", "")
            if length.isdigit() and int(length) > limit:
                raise ResponseTooLarge(f"{shown} is larger than {limit} bytes.")
            # read1 returns what has arrived, so progress and Cancel keep up on slow links.
            read = getattr(response, "read1", response.read)
            while chunk := read(_READ_CHUNK):
                if cancel_event is not None and cancel_event.is_set():
                    raise UploadCancelled("Import cancelled.")
                received += len(chunk)
                if received > limit:
                    raise ResponseTooLarge(f"{shown} is larger than {limit} bytes.")
                out.write(chunk)
                if on_bytes is not None:
                    on_bytes(len(chunk))
            if length.isdigit() and received != int(length):
                raise DecodeError(f"Downloading {shown} stopped after {received} of {length} bytes.")
        return received
    except http.client.HTTPException as error:
        raise DecodeError(f"Downloading {shown} failed: {error!r}") from error
    except urllib.error.HTTPError as error:
        if 300 <= error.code < 400:
            raise DecodeError(f"Downloading {shown} was redirected (HTTP {error.code}); redirects are refused.") from error
        raise DownloadHttpError(f"Downloading {shown} failed with HTTP {error.code}.", error.code) from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise DecodeError(f"Downloading {shown} failed: {getattr(error, 'reason', error)}") from error
