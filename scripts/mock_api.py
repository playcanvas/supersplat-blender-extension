#!/usr/bin/env python3
"""Local stand-in for the SuperSplat API and its file hosts, for trying the import in Blender.

    python3 scripts/mock_api.py [--port 8765] [--scene-dir DIR ...] [--daily-limit] [--reject-key]

It puts the test double in tests/fake_api.py behind an HTTP server on
127.0.0.1, so requests and responses are checked against the same OpenAPI
spec as the unit tests. It serves:

    /api/supersplat/v1/...   the v1 API; any API key is accepted unless --reject-key
    /downloads/<id>/...      download ZIPs: a scene's SOG or SSOG files (target sog
                             or ssog) or its scene.ply (target ply), with a license
    /scene/<id>              a placeholder page for each scene's viewerUrl
    /mock/settings           GET the switches, or POST JSON to change them while running:
                             {"dailyLimitReached": true, "rejectKey": false, "throttle": 32768}

`throttle` limits file downloads to that many bytes per second (0 is unlimited),
to watch progress or try Cancel. The extension accepts plain http only for
localhost and 127.0.0.1, so point Blender at the server with

    SUPERSPLAT_BASE_URL=http://127.0.0.1:8765/api/supersplat

The seeded scenes are listed on startup. --scene-dir adds a real scene folder
(one holding meta.json or lod-meta.json), zipped as its sog or ssog download; a
PLY in the folder, if there is one, is served as its PLY download.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from fake_api import FakeScene, FakeSuperSplat, demo_scenes, scene_from_folder  # noqa: E402
from supersplat.api import HttpResponse  # noqa: E402

API_PATH = "/api/supersplat"
_CHUNK = 16 * 1024


class MockServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, fake_for: Callable[[str], FakeSuperSplat], quiet: bool) -> None:
        super().__init__(("127.0.0.1", port), Handler)
        self.origin = f"http://127.0.0.1:{self.server_port}"
        self.fake: FakeSuperSplat = fake_for(self.origin)
        self.throttle = 0
        self.quiet = quiet


class Handler(BaseHTTPRequestHandler):
    server: MockServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def _handle(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        url = self.server.origin + self.path
        path = urllib.parse.urlsplit(self.path).path
        fake = self.server.fake
        throttled = False
        if path == "/mock/settings":
            response = self._settings(method, body)
        elif path.startswith(API_PATH + "/"):
            headers = {_canonical(name): value for name, value in self.headers.items()}
            seen = len(fake.violations)
            response = fake.send(method, url, headers, body, 30.0)
            for violation in fake.violations[seen:]:
                print(f"  spec violation: {violation}", flush=True)
        elif method == "GET":
            response = fake.file_response(url)
            throttled = path.startswith("/downloads/")
        else:
            response = HttpResponse(405)
        self._send(response, throttled)
        if not self.server.quiet:
            shown = path if not urllib.parse.urlsplit(self.path).query else f"{path}?..."
            print(f"{method} {shown} -> {response.status}", flush=True)

    def _settings(self, method: str, body: bytes | None) -> HttpResponse:
        fake = self.server.fake
        if method == "POST":
            try:
                changes = json.loads(body or b"{}")
            except json.JSONDecodeError:
                return HttpResponse(400, b'{"error":"invalid JSON"}', {"content-type": "application/json"})
            if "dailyLimitReached" in changes:
                fake.daily_limit_reached = bool(changes["dailyLimitReached"])
            if "rejectKey" in changes:
                fake.reject_key = bool(changes["rejectKey"])
            if "throttle" in changes:
                self.server.throttle = max(0, int(changes["throttle"]))
            print(f"settings: {changes}", flush=True)
        value = {
            "dailyLimitReached": fake.daily_limit_reached,
            "rejectKey": fake.reject_key,
            "throttle": self.server.throttle,
        }
        return HttpResponse(200, json.dumps(value).encode(), {"content-type": "application/json"})

    def _send(self, response: HttpResponse, throttled: bool) -> None:
        self.send_response(response.status)
        for name, value in response.headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(response.body)))
        self.end_headers()
        rate = self.server.throttle if throttled else 0
        try:
            if not rate:
                self.wfile.write(response.body)
                return
            step = max(1024, min(_CHUNK, rate // 4))
            for start in range(0, len(response.body), step):
                chunk = response.body[start : start + step]
                self.wfile.write(chunk)
                self.wfile.flush()
                time.sleep(len(chunk) / rate)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the client cancelled

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # _handle prints one line per request, without query strings or headers


def _canonical(name: str) -> str:
    return "-".join(part.capitalize() for part in name.split("-"))


def build_fake(origin: str, scene_dirs: list[Path], reject_key: bool, daily_limit: bool) -> FakeSuperSplat:
    fake = FakeSuperSplat(
        base_url=origin + API_PATH,
        api_key=None,
        downloads_base=origin,
        site_url=origin,
        reject_key=reject_key,
        daily_limit_reached=daily_limit,
    )
    fake.add_scenes(demo_scenes())
    for index, folder in enumerate(scene_dirs, 1):
        plys = sorted(folder.glob("*.ply"))
        extra: dict[str, object] = {"ply": plys[0]} if plys else {"ply_steps": ("queued", "failed")}
        fake.add_scenes([scene_from_folder(f"local{index:03d}", folder.name, folder, **extra)])
    return fake


def _describe(scene: FakeScene) -> str:
    if not scene.downloadable:
        return "animated GSAF, not downloadable" if scene.format == "gsaf" else "not downloadable"
    if scene.format == "compressed-ply" and scene.ply_steps[-1] == "failed":
        return "compressed PLY whose PLY download fails"
    if scene.format == "compressed-ply":
        return "compressed PLY, imported through the PLY download"
    if "meta.json" in scene.files:
        return "SOG that fails to decode, so the PLY download is the fallback"
    if scene.format == "ssog":
        return f"SSOG, {len(scene.lod_counts or [])} levels + environment"
    return f"SOG, {scene.splat_count} splats"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, default=8765, help="port on 127.0.0.1 (0 picks a free one)")
    parser.add_argument("--scene-dir", type=Path, action="append", default=[], help="a scene folder to serve too")
    parser.add_argument("--daily-limit", action="store_true", help="answer download requests with the daily cap")
    parser.add_argument("--reject-key", action="store_true", help="answer every API call with 401")
    parser.add_argument("--quiet", action="store_true", help="do not log requests")
    args = parser.parse_args(argv)
    for folder in args.scene_dir:
        if not ((folder / "meta.json").is_file() or (folder / "lod-meta.json").is_file()):
            parser.error(f"{folder} holds neither meta.json nor lod-meta.json")

    server = MockServer(
        args.port,
        lambda origin: build_fake(origin, args.scene_dir, args.reject_key, args.daily_limit),
        args.quiet,
    )
    print(f"Mock SuperSplat API on {server.origin}", flush=True)
    print(f"  SUPERSPLAT_BASE_URL={server.origin}{API_PATH}", flush=True)
    for scene in server.fake.scenes.values():
        print(f"  {scene.id}  {scene.title:<18} {_describe(scene)}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
