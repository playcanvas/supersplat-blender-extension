"""End-to-end check of import-by-link in a real Blender window, against scripts/mock_api.py.

Run it through scripts/e2e_import.sh, which starts the mock server and Blender:

    blender --factory-startup --online-mode --python scripts/e2e_import.py -- \
        --out DIR --mock http://127.0.0.1:8765 [--real-scene local001]

For each case it fills in the sidebar's link field, runs the import operator
the Import Scene button runs (supersplat.import_scene, invoked), and follows it
through a timer while Blender keeps drawing. It then checks the outcome (the
object, its splats, placement and credits, or the error the panel shows) and
saves a screenshot of the window, plus a viewport render of each imported
scene. DIR/results.json records every check; Blender quits when all cases ran.

The extension is installed from dist/ into the user folder BLENDER_USER_RESOURCES
points at, the real Keychain is blocked and the API key is a dummy held in
memory. Python sockets may reach only this machine.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import traceback
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import bpy

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "tests" / "blender"), str(ROOT / "tests")]

import guards  # noqa: E402

guards.allow_only_loopback()

import harness  # noqa: E402

MOCK_KEY = "mock-api-key"
TIMEOUT = 90.0


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="e2e_import.py")
    parser.add_argument("--out", type=Path, required=True)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--mock", help="origin of scripts/mock_api.py")
    target.add_argument("--backend", help="origin of a local SuperSplat API server; needs --key-file and --case")
    parser.add_argument("--real-scene", default="", help="scene ID the mock serves from --scene-dir")
    parser.add_argument("--key-file", type=Path, help="file holding an API key for --backend (never printed)")
    parser.add_argument(
        "--case",
        action="append",
        default=[],
        help="NAME|LINK|EXPECT for --backend; EXPECT is import, error:<text> or wait:<status text> (then Cancel)",
    )
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    args = parser.parse_args(argv)
    if args.backend and not (args.key_file and args.case):
        parser.error("--backend needs --key-file and at least one --case")
    return args


ARGS = _arguments()
ARGS.out.mkdir(parents=True, exist_ok=True)
ORIGIN = ARGS.mock or ARGS.backend
if not ORIGIN.startswith(("http://127.0.0.1:", "http://localhost:")):
    raise SystemExit(f"Refusing to run against {ORIGIN}; expected a server on this machine.")

bpy.context.preferences.view.show_splash = False
harness.install_extension()
RUNTIME = harness.runtime()
importer = sys.modules[f"{harness.PACKAGE}.importer"]
panels = sys.modules[f"{harness.PACKAGE}.panels"]
blender_splat = sys.modules[f"{harness.PACKAGE}.blender_splat"]
scene_import = harness.core("scene_import")
credentials = harness.core("credentials")
if not harness.core("publish").resolve_base_url().startswith(ORIGIN + "/"):
    raise SystemExit(f"SUPERSPLAT_BASE_URL must point at {ORIGIN}.")
API_KEY = ARGS.key_file.expanduser().read_text(encoding="utf-8").strip() if ARGS.backend else MOCK_KEY
RUNTIME.credentials.save(API_KEY)


@dataclass
class Case:
    name: str
    link: str
    title: str = ""
    count: int = 0
    source: str = ""
    environment: int = 0
    cursor: tuple[float, float, float] = (0.0, 0.0, 0.0)
    level: int | None = None  # look the scene up first, then pick this level
    include_environment: bool = True
    error: str = ""  # expected substring of the message the panel shows
    note: str = ""  # expected substring of the fallback note
    mock: dict[str, object] = field(default_factory=dict)
    missing_key: bool = False
    cancel: bool = False
    wait_status: str = ""  # wait until the status shows this, screenshot, then Cancel
    reopen: bool = False  # after the import, press Import Again and screenshot the options


CASES = [
    Case("sog", "https://superspl.at/scene/sogdemo1", "SOG Demo", 300, "sog", cursor=(0.6, -0.4, 0.3)),
    # First of the lodsdemo cases: later ones reuse its download once it is ready, as the server does.
    Case("preparing-and-cancel", "lodsdemo", wait_status="Preparing", cancel=True),
    Case("ssog-environment", "superspl.at/s?id=lodsdemo", "SSOG Demo", 2100, "ssog", environment=50, cursor=(-0.5, 0.5, 0.0), reopen=True),
    Case("ssog-coarsest-level", "lodsdemo", "SSOG Demo", 400, "ssog", level=1, include_environment=False),
    Case("legacy-ply", "plydemo1", "Legacy PLY Demo", 300, "ply", cursor=(0.0, 0.0, 1.0)),
    Case("ply-fallback", "superspl.at/scene/oldsog01", "Old SOG Demo", 300, "ply", note="could not be decoded"),
    Case("progress-and-cancel", "plydemo1", cancel=True, mock={"throttle": 4096}),
    Case("not-downloadable", "private1", error="is not downloadable: its owner has not allowed downloads"),
    Case("animated-gsaf", "gsafdemo", error="animated (GSAF) scene"),
    Case("daily-cap", "plydemo1", error="today's download limit", mock={"dailyLimitReached": True}),
    Case("daily-cap-sog", "sogdemo1", error="today's download limit", mock={"dailyLimitReached": True}),
    Case("failed-download", "failply1", error="could not prepare the PLY download"),
    Case("bad-key", "sogdemo1", error="Unauthorized", mock={"rejectKey": True}),
    Case("missing-key", "sogdemo1", error=credentials.MISSING_KEY_MESSAGE, missing_key=True),
    Case("file-link", "https://files.example.test/scene/v1/meta.json", error="link to a file, not a scene"),
]


def _mock(path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(ARGS.mock + path, data=data, method="POST" if data else "GET")
    if path.startswith("/api/"):
        request.add_header("Authorization", f"Bearer {MOCK_KEY}")
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def _backend_case(spec: str) -> Case:
    name, link, expect = spec.split("|", 2)
    if expect.startswith("error:"):
        return Case(name, link, error=expect[len("error:") :])
    if expect.startswith("wait:"):
        return Case(name, link, wait_status=expect[len("wait:") :], cancel=True)
    return Case(name, link)


if ARGS.backend:
    CASES = [_backend_case(spec) for spec in ARGS.case]

if ARGS.real_scene:
    info = _mock(f"/api/supersplat/v1/splats/{ARGS.real_scene}")
    CASES.insert(
        5,
        Case("real-scene", ARGS.real_scene, info["title"], info["content"]["splatCount"], info["content"]["format"], cursor=(0.0, 0.0, 0.0)),
    )


# -- Window ---------------------------------------------------------------------------


class Window:
    def __init__(self) -> None:
        self.window = bpy.context.window_manager.windows[0]
        self.area = max((a for a in self.window.screen.areas if a.type == "VIEW_3D"), key=lambda a: a.width * a.height)
        self.space = self.area.spaces.active
        self.sidebar = next(r for r in self.area.regions if r.type == "UI")
        self.view = next(r for r in self.area.regions if r.type == "WINDOW")

    def override(self, region: bpy.types.Region | None = None):  # noqa: ANN201
        return bpy.context.temp_override(window=self.window, area=self.area, region=region or self.view)

    def redraw(self) -> None:
        with bpy.context.temp_override(window=self.window):
            bpy.ops.wm.redraw_timer(type="DRAW_WIN_SWAP", iterations=1)

    def screenshot(self, path: Path):  # noqa: ANN201
        """Capture the window once the sidebar has settled (panels resize over a few redraws)."""
        for _ in range(4):
            for area in self.window.screen.areas:
                area.tag_redraw()
            yield 0.15
        self.redraw()
        with bpy.context.temp_override(window=self.window):
            bpy.ops.screen.screenshot(filepath=str(path), check_existing=False)
        _halve(path)

    def render(self, path: Path) -> None:
        scene = bpy.context.scene
        scene.render.filepath = str(path)
        scene.render.resolution_percentage = 50
        with self.override():
            bpy.ops.render.opengl(write_still=True, view_context=True)


def _halve(path: Path) -> None:
    """Retina screenshots are 5K wide; keep them readable but small."""
    image = bpy.data.images.load(str(path))
    try:
        width, height = image.size
        if width > 3000:
            image.scale(width // 2, height // 2)
            image.save()
    finally:
        bpy.data.images.remove(image)


def _shows_text_editor(path: Path) -> bool:
    """The text editor's background is darker than the 3D view's grey, left of the sidebar."""
    import numpy as np

    image = bpy.data.images.load(str(path))
    try:
        width, height = image.size
        pixels = np.empty(width * height * 4, dtype=np.float32)
        image.pixels.foreach_get(pixels)
    finally:
        bpy.data.images.remove(image)
    middle = pixels.reshape(height, width, 4)[height // 4 : height // 2, width // 3 : width // 2, :3]
    return float(np.median(middle)) < 0.18  # the text editor is about 0.13, the 3D view about 0.23


def _clear_scene() -> None:
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for cloud in list(bpy.data.pointclouds):
        if cloud.users == 0:
            bpy.data.pointclouds.remove(cloud)


# -- Checks ---------------------------------------------------------------------------


class Checks:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passed: list[str] = []

    def that(self, condition: bool, label: str) -> None:
        (self.passed if condition else self.failures).append(label)

    def close(self, a, b, label: str, tolerance: float = 1e-4) -> None:  # noqa: ANN001
        self.that(all(abs(x - y) <= tolerance for x, y in zip(a, b)), f"{label}: {tuple(round(v, 4) for v in a)}")


def _message(state) -> str:  # noqa: ANN001
    return RUNTIME.importing.error or (state.error if state is not None and state.phase == "failed" else "")


def check_import(case: Case, state, before: set, window: Window) -> Checks:  # noqa: ANN001
    import numpy as np

    checks = Checks()
    checks.that(state is not None and state.phase == "complete", f"import completed ({_message(state) or state.phase})")
    obj = bpy.data.objects.get(state.object_name) if state else None
    checks.that(obj is not None and obj not in before, "a new object exists")
    if obj is None:
        return checks
    checks.that(blender_splat.is_splat_object(obj), "it is a Gaussian splat point cloud")
    title = case.title or state.scene.title
    checks.that(obj.name == title[:63], f"named after the scene: {obj.name!r}")
    if case.count:
        checks.that(len(obj.data.points) == case.count, f"{len(obj.data.points):,} splats (expected {case.count:,})")
    else:
        checks.that(len(obj.data.points) > 0, f"{len(obj.data.points):,} splats")
    checks.close(obj.location, case.cursor, "at the 3D cursor")
    checks.close(obj.rotation_euler, (-math.pi / 2, 0.0, math.pi), "upright through its rotation")
    checks.that(bpy.context.view_layer.objects.active == obj and obj.select_get(), "selected and active")
    credit = importer.attribution(obj) or {}
    checks.that(credit.get("scene_id") == case.link.rsplit("=", 1)[-1].rsplit("/", 1)[-1], f"records scene ID {credit.get('scene_id')!r}")
    checks.that(bool(credit.get("author")) and bool(credit.get("license")), f"records author {credit.get('author')!r} and license {credit.get('license')!r}")
    checks.that(credit.get("source") == (case.source or credit.get("source")), f"decoded from {credit.get('source')!r}")
    text = bpy.data.texts.get(importer.ATTRIBUTIONS_TEXT)
    checks.that(text is not None and f"“{title}” by {credit.get('author')}" in text.as_string(), "credited in SuperSplat Attributions")
    environment = bpy.data.objects.get(state.environment_name) if state.environment_name else None
    if case.environment:
        checks.that(environment is not None and environment.parent == obj, "environment is a separate object parented to the scene")
        checks.that(environment is not None and len(environment.data.points) == case.environment, "environment splat count")
    else:
        checks.that(environment is None, "no environment object")
    if case.note:
        checks.that(case.note in state.note, f"fallback noted: {state.note!r}")
    checks.that(
        panels.import_collapsed(bpy.context.scene.supersplat, state),
        "the scene card and options are folded away behind Import Again",
    )
    # Zoom to fit: the view looks at the middle of the splats from a distance that shows them.
    positions = np.empty(len(obj.data.points) * 3, dtype=np.float32)
    obj.data.attributes["position"].data.foreach_get("vector", positions)
    matrix = np.array(obj.matrix_world)
    world = positions.reshape(-1, 3) @ matrix[:3, :3].T + matrix[:3, 3]
    low, high = np.percentile(world, (2, 98), axis=0)
    extent = float(np.linalg.norm(high - low))
    view = window.space.region_3d
    offset = float(np.linalg.norm(np.array(view.view_location) - (low + high) / 2))
    checks.that(offset < 0.05 * extent, f"view centred on the splats (off by {offset:.4f})")
    checks.that(0.5 * extent < view.view_distance < 4 * extent, f"view distance {view.view_distance:.3f} fits extent {extent:.3f}")
    return checks


def check_error(case: Case, state, before: set) -> Checks:  # noqa: ANN001
    checks = Checks()
    message = _message(state)
    checks.that(case.error in message, f"panel shows: {message!r}")
    checks.that(set(bpy.data.objects) == before, "no objects were created")
    return checks


# -- Driver ---------------------------------------------------------------------------


def wait_until(condition, timeout: float = TIMEOUT):  # noqa: ANN001, ANN201
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise TimeoutError("timed out waiting for the import")
        yield 0.1


def run_case(index: int, case: Case, window: Window, results: list) -> object:
    _clear_scene()
    settings = bpy.context.scene.supersplat
    if ARGS.mock:
        _mock("/mock/settings", {"dailyLimitReached": False, "rejectKey": False, "throttle": 0, **case.mock})
    if case.missing_key:
        RUNTIME.credentials.clear()
    bpy.context.scene.cursor.location = case.cursor
    RUNTIME.importing.reset()
    settings.import_link = case.link
    settings.import_environment = case.include_environment
    before = set(bpy.data.objects)
    shots = []
    started = time.monotonic()

    if case.level is not None:
        with window.override(window.sidebar):
            bpy.ops.supersplat.lookup_scene("INVOKE_DEFAULT")
        yield from wait_until(lambda: not RUNTIME.importing.busy())
        settings.import_level = str(case.level)
        shot = ARGS.out / f"{index:02d}-{case.name}-picker.png"
        yield from window.screenshot(shot)
        shots.append(shot.name)

    reported = ""
    try:
        with window.override(window.sidebar):
            bpy.ops.supersplat.import_scene("INVOKE_DEFAULT")
    except RuntimeError as error:
        # A button shows an operator's error report; a scripted call raises it.
        reported = str(error).removeprefix("Error: ").strip()
    seen_status = ""
    if case.wait_status:
        def waiting() -> bool:
            nonlocal seen_status
            state = RUNTIME.importing.state()
            seen_status = state.status_text if state else ""
            return case.wait_status in seen_status or not RUNTIME.importing.busy()

        yield from wait_until(waiting)
        yield 1.0
        shot = ARGS.out / f"{index:02d}-{case.name}-waiting.png"
        yield from window.screenshot(shot)
        shots.append(shot.name)
        with window.override(window.sidebar):
            bpy.ops.supersplat.cancel_import()
    elif case.cancel:
        yield from wait_until(lambda: (RUNTIME.importing.state().phase == "downloading" and (RUNTIME.importing.state().progress or 0) > 0.15))
        shot = ARGS.out / f"{index:02d}-{case.name}-progress.png"
        yield from window.screenshot(shot)
        shots.append(shot.name)
        with window.override(window.sidebar):
            bpy.ops.supersplat.cancel_import()
    yield from wait_until(lambda: not RUNTIME.importing.busy())
    yield 0.3  # let the modal operator finish and the sidebar redraw
    state = RUNTIME.importing.state()

    shot = ARGS.out / f"{index:02d}-{case.name}.png"
    yield from window.screenshot(shot)
    shots.append(shot.name)
    if case.cancel:
        checks = Checks()
        if case.wait_status:
            checks.that(case.wait_status in seen_status, f"status showed: {seen_status!r}")
        checks.that(state is not None and state.phase == "cancelled", f"import cancelled ({state.phase if state else None})")
        checks.that(set(bpy.data.objects) == before, "no objects were created")
    elif case.error:
        checks = check_error(case, state, before)
        if reported:
            checks.that(case.error in reported, f"operator reports: {reported!r}")
    else:
        checks = check_import(case, state, before, window)
        render = ARGS.out / f"{index:02d}-{case.name}-render.png"
        window.render(render)
        shots.append(render.name)
        if case.reopen:
            with window.override(window.sidebar):
                bpy.ops.supersplat.reopen_import()
            reopened = RUNTIME.importing.state()
            checks.that(reopened.phase == "found", f"Import Again shows the options ({reopened.phase})")
            checks.that(not panels.import_collapsed(settings, reopened), "the scene card is open again")
            shot = ARGS.out / f"{index:02d}-{case.name}-import-again.png"
            yield from window.screenshot(shot)
            shots.append(shot.name)
    results.append(
        {
            "case": case.name,
            "link": case.link,
            "passed": not checks.failures,
            "failures": checks.failures,
            "checks": checks.passed,
            "message": _message(state) or reported,
            "status": seen_status,
            "note": state.note if state else "",
            "seconds": round(time.monotonic() - started, 2),
            "screenshots": shots,
        }
    )
    print(f"[e2e] {case.name}: {'PASS' if not checks.failures else 'FAIL ' + str(checks.failures)}", flush=True)


def run():  # noqa: ANN201
    results: list[dict] = []
    window = Window()
    try:
        window.space.show_region_ui = True
        window.space.shading.type = "RENDERED"
        window.redraw()
        yield 0.5
        window.sidebar.active_panel_category = "SuperSplat"
        for index, case in enumerate(CASES, 1):
            try:
                yield from run_case(index, case, window, results)
            except Exception:  # noqa: BLE001 - recorded, then the next case runs
                results.append({"case": case.name, "passed": False, "failures": [traceback.format_exc()]})
                print(f"[e2e] {case.name}: ERROR\n{traceback.format_exc()}", flush=True)
            finally:
                if not RUNTIME.credentials.has_key():
                    RUNTIME.credentials.save(API_KEY)
        # The credits every import added, in a text editor.
        credits = bpy.data.texts.get(importer.ATTRIBUTIONS_TEXT)
        (ARGS.out / "attributions.txt").write_text(credits.as_string() if credits else "", encoding="utf-8")
        shot = ARGS.out / f"{len(CASES) + 1:02d}-attributions.png"
        for _ in range(5):
            # A new editor in an area can take a few redraws to show; check the pixels.
            if window.area.type != "TEXT_EDITOR":
                with bpy.context.temp_override(window=window.window, area=window.area):
                    bpy.ops.screen.space_type_set_or_cycle(space_type="TEXT_EDITOR")
            editor = window.area.spaces.active
            editor.text = credits
            editor.font_size = 18
            editor.show_syntax_highlight = False
            editor.top = 0
            yield 1.0
            yield from window.screenshot(shot)
            if _shows_text_editor(shot):
                break
        else:
            results.append({"case": "attributions-screenshot", "passed": False, "failures": ["the text editor never drew"]})
    except Exception:  # noqa: BLE001
        results.append({"case": "driver", "passed": False, "failures": [traceback.format_exc()]})
    summary = {
        "passed": bool(results) and all(r["passed"] for r in results),
        "blender": bpy.app.version_string,
        "cases": results,
    }
    (ARGS.out / "results.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"[e2e] {'PASSED' if summary['passed'] else 'FAILED'}: {sum(r['passed'] for r in results)}/{len(results)} cases", flush=True)
    bpy.ops.wm.quit_blender()


_driver = run()


def _tick() -> float | None:
    try:
        return next(_driver)
    except StopIteration:
        return None


bpy.app.timers.register(_tick, first_interval=1.0)
