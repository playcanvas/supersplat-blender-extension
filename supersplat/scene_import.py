"""Look up a SuperSplat scene and download it for Blender, independent of bpy.

ImportSession.run works on a background thread:

    read the API key
    GET /v1/splats/{id}                    the scene, its author and license; stop
                                           unless it is downloadable
    POST /v1/splats/{id}/downloads         target "sog" or "ssog" for scenes in those
      {target}                             formats, "ply" for the rest
    GET  .../downloads/{target}            poll until ready; once more after "failed"
    POST .../downloads/{target}/file       the signed url of the ZIP; fetch it. Any
                                           version but the lookup's stops the import
    sog, ssog                              unpack it and decode its meta.json or
                                           lod-meta.json (sog.py) at the chosen level;
                                           if that fails, download the "ply" target
    ply                                    unpack it and read its scene.ply

It ends in phase "decoded" holding the splats; the main thread turns them into
Blender objects and calls finish(). The only file URLs fetched are the signed
URLs of downloads; the published scene files are never read directly.
"""

from __future__ import annotations

import copy
import gzip
import logging
import os
import re
import shutil
import stat
import threading
import time
import urllib.parse
import uuid
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Protocol

from . import sog
from .api import ApiClient, Transport, format_bytes, is_secure_url
from .credentials import KEY_READ_TIMEOUT, CredentialStore, key_read_status, read_api_key
from .errors import (
    ApiError,
    ConfigurationError,
    DecodeError,
    DownloadHttpError,
    ProtocolError,
    SuperSplatError,
    UploadCancelled,
)
from .ply import SplatData, read_3dgs_ply

log = logging.getLogger(__name__)

SITE_HOSTS = frozenset({"superspl.at", "www.superspl.at"})
_SCENE_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_FILE_SUFFIXES = (".json", ".ply", ".sog", ".webp", ".zip", ".spz", ".splat")

# The largest scenes SuperSplat accepts are 15 GiB; the PLY of one is bigger than its SOG.
# This caps a download ZIP and what it unpacks to; a SOG file is capped at sog.MAX_FILE_BYTES.
MAX_DOWNLOAD_BYTES = 16 * 1024**3
MAX_ARCHIVE_MEMBERS = 100_000

EMPTY_LINK_MESSAGE = "Paste a SuperSplat link or scene ID first."
INVALID_LINK_MESSAGE = "Paste a SuperSplat scene link (superspl.at/scene/...) or a scene ID."
FILE_LINK_MESSAGE = (
    "That is a link to a file, not a scene. Paste the scene's SuperSplat link "
    "(superspl.at/scene/...) or its ID."
)
# Seconds between polls of a download that is being prepared.
POLL_INTERVAL = 2.0
# Formatted with the download's label: SOG, SSOG or PLY.
PREPARE_FAILED_MESSAGE = "SuperSplat could not prepare the {} download of this scene. Try again later."
PREPARE_TIMEOUT_MESSAGE = "SuperSplat is taking too long to prepare the {} download of this scene. Try again later."
REPUBLISHED_MESSAGE = "The scene was republished while importing. Import it again."
# 404 errors of the download routes for a scene that cannot be downloaded (any more).
_NOT_DOWNLOADABLE_ERRORS = frozenset({"downloadable splat not found", "splat not found"})
# The download target for each content.format; scenes in other formats download as "ply".
_TARGETS = {"sog": "sog", "ssog": "ssog"}

LICENSES = {
    "by": "CC BY 4.0",
    "by-sa": "CC BY-SA 4.0",
    "by-nd": "CC BY-ND 4.0",
    "by-nc": "CC BY-NC 4.0",
    "by-nc-sa": "CC BY-NC-SA 4.0",
    "by-nc-nd": "CC BY-NC-ND 4.0",
}


def site_hosts(environ: dict[str, str] | None = None) -> frozenset[str]:
    """superspl.at, plus any hosts in SUPERSPLAT_SITE_HOSTS (comma-separated)."""
    extra = (os.environ if environ is None else environ).get("SUPERSPLAT_SITE_HOSTS", "")
    return SITE_HOSTS | {host.strip().lower() for host in extra.split(",") if host.strip()}


def parse_scene_reference(text: str) -> str:
    """The scene ID in a SuperSplat link (superspl.at/scene/<id>, superspl.at/s?id=<id>) or a bare ID.

    Raises ConfigurationError for anything else, including links to files: the
    files of a scene are only ever fetched from the URLs the API returns.
    """
    value = text.strip()
    if not value:
        raise ConfigurationError(EMPTY_LINK_MESSAGE)
    if _SCENE_ID.match(value):
        return value
    try:
        parsed = urllib.parse.urlsplit(value if "://" in value else f"https://{value}")
        host = (parsed.hostname or "").lower()
    except ValueError:
        raise ConfigurationError(INVALID_LINK_MESSAGE) from None
    if parsed.path.lower().endswith(_FILE_SUFFIXES):
        raise ConfigurationError(FILE_LINK_MESSAGE)
    if parsed.scheme not in ("http", "https") or host not in site_hosts():
        raise ConfigurationError(INVALID_LINK_MESSAGE)
    segments = [segment for segment in parsed.path.split("/") if segment]
    scene_id = ""
    if len(segments) >= 2 and segments[0] == "scene":
        scene_id = urllib.parse.unquote(segments[1])
    elif segments == ["s"]:
        ids = urllib.parse.parse_qs(parsed.query).get("id", [])
        scene_id = ids[0] if len(ids) == 1 else ""
    if not _SCENE_ID.match(scene_id):
        raise ConfigurationError(INVALID_LINK_MESSAGE)
    return scene_id


def license_name(code: str) -> str:
    return LICENSES.get(code, code.upper() if code else "No license")


def license_url(code: str) -> str:
    """The Creative Commons deed of a license code, or "" for codes this version does not know."""
    return f"https://creativecommons.org/licenses/{code}/4.0/" if code in LICENSES else ""


@dataclass(frozen=True)
class SceneInfo:
    """What the scene lookup API says about a scene."""

    id: str
    title: str
    status: str
    format: str
    viewer_url: str
    author_username: str
    author_name: str
    downloadable: bool
    license: str = ""
    content_format: str = ""
    version: int | None = None
    splat_count: int | None = None
    lod_counts: tuple[int, ...] = ()

    @classmethod
    def from_api(cls, splat: dict[str, Any]) -> SceneInfo:
        def text(value: Any, label: str, default: str | None = None) -> str:
            if isinstance(value, str):
                return value
            if default is not None and value is None:
                return default
            raise ProtocolError(f"SuperSplat returned a scene without a valid {label}.")

        author = splat.get("author")
        if not isinstance(author, dict):
            raise ProtocolError("SuperSplat returned a scene without an author.")
        content = splat.get("content")
        if content is not None and not isinstance(content, dict):
            raise ProtocolError("SuperSplat returned invalid scene content.")
        content = content or {}
        counts = content.get("lodCounts") or []
        if not isinstance(counts, list) or not all(isinstance(c, int) and c >= 0 for c in counts):
            raise ProtocolError("SuperSplat returned invalid level-of-detail counts.")
        splat_count = content.get("splatCount")
        version = content.get("version")
        username = text(author.get("username"), "author", "")
        return cls(
            id=text(splat.get("id"), "id"),
            title=text(splat.get("title"), "title", "").strip() or "Untitled scene",
            status=text(splat.get("status"), "status", ""),
            format=text(splat.get("format"), "format", "").lower(),
            viewer_url=text(splat.get("viewerUrl"), "viewer URL", ""),
            author_username=username,
            author_name=text(author.get("displayName"), "author", "").strip() or username,
            downloadable=splat.get("downloadable") is True,
            license=text(splat.get("license"), "license", ""),
            content_format=text(content.get("format"), "content format", "").lower(),
            version=version if isinstance(version, int) else None,
            splat_count=splat_count if isinstance(splat_count, int) else None,
            lod_counts=tuple(counts),
        )

    @property
    def animated(self) -> bool:
        return self.format == "gsaf"

    @property
    def download_target(self) -> str:
        """What to ask the downloads endpoint for: the scene's own format, else a PLY."""
        return _TARGETS.get(self.content_format, "ply")

    @property
    def levels(self) -> int:
        """Known levels of detail: several for SSOG with lodCounts, else one."""
        return len(self.lod_counts) if self.content_format == "ssog" and self.lod_counts else 1

    @property
    def credit(self) -> str:
        return f"“{self.title}” by {self.author_name}"

    def problem(self) -> str:
        """Why this scene cannot be imported, or "" if it can."""
        if self.animated:
            return (
                f"{self.credit} is an animated (GSAF) scene. "
                "Animated scenes cannot be imported into Blender yet."
            )
        if self.downloadable:
            return ""
        if self.status == "processing":
            return f"{self.credit} is still processing on SuperSplat. Try again once it is published."
        return not_downloadable_message(self)


def not_downloadable_message(scene: SceneInfo | None) -> str:
    name = scene.credit if scene is not None else "This scene"
    return f"{name} is not downloadable: its owner has not allowed downloads."


def credit_lines(scene: SceneInfo) -> list[str]:
    """Attribution for one scene, as written to the SuperSplat Attributions text."""
    handle = f" (@{scene.author_username})" if scene.author_username else ""
    deed = license_url(scene.license)
    lines = [f"{scene.credit}{handle}"]
    if scene.viewer_url:
        lines.append(f"  Scene:   {scene.viewer_url}")
    lines.append(f"  License: {license_name(scene.license)}" + (f", {deed}" if deed else ""))
    return lines


class FileSource(Protocol):
    """Fetches the signed URLs of downloads, never with the API key."""

    def download(
        self,
        url: str,
        path: Path,
        limit: int,
        *,
        cancel_event: threading.Event,
        on_bytes: Callable[[int], None],
    ) -> int:
        """Stream a large file to `path`; see sog.http_download."""


class HttpFiles:
    def download(
        self,
        url: str,
        path: Path,
        limit: int,
        *,
        cancel_event: threading.Event,
        on_bytes: Callable[[int], None],
    ) -> int:
        return sog.http_download(url, path, limit, cancel_event=cancel_event, on_bytes=on_bytes)


@dataclass(frozen=True)
class ImportRequest:
    splat_id: str
    credentials: CredentialStore
    base_url: str
    staging_dir: Path
    # SSOG level of detail; 0 is the finest. Clamped to the levels the scene has.
    level: int = 0
    environment: bool = True
    # Stop after the lookup, to show the scene and its levels before importing.
    lookup_only: bool = False
    key_timeout: float = KEY_READ_TIMEOUT
    poll_interval: float | None = None  # None uses POLL_INTERVAL
    prepare_timeout: float = 20 * 60.0
    retries: int = 3


@dataclass
class DecodedScene:
    source: str  # "sog", "ssog" or "ply"
    splats: SplatData
    environment: SplatData | None = None
    level: int = 0


@dataclass
class ImportState:
    # authorizing, looking_up, preparing, downloading, decoding -> decoded -> complete;
    # found (lookup only, or reopen() after complete), failed, cancelled
    phase: str = "idle"
    status_text: str = ""
    progress: float | None = None  # None while there is no measurable progress
    progress_text: str = ""
    error: str = ""
    splat_id: str = ""
    lookup_only: bool = False
    scene: SceneInfo | None = None
    source: str = ""
    level: int = 0
    # Why the PLY download was used instead of the scene's SOG or SSOG download.
    note: str = ""
    object_name: str = ""
    environment_name: str = ""
    splat_count: int = 0

    @property
    def active(self) -> bool:
        return self.phase in {"authorizing", "looking_up", "downloading", "preparing", "decoding"}


def format_count(count: int) -> str:
    """Splat counts for labels: 2,100 / 466k / 7.5M."""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M".replace(".0M", "M")
    if count >= 10_000:
        return f"{count / 1000:.0f}k"
    return f"{count:,}"


class ImportSession:
    def __init__(
        self,
        request: ImportRequest,
        *,
        transport: Transport | None = None,
        files: FileSource | None = None,
    ) -> None:
        self.request = request
        self.transport = transport
        self.files = files or HttpFiles()
        self.cancel_event = threading.Event()
        self._lock = threading.Lock()
        self._state = ImportState(
            phase="authorizing",
            status_text=key_read_status(request.credentials),
            splat_id=request.splat_id,
            lookup_only=request.lookup_only,
        )
        self._result: DecodedScene | None = None
        self._thread: threading.Thread | None = None

    # -- control, from the main thread ---------------------------------------------

    def snapshot(self) -> ImportState:
        with self._lock:
            return copy.copy(self._state)

    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, daemon=True, name="supersplat-import")
        self._thread.start()

    def join(self, timeout: float | None = None) -> bool:
        if self._thread is not None:
            self._thread.join(timeout)
            return not self._thread.is_alive()
        return True

    def cancel(self) -> None:
        """Stop the worker, or drop a decoded scene the main thread has not built yet."""
        self.cancel_event.set()
        with self._lock:
            if self._state.phase == "decoded":
                self._result = None
                self._state.phase, self._state.status_text = "cancelled", "Import cancelled."
            elif self._state.active:
                self._state.status_text = "Cancelling..."

    def take_result(self) -> DecodedScene | None:
        """Hand the decoded splats to the main thread (once) to build objects from."""
        with self._lock:
            result, self._result = self._result, None
            return result

    def finish(self, *, object_name: str, environment_name: str = "", splat_count: int = 0) -> None:
        self._update(
            phase="complete",
            status_text="Imported.",
            progress=1.0,
            object_name=object_name,
            environment_name=environment_name,
            splat_count=splat_count,
        )

    def reopen(self) -> None:
        """Back from an import to its lookup, to pick options and import the scene again."""
        with self._lock:
            if self._state.phase == "complete":
                self._state.phase, self._state.status_text = "found", "Ready to import."
                self._state.note = ""

    def fail(self, message: str) -> None:
        with self._lock:
            self._result = None
        self._update(phase="failed", status_text="Import failed.", error=message)

    # -- worker ------------------------------------------------------------------------

    def run(self) -> ImportState:
        request = self.request
        work = Path(request.staging_dir) / f"import-{uuid.uuid4().hex}"
        try:
            api_key = read_api_key(
                request.credentials, timeout=request.key_timeout, cancel_event=self.cancel_event
            )
            client = ApiClient(api_key, request.base_url, transport=self.transport)
            self._update(phase="looking_up", status_text="Looking up the scene...")
            scene = self._lookup(client)
            self._update(scene=scene)
            problem = scene.problem()
            if problem:
                raise SuperSplatError(problem)
            if request.lookup_only:
                self._update(phase="found", status_text="Ready to import.")
                return self.snapshot()
            work.mkdir(parents=True, exist_ok=True)
            result = None
            reason = ""
            target = scene.download_target
            if target != "ply":
                archive = self._download(client, scene, target, work / f"{target}.zip")
                try:
                    result = self._decode_published(scene, archive, work / target)
                except UploadCancelled:
                    raise
                except Exception as error:  # noqa: BLE001 - any unpack or decode failure falls back to the PLY
                    self._check_cancel()
                    reason = (str(error) or error.__class__.__name__).rstrip(".")
                    finest = " (its finest level, without the environment)" if target == "ssog" else ""
                    self._update(
                        note=f"The {target.upper()} files could not be decoded ({reason}), "
                        f"so SuperSplat's PLY of the scene was imported instead{finest}."
                    )
                    # Make room for the PLY.
                    archive.unlink(missing_ok=True)
                    shutil.rmtree(work / target, ignore_errors=True)
            if result is None:
                try:
                    result = self._download_ply(client, scene, work)
                except SuperSplatError as error:
                    if reason and not isinstance(error, UploadCancelled):
                        raise SuperSplatError(
                            f"{error} The {target.upper()} files could not be decoded "
                            f"either: {reason}."
                        ) from error
                    raise
            with self._lock:
                if self.cancel_event.is_set():
                    raise UploadCancelled("Import cancelled.")
                self._result = result
                for name, value in (
                    ("phase", "decoded"), ("status_text", "Creating objects..."), ("progress", None),
                    ("progress_text", ""), ("source", result.source), ("level", result.level),
                ):
                    setattr(self._state, name, value)
        except UploadCancelled:
            self._update(phase="cancelled", status_text="Import cancelled.", error="")
        except SuperSplatError as error:
            self.fail(str(error) or error.__class__.__name__)
        except Exception as error:  # noqa: BLE001 - surfaced in the panel; the traceback goes to the log
            log.exception("Unexpected error importing a SuperSplat scene")
            self.fail(f"Unexpected error: {error.__class__.__name__}: {error}")
        finally:
            shutil.rmtree(work, ignore_errors=True)
        return self.snapshot()

    def _lookup(self, client: ApiClient) -> SceneInfo:
        splat_id = self.request.splat_id
        try:
            splat = self._call(lambda: client.get_splat(splat_id))
        except ApiError as error:
            if error.status == 404:
                raise SuperSplatError(
                    f"No SuperSplat scene has the ID “{splat_id}”. Check the link."
                ) from error
            raise
        scene = SceneInfo.from_api(splat)
        if scene.id != splat_id:
            raise ProtocolError("SuperSplat returned a different scene than requested.")
        return scene

    def _decode_published(self, scene: SceneInfo, archive: Path, folder: Path) -> DecodedScene:
        """Unpack a SOG or SSOG download and decode the chosen level (and environment)."""
        request = self.request
        target = scene.download_target
        level = min(max(request.level, 0), scene.levels - 1) if target == "ssog" else 0
        self._update(level=level)
        self._unpack(archive, folder, sog.MAX_FILE_BYTES)
        manifest = find_manifest(folder)
        self._update(status_text=f"Decoding {target.upper()}...", progress=None, progress_text="")
        decoded = sog.load_scene(manifest, level, environment=request.environment)
        self._check_cancel()
        return DecodedScene(target, decoded.splats, decoded.environment, decoded.level)

    def _download_ply(self, client: ApiClient, scene: SceneInfo, work: Path) -> DecodedScene:
        archive = self._download(client, scene, "ply", work / "ply.zip")
        folder = work / "ply"
        self._unpack(archive, folder, MAX_DOWNLOAD_BYTES)
        ply = find_ply(folder)
        self._update(status_text="Reading PLY...", progress=0.0, progress_text="")
        splats = read_3dgs_ply(ply, cancel_event=self.cancel_event, on_progress=lambda f: self._update(progress=f))
        return DecodedScene("ply", splats, None, 0)

    def _unpack(self, archive: Path, folder: Path, file_limit: int) -> None:
        self._check_cancel()
        self._update(phase="decoding", status_text="Unpacking...", progress=0.0, progress_text="")
        unpack_archive(
            archive,
            folder,
            limit=MAX_DOWNLOAD_BYTES,
            file_limit=file_limit,
            cancel_event=self.cancel_event,
            on_progress=lambda f: self._update(progress=f),
        )
        archive.unlink(missing_ok=True)

    def _download(self, client: ApiClient, scene: SceneInfo, target: str, archive: Path) -> Path:
        """Have SuperSplat prepare the `target` download of the scene, and fetch its ZIP to `archive`."""
        label = target.upper()
        self._prepare(client, scene, target)
        fresh_url = True
        while True:
            file = self._file(client, scene, target)
            size = file["bytes"] or 0  # None (unknown): the download still runs, without a total
            if size > MAX_DOWNLOAD_BYTES:
                raise DecodeError(
                    f"The {label} download of this scene is {format_bytes(size)}, over the "
                    f"{format_bytes(MAX_DOWNLOAD_BYTES)} download limit."
                )
            self._update(
                phase="downloading", status_text=f"Downloading {label}...", progress=0.0 if size else None,
                progress_text="",
            )
            received = 0

            def on_bytes(count: int) -> None:
                nonlocal received
                received += count
                text = f"{format_bytes(received)} / {format_bytes(size)}" if size else format_bytes(received)
                self._update(progress=min(received / size, 1.0) if size else None, progress_text=text)

            try:
                self.files.download(
                    file["url"], archive, MAX_DOWNLOAD_BYTES, cancel_event=self.cancel_event, on_bytes=on_bytes
                )
                return archive
            except DownloadHttpError as error:
                # Signed URLs last minutes; ask for a new one, once.
                if not fresh_url or error.status not in (400, 401, 403, 404):
                    raise
                fresh_url = False

    def _prepare(self, client: ApiClient, scene: SceneInfo, target: str) -> dict[str, Any]:
        """Request the `target` download and poll it until SuperSplat has packaged its ZIP."""
        request = self.request
        label = target.upper()
        deadline = time.monotonic() + request.prepare_timeout
        requeued = False  # a failed (or stalled) or vanished download is requested again, once

        def create() -> dict[str, Any]:
            return self._current(scene, self._download_call(lambda: client.create_download(scene.id, target), scene))

        self._update(
            phase="preparing", status_text=f"Requesting the {label} download...", progress=None, progress_text=""
        )
        download = create()
        while True:
            status = download.get("status")
            if status == "ready":
                return download
            if status == "failed":
                if requeued:
                    raise SuperSplatError(PREPARE_FAILED_MESSAGE.format(label))
                requeued = True
                download = create()
                continue
            if status not in ("queued", "processing"):
                raise ProtocolError(f"SuperSplat returned an unknown download status {status!r}.")
            self._update(status_text="Preparing...", progress_text=f"SuperSplat is packaging the {label} ({status})")
            if time.monotonic() >= deadline:
                raise SuperSplatError(PREPARE_TIMEOUT_MESSAGE.format(label))
            self._sleep(POLL_INTERVAL if request.poll_interval is None else request.poll_interval)
            try:
                download = self._current(
                    scene, self._download_call(lambda: client.get_download(scene.id, target), scene)
                )
            except ApiError as error:
                # 404: no current download of this target any more; request it again.
                if error.status != 404 or requeued:
                    raise
                requeued = True
                download = create()

    def _file(self, client: ApiClient, scene: SceneInfo, target: str) -> dict[str, Any]:
        """The signed URL of a ready download."""
        for attempt in range(2):
            try:
                file = self._download_call(lambda: client.create_download_file(scene.id, target), scene)
            except ApiError as error:
                # 409 not ready, or 404 (no download, or its file is gone): prepare it again, once.
                if attempt or error.status not in (404, 409):
                    raise
                self._prepare(client, scene, target)
                continue
            url, size = file.get("url"), file.get("bytes")
            if not isinstance(url, str) or not is_secure_url(url):
                raise ProtocolError("SuperSplat returned a download file without a valid https URL.")
            if size is not None and (not isinstance(size, int) or size < 0):
                raise ProtocolError("SuperSplat returned a download file with an invalid size.")
            return self._current(scene, file)
        raise AssertionError("unreachable")

    @staticmethod
    def _current(scene: SceneInfo, value: dict[str, Any]) -> dict[str, Any]:
        """`value`, unless it belongs to another version of the scene than the one looked up."""
        version = value.get("version")
        if scene.version is not None and isinstance(version, int) and version != scene.version:
            raise SuperSplatError(REPUBLISHED_MESSAGE)
        return value

    def _download_call(self, operation: Callable[[], dict[str, Any]], scene: SceneInfo) -> dict[str, Any]:
        try:
            return self._call(operation)
        except ApiError as error:
            body = error.body if isinstance(error.body, dict) else {}
            # Other 404s ("download not found", "download file not found") mean: request it again.
            if error.status == 404 and body.get("error") in _NOT_DOWNLOADABLE_ERRORS:
                raise SuperSplatError(not_downloadable_message(scene)) from error
            raise

    def _call(self, operation: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """Run an API call, retrying rate limits, server errors and network failures."""
        for attempt in range(self.request.retries + 1):
            self._check_cancel()
            try:
                return operation()
            except ApiError as error:
                if attempt >= self.request.retries or not error.transient:
                    raise
                delay = error.retry_after if error.retry_after is not None else 2.0**attempt
                self._sleep(min(delay, 30.0))
        raise AssertionError("unreachable")

    def _sleep(self, seconds: float) -> None:
        if self.cancel_event.wait(max(seconds, 0.0)):
            raise UploadCancelled("Import cancelled.")

    def _check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise UploadCancelled("Import cancelled.")

    def _update(self, **values: Any) -> None:
        with self._lock:
            for name, value in values.items():
                setattr(self._state, name, value)


def _member_path(info: zipfile.ZipInfo) -> PurePosixPath:
    """A ZIP member's name as a relative path, refused if it could land outside the folder."""
    name = info.filename
    parts = name.split("/")
    if (
        name.startswith("/")
        or any(part in ("", ".", "..") for part in parts)
        or any(char in name for char in "\\:\0")
        or stat.S_ISLNK(info.external_attr >> 16)
    ):
        raise DecodeError(f"The download holds an unsafe file name: {name!r}")
    return PurePosixPath(*parts)


def unpack_archive(
    archive: Path,
    folder: Path,
    *,
    limit: int,
    file_limit: int,
    cancel_event: threading.Event | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> None:
    """Unpack a download ZIP into `folder`.

    Names that are absolute, hold `..`, backslashes or drive letters, and
    symlinks, are refused, as are archives that unpack to more than `limit`
    bytes or hold a file over `file_limit`. Files stored gzip-compressed (as the
    published manifests are) are written decompressed. A bare PLY, rather than
    a ZIP, is accepted as scene.ply.
    """
    folder.mkdir(parents=True, exist_ok=True)
    with archive.open("rb") as stream:
        bare_ply = stream.read(4) == b"ply\n"
    if bare_ply:
        if archive.stat().st_size > file_limit:
            raise DecodeError(f"scene.ply is over the {format_bytes(file_limit)} limit.")
        shutil.move(archive, folder / "scene.ply")
        return
    try:
        with zipfile.ZipFile(archive) as bundle:
            members = [info for info in bundle.infolist() if not info.is_dir()]
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise DecodeError(f"The download holds {len(members):,} files, more than {MAX_ARCHIVE_MEMBERS:,}.")
            total = sum(info.file_size for info in members)
            if total > limit:
                raise DecodeError(
                    f"The download unpacks to {format_bytes(total)}, over the {format_bytes(limit)} limit."
                )
            written = 0
            done = 0  # entry sizes of the members unpacked, for progress
            for info in members:
                relative = _member_path(info)
                if info.file_size > file_limit:
                    raise DecodeError(f"{relative} is {format_bytes(info.file_size)}, over the {format_bytes(file_limit)} limit.")
                target = folder.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                copied = 0
                # "xb": a second member of the same name is an error, not an overwrite.
                with bundle.open(info) as source, target.open("xb") as out:
                    gzipped = source.peek(2)[:2] == b"\x1f\x8b"
                    reader = gzip.GzipFile(fileobj=source) if gzipped else source
                    while True:
                        try:
                            chunk = reader.read(1024 * 1024)
                        except (EOFError, zlib.error, gzip.BadGzipFile) as error:
                            if not gzipped:
                                raise
                            raise DecodeError(f"{relative} is not valid gzip: {error}") from error
                        if not chunk:
                            break
                        if cancel_event is not None and cancel_event.is_set():
                            raise UploadCancelled("Import cancelled.")
                        copied += len(chunk)
                        written += len(chunk)
                        if copied > file_limit or written > limit:
                            raise DecodeError(f"{relative} unpacks to more than the download limit.")
                        out.write(chunk)
                        if on_progress is not None and total:
                            on_progress(min((done + min(copied, info.file_size)) / total, 1.0))
                done += info.file_size
                if on_progress is not None and total:
                    on_progress(done / total)
    except FileExistsError as error:
        raise DecodeError(f"The download holds {Path(error.filename).name} twice.") from error
    except (zipfile.BadZipFile, EOFError, zlib.error, gzip.BadGzipFile) as error:
        raise DecodeError(f"The download is not a valid ZIP archive: {error}") from error
    except (NotImplementedError, RuntimeError) as error:  # unsupported compression, encryption
        raise DecodeError(f"The download could not be unpacked: {error}") from error


def find_manifest(folder: Path) -> Path:
    """The lod-meta.json (else meta.json) of an unpacked SOG or SSOG download.

    Looked for at the top of the folder, then one folder down, so the files may
    be wrapped in a folder. SSOG level folders hold meta.json files of their
    own, which is why lod-meta.json wins wherever it is.
    """
    for name in (sog.LOD_META, sog.META):
        for pattern in (name, f"*/{name}"):
            found = sorted(path for path in folder.glob(pattern) if path.is_file())
            if len(found) > 1:
                raise DecodeError(f"The download holds {len(found)} {name} files; expected one.")
            if found:
                return found[0]
    raise DecodeError(f"The download holds neither {sog.LOD_META} nor {sog.META}.")


def find_ply(folder: Path) -> Path:
    """The scene.ply of an unpacked PLY download (or its one PLY of another name)."""
    plys = sorted(path for path in folder.rglob("*") if path.is_file() and path.suffix.lower() == ".ply")
    named = [path for path in plys if path.name.lower() == "scene.ply"]
    chosen = named or plys
    if len(chosen) != 1:
        raise DecodeError(f"The PLY download should hold one scene.ply; it holds {len(chosen)} PLY files.")
    return chosen[0]
