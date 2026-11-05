"""In-memory SuperSplat API that implements the extension's HTTP transport.

Responses follow the schemas in tests/fixtures/openapi.json, and every request
body and response is checked against that spec. No sockets are opened.

Apart from publishing, it serves scenes for import: register FakeScene objects in
`scenes` (demo_scenes() builds the standard set from the fixtures). Their
download ZIPs (targets sog, ssog and ply) are served by download(), the
extension's FileSource interface, or as raw responses by file_response() for
scripts/mock_api.py, which puts this fake behind a local HTTP server. The
published scene files themselves are never served: only ZIPs are.

The ZIP layout assumed until the server's is confirmed:

    sog    meta.json and its WebP textures, LICENSE.txt
    ssog   lod-meta.json (gzip-compressed, as published), every level's folders,
           env/, LICENSE.txt
    ply    scene.ply, LICENSE.txt
"""

from __future__ import annotations

import gzip
import io
import json
import re
import threading
import time
import urllib.parse
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

import spec_check
from supersplat.api import HttpResponse, TransportError
from supersplat.errors import DownloadHttpError, ResponseTooLarge, UploadCancelled

FAKE_BASE_URL = "https://api.supersplat.test"
FAKE_STORAGE = "https://storage.example.invalid"
FAKE_DOWNLOADS = "https://downloads.example.invalid"
FAKE_SITE = "https://example.invalid"
FAKE_API_KEY = "dummy-api-key-0000"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "sog"
_TIMESTAMP = "2099-01-01T00:00:00.000Z"
_RESETS_AT = "2099-01-02T00:00:00.000Z"
DAILY_DOWNLOAD_LIMIT = 5
DAILY_LIMIT_MESSAGE = "Your account has reached today's download limit. Try again later."

_ROUTES = [
    ("GET", re.compile(r"^/v1/me$"), "/v1/me"),
    ("GET", re.compile(r"^/v1/splats/(?P<id>[^/]+)$"), "/v1/splats/{splatId}"),
    ("POST", re.compile(r"^/v1/splats/uploads$"), "/v1/splats/uploads"),
    (
        "POST",
        re.compile(r"^/v1/splats/uploads/(?P<id>[^/]+)/part-upload-urls$"),
        "/v1/splats/uploads/{uploadId}/part-upload-urls",
    ),
    (
        "POST",
        re.compile(r"^/v1/splats/uploads/(?P<id>[^/]+)/complete$"),
        "/v1/splats/uploads/{uploadId}/complete",
    ),
    ("POST", re.compile(r"^/v1/splats/(?P<id>[^/]+)/downloads$"), "/v1/splats/{splatId}/downloads"),
    (
        "GET",
        re.compile(r"^/v1/splats/(?P<id>[^/]+)/downloads/(?P<target>[^/]+)$"),
        "/v1/splats/{splatId}/downloads/{target}",
    ),
    (
        "POST",
        re.compile(r"^/v1/splats/(?P<id>[^/]+)/downloads/(?P<target>[^/]+)/file$"),
        "/v1/splats/{splatId}/downloads/{target}/file",
    ),
]


@dataclass
class Recorded:
    method: str
    url: str
    route: str
    headers: dict[str, str]
    json: Any = None


@dataclass
class FakeScene:
    """A published scene that the lookup and download endpoints serve."""

    id: str
    title: str
    # Splat.format; also content.format when downloadable ("sog", "ssog", "compressed-ply", "gsaf").
    format: str = "sog"
    author_username: str = "dummy-artist"
    author_name: str = "Dummy Artist"
    license: str = "by"
    downloadable: bool = True
    status: str = "completed"
    # The published files, zipped as the sog or ssog download; `files` adds or replaces some.
    folder: Path | None = None
    files: dict[str, bytes] = field(default_factory=dict)
    # Zipped as the ply download.
    ply: Path | None = None
    ply_steps: tuple[str, ...] = ("queued", "processing", "ready")
    # Steps of the sog or ssog download.
    sog_steps: tuple[str, ...] = ("queued", "processing", "ready")
    splat_count: int | None = None
    lod_counts: list[int] | None = None
    # content.version; republishing bumps it, and downloads of older versions stop being current.
    version: int = 1

    @property
    def targets(self) -> tuple[str, ...]:
        """The download targets the server prepares: the scene's own format, and a PLY."""
        return (self.format, "ply") if self.format in ("sog", "ssog") else ("ply",)

    def steps(self, target: str) -> tuple[str, ...]:
        return self.ply_steps if target == "ply" else self.sog_steps

    def published_files(self) -> dict[str, bytes]:
        """The scene's SOG or SSOG files by relative path, PLYs left out."""
        found: dict[str, bytes] = {}
        if self.folder is not None:
            for path in sorted(self.folder.rglob("*")):
                if path.is_file() and path.suffix.lower() != ".ply" and not path.name.startswith("."):
                    found[path.relative_to(self.folder).as_posix()] = path.read_bytes()
        found.update(self.files)
        return found


def scene_from_folder(scene_id: str, title: str, folder: Path, **values: Any) -> FakeScene:
    """A SOG or SSOG FakeScene for a folder holding meta.json or lod-meta.json."""
    lod = folder / "lod-meta.json"
    if lod.is_file():
        meta = _read_json(lod)
        counts = meta.get("counts") or None
        return FakeScene(
            scene_id, title, "ssog", folder=folder,
            splat_count=counts[0] if counts else None, lod_counts=counts, **values,
        )
    meta = _read_json(folder / "meta.json")
    return FakeScene(scene_id, title, "sog", folder=folder, splat_count=meta.get("count"), **values)


def demo_scenes(fixtures: Path = FIXTURES) -> list[FakeScene]:
    """The scenes the Blender tests and the mock server import."""
    v1_meta = _read_json(fixtures / "sh3" / "meta.json")
    v1_meta.pop("version")  # a legacy (version 1) SOG, which the decoder refuses
    return [
        scene_from_folder("sogdemo1", "SOG Demo", fixtures / "sh3", ply=fixtures / "sh3.ply"),
        scene_from_folder(
            "lodsdemo", "SSOG Demo", fixtures / "lod", license="by-sa",
            author_username="lod-artist", author_name="Lod Artist", ply=fixtures / "lod-0.ply",
        ),
        FakeScene("plydemo1", "Legacy PLY Demo", "compressed-ply", license="by-nc", ply=fixtures / "sh3.ply", splat_count=300),
        scene_from_folder(
            "oldsog01", "Old SOG Demo", fixtures / "sh3", ply=fixtures / "sh3.ply",
            files={"meta.json": json.dumps(v1_meta).encode()},
        ),
        FakeScene("private1", "Private Demo", "sog", downloadable=False),
        FakeScene("gsafdemo", "Animated Demo", "gsaf", downloadable=False),
        FakeScene(
            "failply1", "Broken PLY Demo", "compressed-ply", ply=fixtures / "sh3.ply", splat_count=300,
            ply_steps=("queued", "processing", "failed"),
        ),
    ]


@dataclass
class FakeSuperSplat:
    base_url: str = FAKE_BASE_URL
    api_key: str | None = FAKE_API_KEY  # None accepts any bearer token
    part_size: int = 5 * 1024 * 1024
    put_delay: float = 0.0
    upload_id: str = "upl_0123456789abcdef01234567"
    splat_id: str = "a1b2c3d4"
    username: str | None = "dummy-user"
    create_status: int = 201
    session_overrides: dict[str, Any] = field(default_factory=dict)
    part_url_overrides: dict[str, Any] = field(default_factory=dict)
    complete_overrides: dict[str, Any] = field(default_factory=dict)
    splat_overrides: dict[str, Any] = field(default_factory=dict)
    # route (spec path template) -> queued responses returned before normal handling
    queued: dict[str, list[HttpResponse]] = field(default_factory=dict)
    # part number -> queued PUT failures
    put_failures: dict[int, list[HttpResponse]] = field(default_factory=dict)
    # -- import ------------------------------------------------------------------
    # Origin of the signed download URLs.
    downloads_base: str = FAKE_DOWNLOADS
    site_url: str = FAKE_SITE
    scenes: dict[str, FakeScene] = field(default_factory=dict)
    # Every API call answers 401, as for a revoked key.
    reject_key: bool = False
    # Download requests answer 429 daily_download_limit_reached.
    daily_limit_reached: bool = False
    # Seconds to wait per 64 KiB served by download(), so tests can cancel mid-file.
    download_delay: float = 0.0
    # .../file answers bytes: null, as the API may when it does not know the size.
    unknown_file_bytes: bool = False

    def __post_init__(self) -> None:
        self.lock = threading.Lock()
        self.requests: list[Recorded] = []
        self.violations: list[str] = []
        self.parts: dict[int, bytes] = {}
        self.session: dict[str, Any] | None = None
        self.completed_parts: list[dict[str, Any]] | None = None
        self.file_requests: list[str] = []
        # (scene id, target) -> [step index, version]: each scene's current download per target
        self.downloads: dict[tuple[str, str], list[int]] = {}
        # Signed URLs handed out by .../file, in order.
        self.minted: list[str] = []
        # (scene id, target) -> ZIP
        self._archives: dict[tuple[str, str], bytes] = {}

    # -- helpers for tests -----------------------------------------------------

    def queue(self, route: str, status: int, body: Any, headers: dict[str, str] | None = None) -> None:
        self.queued.setdefault(route, []).append(_json(status, body, headers))

    def routes(self) -> list[str]:
        return [f"{item.method} {item.route}" for item in self.requests]

    def uploaded_bytes(self) -> bytes:
        return b"".join(self.parts[number] for number in sorted(self.parts))

    def add_scenes(self, scenes: Iterable[FakeScene]) -> FakeSuperSplat:
        for scene in scenes:
            self.scenes[scene.id] = scene
        return self

    def scene_url(self, scene_id: str) -> str:
        return f"{self.site_url}/scene/{scene_id}"

    # -- Transport -------------------------------------------------------------

    def send(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | Iterable[bytes] | None,
        timeout: float,
    ) -> HttpResponse:
        if url.startswith(FAKE_STORAGE + "/"):
            return self._put(method, url, headers, body)
        if not url.startswith(self.base_url + "/"):
            raise TransportError(f"fake transport has no route to {urllib.parse.urlsplit(url).netloc}")
        path = urllib.parse.urlsplit(url).path[len(urllib.parse.urlsplit(self.base_url).path) :]
        for route_method, pattern, template in _ROUTES:
            match = pattern.match(path)
            if route_method == method and match:
                return self._api(method, url, template, match, headers, body)
        return _json(404, {"error": "Resource not found"})

    def _authorized(self, headers: dict[str, str]) -> bool:
        if self.reject_key:
            return False
        token = headers.get("Authorization", "")
        if self.api_key is None:
            return token.startswith("Bearer ") and bool(token[len("Bearer ") :].strip())
        return token == f"Bearer {self.api_key}"

    def _api(self, method, url, template, match, headers, body) -> HttpResponse:  # noqa: ANN001
        payload = json.loads(body) if body else None
        with self.lock:
            self.requests.append(Recorded(method, url, template, dict(headers), payload))
        if not self._authorized(headers):
            return _json(401, {"error": "Unauthorized"})
        if payload is not None:
            problems = spec_check.errors(payload, spec_check.request_schema(method, template))
            if problems:
                self.violations += [f"{method} {template} request: {p}" for p in problems]
                return _json(400, {"error": "Invalid request"})
        queued = self.queued.get(template)
        if queued:
            return queued.pop(0)

        status, value, extra = 200, None, None
        opaque_id = urllib.parse.unquote(match.groupdict().get("id", ""))
        if template == "/v1/me":
            value = {"id": "user-0001", "username": self.username}
        elif template == "/v1/splats/uploads":
            self.session = {
                "id": self.upload_id,
                "status": "created",
                "contentLength": payload.get("contentLength"),
                "sourceFormat": payload["sourceFormat"],
                "title": payload.get("title", "Untitled"),
                "description": payload.get("description", ""),
                "partSize": self.part_size,
                "uploadedParts": [],
                "expiresAt": _TIMESTAMP,
            }
            self.session.update(self.session_overrides)
            status, value = self.create_status, self.session
        elif template.endswith("part-upload-urls"):
            if opaque_id != self.upload_id:
                return _json(404, {"error": "Resource not found"})
            value = {
                "uploadId": self.upload_id,
                "partSize": self.part_size,
                "urls": [
                    {
                        "partNumber": number,
                        "url": f"{FAKE_STORAGE}/{self.upload_id}/part-{number}?signature=dummy",
                        "expiresAt": _TIMESTAMP,
                    }
                    for number in payload["parts"]
                ],
            }
            value.update(self.part_url_overrides)
        elif template.endswith("/complete"):
            if opaque_id != self.upload_id:
                return _json(404, {"error": "Resource not found"})
            self.completed_parts = payload["parts"]
            for part in payload["parts"]:
                if f'"etag-{part["partNumber"]}"' != part["etag"]:
                    return _json(400, {"error": "Invalid part ETag"})
            status = 201
            value = {
                "uploadId": self.upload_id,
                "status": "completed",
                "splatId": self.splat_id,
                "editUrl": f"{self.site_url}/scene/{self.splat_id}/edit",
            }
            value.update(self.complete_overrides)
        elif template == "/v1/splats/{splatId}":
            if opaque_id in self.scenes:
                value = self._scene_json(self.scenes[opaque_id])
            elif opaque_id == self.splat_id:
                value = self._published_json()
            else:
                return _json(404, {"error": "Resource not found"})
        elif template == "/v1/splats/{splatId}/downloads":
            status, value, extra = self._create_download(opaque_id, payload["target"])
        elif template == "/v1/splats/{splatId}/downloads/{target}":
            status, value, extra = self._poll_download(opaque_id, urllib.parse.unquote(match["target"]))
        elif template == "/v1/splats/{splatId}/downloads/{target}/file":
            status, value, extra = self._download_file(opaque_id, urllib.parse.unquote(match["target"]))
        problems = spec_check.errors(value, spec_check.response_schema(method, template, status))
        self.violations += [f"{method} {template} {status} response: {p}" for p in problems]
        return _json(status, value, extra)

    def _published_json(self) -> dict[str, Any]:
        """The splat a publish just created, still processing."""
        value = {
            "id": self.splat_id,
            "title": (self.session or {}).get("title", "Untitled"),
            "description": (self.session or {}).get("description", ""),
            "status": "processing",
            "visibility": "unlisted",
            "format": None,
            "size": 0,
            "viewerUrl": self.scene_url(self.splat_id),
            "createdAt": _TIMESTAMP,
            "updatedAt": _TIMESTAMP,
            "completedAt": None,
            "author": {"username": self.username or "dummy-user", "displayName": "Dummy User"},
            "thumbnails": None,
            "downloadable": False,
            "license": None,
            "content": None,
        }
        value.update(self.splat_overrides)
        return value

    def _scene_json(self, scene: FakeScene) -> dict[str, Any]:
        completed = scene.status == "completed"
        content = None
        if scene.downloadable:
            content = {
                "format": scene.format,
                "version": scene.version,
                "splatCount": scene.splat_count,
            }
            if scene.format == "ssog" and scene.lod_counts:
                content["lodCounts"] = list(scene.lod_counts)
        return {
            "id": scene.id,
            "title": scene.title,
            "description": "",
            "status": scene.status,
            "visibility": "listed",
            "format": scene.format if completed else None,
            "size": 0,
            "viewerUrl": self.scene_url(scene.id),
            "createdAt": _TIMESTAMP,
            "updatedAt": _TIMESTAMP,
            "completedAt": _TIMESTAMP if completed else None,
            "author": {"username": scene.author_username, "displayName": scene.author_name},
            "thumbnails": None,
            "downloadable": scene.downloadable,
            "license": scene.license if scene.downloadable else None,
            "content": content,
        }

    # -- downloads -------------------------------------------------------------

    def _downloadable(self, scene_id: str) -> tuple[FakeScene | None, tuple[int, Any, Any] | None]:
        scene = self.scenes.get(scene_id)
        if scene is None:
            return None, (404, {"error": "splat not found"}, None)
        if not scene.downloadable:
            return None, (404, {"error": "downloadable splat not found"}, None)
        return scene, None

    def _current(self, scene: FakeScene, target: str) -> list[int] | None:
        """The scene's download of `target`, unless it belongs to an older version."""
        entry = self.downloads.get((scene.id, target))
        return entry if entry is not None and entry[1] == scene.version else None

    def _create_download(self, scene_id: str, target: str) -> tuple[int, Any, Any]:
        scene, error = self._downloadable(scene_id)
        if error is not None:
            return error
        if target not in scene.targets:
            return 400, {"error": f"target {target} is not available for this splat"}, None
        with self.lock:
            entry = self._current(scene, target)
            status = 200
            if entry is None or scene.steps(target)[entry[0]] == "failed":
                self.downloads[(scene_id, target)] = [0, scene.version]
                status = 202
        location = f"/api/supersplat/v1/splats/{scene_id}/downloads/{target}"
        return status, self._download_json(scene, target), {"Location": location}

    def _poll_download(self, scene_id: str, target: str) -> tuple[int, Any, Any]:
        scene, error = self._downloadable(scene_id)
        if error is not None:
            return error
        with self.lock:
            entry = self._current(scene, target)
            if entry is None:
                return 404, {"error": "download not found"}, None
            # The job moves on between polls.
            entry[0] = min(entry[0] + 1, len(scene.steps(target)) - 1)
        return 200, self._download_json(scene, target), None

    def _download_file(self, scene_id: str, target: str) -> tuple[int, Any, Any]:
        scene, error = self._downloadable(scene_id)
        if error is not None:
            return error
        with self.lock:
            entry = self._current(scene, target)
        if entry is None:
            return 404, {"error": "download not found"}, None
        if scene.steps(target)[entry[0]] != "ready":
            return 409, {"error": "The download is not ready yet.", "code": "not_ready"}, None
        if self.daily_limit_reached:
            body = {
                "error": DAILY_LIMIT_MESSAGE,
                "code": "daily_download_limit_reached",
                "limit": DAILY_DOWNLOAD_LIMIT,
                "count": DAILY_DOWNLOAD_LIMIT,
                "resetsAt": _RESETS_AT,
            }
            return 429, body, {"Retry-After": "22320"}
        with self.lock:
            url = f"{self.downloads_base}/downloads/{scene.id}/{target}/{len(self.minted):08x}/archive.zip?signature=dummy"
            self.minted.append(url)
        value = {
            "url": url,
            "urlExpiresAt": _TIMESTAMP,
            "bytes": None if self.unknown_file_bytes else len(self._archive(scene, target)),
            "version": entry[1],
        }
        return 200, value, None

    def _download_json(self, scene: FakeScene, target: str) -> dict[str, Any]:
        step, version = self.downloads[(scene.id, target)]
        status = scene.steps(target)[step]
        return {
            "splatId": scene.id,
            "target": target,
            "version": version,
            "status": status,
            "bytes": len(self._archive(scene, target)) if status == "ready" else None,
        }

    def _archive(self, scene: FakeScene, target: str) -> bytes:
        with self.lock:
            key = (scene.id, target)
            if key not in self._archives:
                buffer = io.BytesIO()
                with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
                    if target == "ply":
                        bundle.write(scene.ply, "scene.ply")
                    else:
                        for name, data in scene.published_files().items():
                            if name == "lod-meta.json" and data[:2] != b"\x1f\x8b":
                                data = gzip.compress(data, mtime=0)
                            bundle.writestr(name, data)
                    bundle.writestr("LICENSE.txt", f"{scene.title} by {scene.author_name}, {scene.license}\n")
                self._archives[key] = buffer.getvalue()
            return self._archives[key]

    # -- files (FileSource) ----------------------------------------------------

    def file_response(self, url: str) -> HttpResponse:
        """A download ZIP or scene page, as signed storage or the site would answer."""
        with self.lock:
            self.file_requests.append(url)
        parts = urllib.parse.urlsplit(url)
        site, files = urllib.parse.urlsplit(self.site_url), urllib.parse.urlsplit(self.downloads_base)
        if (parts.scheme, parts.netloc) == (site.scheme, site.netloc):
            page = re.fullmatch(re.escape(site.path) + r"/scene/([^/]+)", parts.path)
            if page and page[1] in self.scenes:
                html = f"<!doctype html><title>{self.scenes[page[1]].title}</title><h1>Mock scene page</h1>"
                return HttpResponse(200, html.encode(), {"content-type": "text/html; charset=utf-8"})
        if (parts.scheme, parts.netloc) != (files.scheme, files.netloc):
            return HttpResponse(404)
        path = parts.path[len(files.path) :]
        archive = re.fullmatch(r"/downloads/([^/]+)/(ply|sog|ssog)/[0-9a-f]+/archive\.zip", path)
        if archive and archive[1] in self.scenes:
            if "signature" not in urllib.parse.parse_qs(parts.query):
                return HttpResponse(403)
            return HttpResponse(200, self._archive(self.scenes[archive[1]], archive[2]), {"content-type": "application/zip"})
        return HttpResponse(404)

    def downloaded_targets(self) -> list[str]:
        """The target of each ZIP fetched, in order."""
        return [
            match[1] if (match := re.search(r"/downloads/[^/]+/(ply|sog|ssog)/", url)) else url
            for url in self.file_requests
        ]

    def download(
        self,
        url: str,
        path: Path,
        limit: int,
        *,
        cancel_event: threading.Event,
        on_bytes: Callable[[int], None],
    ) -> int:
        response = self.file_response(url)
        if response.status != 200:
            raise DownloadHttpError(f"Downloading failed with HTTP {response.status}.", response.status)
        if len(response.body) > limit:
            raise ResponseTooLarge(f"The download is larger than {limit} bytes.")
        with Path(path).open("wb") as out:
            for start in range(0, len(response.body), 65536):
                if cancel_event.is_set():
                    raise UploadCancelled("Import cancelled.")
                if self.download_delay:
                    time.sleep(self.download_delay)
                chunk = response.body[start : start + 65536]
                out.write(chunk)
                on_bytes(len(chunk))
        return len(response.body)

    # -- signed upload storage ---------------------------------------------------

    def _put(self, method, url, headers, body) -> HttpResponse:  # noqa: ANN001
        number = int(re.search(r"/part-(\d+)\?", url).group(1))
        with self.lock:
            self.requests.append(Recorded(method, url, "signed PUT", dict(headers)))
            if method != "PUT":
                self.violations.append(f"signed URL called with {method}")
            if "Authorization" in headers:
                self.violations.append("API key sent to signed storage URL")
            failures = self.put_failures.get(number)
            failure = failures.pop(0) if failures else None
        data = bytearray()
        for chunk in ([body] if isinstance(body, bytes) else body or []):
            data += chunk
            if self.put_delay:
                time.sleep(self.put_delay)
        if failure is not None:
            return failure
        if int(headers.get("Content-Length", "-1")) != len(data):
            self.violations.append(f"part {number} Content-Length mismatch")
        with self.lock:
            self.parts[number] = bytes(data)
        return HttpResponse(200, b"", {"etag": f'"etag-{number}"'})


def _json(status: int, value: Any, headers: dict[str, str] | None = None) -> HttpResponse:
    merged = {"content-type": "application/json"}
    merged.update({key.lower(): val for key, val in (headers or {}).items()})
    return HttpResponse(status, json.dumps(value).encode("utf-8"), merged)


def _read_json(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return json.loads(gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data)
