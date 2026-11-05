"""3D Gaussian splat PLY writer and reader.

Blender 5.3 imports a 3DGS PLY into a POINTCLOUD with `type == 'GAUSSIAN_SPLAT'`
and these point attributes:

    position       FLOAT_VECTOR  (x, y, z)
    radiance:base  FLOAT4        (f_dc_0, f_dc_1, f_dc_2, sigmoid(opacity))
    scale          FLOAT_VECTOR  (exp(scale_0), exp(scale_1), exp(scale_2))
    rotation       QUATERNION    (rot_0, rot_1, rot_2, rot_3), normalized, w first
    radiance:sh_k  FLOAT_VECTOR  (f_rest_k, f_rest_{k+K}, f_rest_{k+2K}), K = coefficients per channel

The writer applies the inverse mapping, so an imported PLY round-trips (up to
float32 precision and the opacity clamp below). The reader applies the same
mapping as Blender's importer, off the main thread.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

import numpy as np

from .errors import DecodeError, ExportError, UploadCancelled

# Keeps the opacity logit finite for fully transparent/opaque splats: |opacity| <= ~13.8.
ALPHA_EPSILON = 1e-6
SCALE_FLOOR = 1e-30
CHUNK_ROWS = 65536


def coefficients_for_degree(degree: int) -> int:
    """Higher-order SH coefficients per colour channel (excluding DC)."""
    return (degree + 1) ** 2 - 1


def degree_for_coefficients(count: int) -> int:
    for degree in (3, 2, 1):
        if count >= coefficients_for_degree(degree):
            return degree
    return 0


@dataclass
class SplatData:
    """Flat per-splat columns in Blender's attribute layout."""

    count: int
    position: Sequence[float]
    base: Sequence[float]
    scale: Sequence[float]
    rotation: Sequence[float]
    sh: list[Sequence[float]] = field(default_factory=list)

    @property
    def sh_degree(self) -> int:
        return degree_for_coefficients(len(self.sh))

    def validate(self) -> None:
        if self.count <= 0:
            raise ExportError("The splat object contains no points.")
        expected = {
            "position": (self.position, 3),
            "radiance:base": (self.base, 4),
            "scale": (self.scale, 3),
            "rotation": (self.rotation, 4),
        }
        for index, column in enumerate(self.sh):
            expected[f"radiance:sh_{index}"] = (column, 3)
        for name, (values, width) in expected.items():
            if len(values) != self.count * width:
                raise ExportError(
                    f"Attribute '{name}' has {len(values)} values; expected {self.count * width}."
                )


def property_names(sh_degree: int) -> list[str]:
    rest = 3 * coefficients_for_degree(sh_degree)
    return (
        ["x", "y", "z", "f_dc_0", "f_dc_1", "f_dc_2"]
        + [f"f_rest_{index}" for index in range(rest)]
        + ["opacity", "scale_0", "scale_1", "scale_2", "rot_0", "rot_1", "rot_2", "rot_3"]
    )


def ply_header(count: int, sh_degree: int) -> bytes:
    lines = ["ply", "format binary_little_endian 1.0", f"element vertex {count}"]
    lines += [f"property float {name}" for name in property_names(sh_degree)]
    lines.append("end_header")
    return ("\n".join(lines) + "\n").encode("ascii")


def write_3dgs_ply(
    path: str | Path,
    data: SplatData,
    sh_degree: int | None = None,
    *,
    cancel_event: threading.Event | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> int:
    """Write `data` as a binary little-endian 3DGS PLY and return its size in bytes.

    `sh_degree` is clamped to the degree available in `data`; None writes all of it.
    """
    data.validate()
    available = data.sh_degree
    degree = available if sh_degree is None else max(0, min(int(sh_degree), available))
    rest = coefficients_for_degree(degree)

    def column(values: Sequence[float], width: int) -> np.ndarray:
        return np.asarray(values, dtype=np.float32).reshape(-1, width)

    position = column(data.position, 3)
    base = column(data.base, 4)
    scale = column(data.scale, 3)
    rotation = column(data.rotation, 4)
    sh = [column(values, 3) for values in data.sh[:rest]]

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("wb") as stream:
            stream.write(ply_header(data.count, degree))
            for start in range(0, data.count, CHUNK_ROWS):
                if cancel_event is not None and cancel_event.is_set():
                    raise UploadCancelled("Export cancelled.")
                rows = slice(start, min(start + CHUNK_ROWS, data.count))
                out = np.empty((rows.stop - start, 14 + 3 * rest), dtype="<f4")
                out[:, 0:3] = position[rows]
                out[:, 3:6] = base[rows, 0:3]
                # f_rest is channel-major: all coefficients of R, then G, then B.
                for k, coefficients in enumerate(sh):
                    for channel in range(3):
                        out[:, 6 + channel * rest + k] = coefficients[rows, channel]
                offset = 6 + 3 * rest
                alpha = np.clip(base[rows, 3].astype(np.float64), ALPHA_EPSILON, 1.0 - ALPHA_EPSILON)
                out[:, offset] = np.log(alpha / (1.0 - alpha))
                out[:, offset + 1 : offset + 4] = np.log(
                    np.maximum(scale[rows].astype(np.float64), SCALE_FLOOR)
                )
                out[:, offset + 4 : offset + 8] = rotation[rows]
                stream.write(out.tobytes())
                if on_progress is not None:
                    on_progress(rows.stop / data.count)
    except OSError as error:
        raise ExportError(f"Could not write the PLY export: {error.strerror or error}") from error
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path.stat().st_size


# -- Reading ------------------------------------------------------------------------

_PLY_TYPES = {
    "char": "i1", "int8": "i1", "uchar": "u1", "uint8": "u1",
    "short": "i2", "int16": "i2", "ushort": "u2", "uint16": "u2",
    "int": "i4", "int32": "i4", "uint": "u4", "uint32": "u4",
    "float": "f4", "float32": "f4", "double": "f8", "float64": "f8",
}
_PLY_FORMATS = {"binary_little_endian": "<", "binary_big_endian": ">"}
_MAX_HEADER_BYTES = 64 * 1024
_REQUIRED_PROPERTIES = property_names(0)


def _ply_layout(path: Path) -> tuple[int, int, np.dtype]:
    """(vertex count, byte offset of the vertex rows, row dtype) of a binary PLY."""
    with path.open("rb") as stream:
        head = stream.read(_MAX_HEADER_BYTES)
    end = head.find(b"end_header\n")
    if not head.startswith(b"ply\n") or end < 0:
        raise DecodeError(f"{path.name} is not a PLY file.")
    try:
        lines = head[:end].decode("ascii").splitlines()[1:]
    except UnicodeDecodeError as error:
        raise DecodeError(f"{path.name} has an invalid PLY header.") from error
    order = None
    elements: list[tuple[str, int, list[tuple[str, str]]]] = []
    for line in lines:
        words = line.split()
        if not words or words[0] in ("comment", "obj_info"):
            continue
        if words[0] == "format" and len(words) == 3:
            order = _PLY_FORMATS.get(words[1])
            if order is None:
                raise DecodeError(f"{path.name} is a {words[1]} PLY; only binary PLY files are supported.")
        elif words[0] == "element" and len(words) == 3 and words[2].isdigit():
            elements.append((words[1], int(words[2]), []))
        elif words[0] == "property" and elements and len(words) == 3 and words[1] in _PLY_TYPES:
            elements[-1][2].append((words[2], _PLY_TYPES[words[1]]))
        elif words[0] == "property" and elements and words[1:2] == ["list"]:
            elements[-1][2].append((words[-1], "list"))
        else:
            raise DecodeError(f"{path.name} has an unsupported PLY header line: {line!r}")
    if order is None:
        raise DecodeError(f"{path.name} has no PLY format line.")
    offset = end + len(b"end_header\n")
    for name, count, properties in elements:
        if any(kind == "list" for _, kind in properties):
            raise DecodeError(f"{path.name}: element {name!r} has list properties, which are not supported.")
        dtype = np.dtype([(prop, order + kind) for prop, kind in properties])
        if name == "vertex":
            return count, offset, dtype
        offset += count * dtype.itemsize
    raise DecodeError(f"{path.name} has no vertex element.")


def read_3dgs_ply(
    path: str | Path,
    *,
    cancel_event: threading.Event | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> SplatData:
    """Read a binary 3DGS PLY into Blender's attribute layout (see the module docstring).

    Properties may come in any order and any numeric type. Raises DecodeError
    for files that are not 3DGS PLYs.
    """
    path = Path(path)
    count, offset, dtype = _ply_layout(path)
    names = dtype.names or ()
    missing = [name for name in _REQUIRED_PROPERTIES if name not in names]
    if missing:
        raise DecodeError(f"{path.name} is not a Gaussian splat PLY; it lacks {', '.join(missing)}.")
    if count <= 0:
        raise DecodeError(f"{path.name} contains no splats.")
    if path.stat().st_size < offset + count * dtype.itemsize:
        raise DecodeError(f"{path.name} is truncated: it should hold {count} splats.")
    rest = 0
    while f"f_rest_{rest}" in names:
        rest += 1
    coefficients = coefficients_for_degree(degree_for_coefficients(rest // 3))
    stride = rest // 3

    rows = np.memmap(path, dtype=dtype, mode="r", offset=offset, shape=(count,))
    position = np.empty((count, 3), dtype=np.float32)
    base = np.empty((count, 4), dtype=np.float32)
    scale = np.empty((count, 3), dtype=np.float32)
    rotation = np.empty((count, 4), dtype=np.float32)
    sh = [np.empty((count, 3), dtype=np.float32) for _ in range(coefficients)]
    try:
        for start in range(0, count, CHUNK_ROWS * 16):
            if cancel_event is not None and cancel_event.is_set():
                raise UploadCancelled("Import cancelled.")
            chunk = slice(start, min(start + CHUNK_ROWS * 16, count))
            block = rows[chunk]

            def field(name: str) -> np.ndarray:
                return np.asarray(block[name], dtype=np.float64)

            for axis, name in enumerate(("x", "y", "z")):
                position[chunk, axis] = field(name)
            for channel in range(3):
                base[chunk, channel] = field(f"f_dc_{channel}")
            base[chunk, 3] = 1.0 / (1.0 + np.exp(-field("opacity")))
            for axis in range(3):
                scale[chunk, axis] = np.exp(field(f"scale_{axis}"))
            quaternion = np.stack([field(f"rot_{axis}") for axis in range(4)], axis=1)
            norm = np.linalg.norm(quaternion, axis=1, keepdims=True)
            identity = norm[:, 0] == 0
            quaternion[identity] = (1.0, 0.0, 0.0, 0.0)
            norm[identity] = 1.0
            rotation[chunk] = quaternion / norm
            for k, column in enumerate(sh):
                for channel in range(3):
                    column[chunk, channel] = field(f"f_rest_{k + channel * stride}")
            if on_progress is not None:
                on_progress(chunk.stop / count)
    finally:
        del rows
    return SplatData(
        count=count,
        position=position.reshape(-1),
        base=base.reshape(-1),
        scale=scale.reshape(-1),
        rotation=rotation.reshape(-1),
        sh=[column.reshape(-1) for column in sh],
    )
