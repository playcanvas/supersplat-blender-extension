"""Import a SuperSplat scene by link: operators, and the objects they create."""

from __future__ import annotations

import math
import shutil
import time

import bpy
import numpy as np
from mathutils import Matrix, Vector

from . import blender_splat
from .properties import looked_up_scene
from .runtime import runtime, staging_dir
from .sessions import OFFLINE_MESSAGE, CancelOperator, SessionOperator, error_message, redraw_sidebars
from .supersplat.api import is_secure_url
from .supersplat.axes import BLENDER_TO_3DGS
from .supersplat.credentials import MISSING_KEY_MESSAGE
from .supersplat.errors import ConfigurationError, SuperSplatError
from .supersplat.publish import resolve_base_url
from .supersplat.scene_import import (
    DecodedScene,
    ImportRequest,
    ImportSession,
    ImportState,
    SceneInfo,
    credit_lines,
    license_name,
    license_url,
    parse_scene_reference,
)

ATTRIBUTION_PROPERTY = "supersplat"
ATTRIBUTIONS_TEXT = "SuperSplat Attributions"
# Staging entries older than this are leftovers of a Blender that ended mid-transfer.
STALE_STAGING_SECONDS = 24 * 60 * 60

_ATTRIBUTIONS_HEADER = (
    "Gaussian splat scenes imported from SuperSplat. Credit their authors as their\n"
    "licenses require when you share renders or exports of them.\n"
)
# Imported splats keep their file coordinates; the object's rotation undoes the
# publish conversion (axes.py), so the scene stands upright and republishes unchanged.
UPRIGHT = Matrix(BLENDER_TO_3DGS).transposed().to_euler("XYZ")
# Frame the middle of the splats so far-off floaters do not shrink the view.
_FRAME_PERCENTILES = (2.0, 98.0)
_FRAME_SAMPLES = 200_000


def _start(context: bpy.types.Context, *, lookup_only: bool) -> ImportSession:
    """Validate on the main thread, then start the background session."""
    if runtime.importing.busy():
        raise SuperSplatError("An import is already running.")
    if not bpy.app.online_access:
        raise SuperSplatError(OFFLINE_MESSAGE)
    settings = context.scene.supersplat
    splat_id = parse_scene_reference(settings.import_link)
    # Existence check only; the worker reads the key, which may wait on a Keychain prompt.
    if not runtime.credentials.has_key():
        raise ConfigurationError(MISSING_KEY_MESSAGE)
    # The level picker lists the levels of the scene last looked up; any other scene starts finest.
    level = int(settings.import_level or 0) if looked_up_scene(settings) is not None else 0
    request = ImportRequest(
        splat_id=splat_id,
        credentials=runtime.credentials,
        base_url=resolve_base_url(),
        staging_dir=staging_dir(),
        level=level,
        environment=settings.import_environment,
        lookup_only=lookup_only,
    )
    session = ImportSession(request, transport=runtime.transport, files=runtime.files)
    runtime.importing.error = ""
    runtime.importing.session = session
    session.start()
    return session


# -- Objects ----------------------------------------------------------------------


def build_scene(context: bpy.types.Context, scene: SceneInfo, decoded: DecodedScene) -> tuple[bpy.types.Object, bpy.types.Object | None]:
    """Create the scene's splat objects at the 3D cursor, credit it, select it and frame it."""
    collection = context.collection or context.scene.collection
    obj = blender_splat.new_splat_object(scene.title, decoded.splats, collection)
    obj.rotation_mode = "XYZ"
    obj.rotation_euler = UPRIGHT
    obj.location = context.scene.cursor.location.copy()
    _record(obj, scene, decoded, "scene")
    environment = None
    if decoded.environment is not None and decoded.environment.count:
        environment = blender_splat.new_splat_object(f"{scene.title} Environment", decoded.environment, collection)
        environment.parent = obj
        environment.matrix_parent_inverse = Matrix.Identity(4)
        _record(environment, scene, decoded, "environment")
    add_credits(scene)

    view_layer = context.view_layer
    for other in view_layer.objects:
        other.select_set(False)
    obj.select_set(True)
    view_layer.objects.active = obj
    view_layer.update()
    frame(context, obj)
    return obj, environment


def _record(obj: bpy.types.Object, scene: SceneInfo, decoded: DecodedScene, part: str) -> None:
    obj[ATTRIBUTION_PROPERTY] = {
        "scene_id": scene.id,
        "title": scene.title,
        "author": scene.author_name,
        "author_username": scene.author_username,
        "license": scene.license,
        "viewer_url": scene.viewer_url,
        "source": decoded.source,
        "level": decoded.level,
        "part": part,
    }


def attribution(obj: bpy.types.Object | None) -> dict[str, object] | None:
    """The SuperSplat credit an imported object carries, as a plain dict."""
    if obj is None:
        return None
    value = obj.get(ATTRIBUTION_PROPERTY)
    if value is None or not hasattr(value, "to_dict"):
        return None
    return value.to_dict()


def add_credits(scene: SceneInfo) -> bpy.types.Text:
    """Add the scene to the SuperSplat Attributions text, once."""
    text = bpy.data.texts.get(ATTRIBUTIONS_TEXT) or bpy.data.texts.new(ATTRIBUTIONS_TEXT)
    body = text.as_string() or _ATTRIBUTIONS_HEADER
    entry = "\n".join(credit_lines(scene))
    if entry not in body:
        body = body.rstrip("\n") + "\n\n" + entry + "\n"
    text.from_string(body)
    return text


def frame(context: bpy.types.Context, obj: bpy.types.Object) -> None:
    """Point every 3D view of the window at the middle of the object's splats."""
    window = context.window or (context.window_manager.windows[0] if context.window_manager.windows else None)
    if window is None:
        return
    points = _world_sample(obj)
    if points is None:
        return
    low, high = np.percentile(points, _FRAME_PERCENTILES, axis=0)
    center = Vector((low + high) / 2)
    radius = max(float(np.linalg.norm(high - low)) / 2, 1e-3)
    for area in window.screen.areas:
        if area.type != "VIEW_3D":
            continue
        space = area.spaces.active
        view = space.region_3d
        if view.view_perspective == "CAMERA":
            view.view_perspective = "PERSP"
        region = next((r for r in area.regions if r.type == "WINDOW"), None)
        aspect = region.width / region.height if region and region.height else 1.0
        # The viewport's lens sees a 72 mm sensor (36 mm at 2x zoom) across its wider
        # side; fit the narrower one.
        half_angle = math.atan(36.0 / space.lens * min(aspect, 1.0 / aspect))
        distance = radius / math.tan(half_angle) * 1.05
        view.view_location = center
        view.view_distance = distance
        space.clip_end = max(space.clip_end, (distance + radius) * 2)
        area.tag_redraw()


def _world_sample(obj: bpy.types.Object) -> np.ndarray | None:
    attribute = obj.data.attributes.get("position")
    count = len(obj.data.points)
    if attribute is None or count == 0:
        return None
    positions = np.empty(count * 3, dtype=np.float32)
    attribute.data.foreach_get("vector", positions)
    positions = positions.reshape(-1, 3)[:: max(1, count // _FRAME_SAMPLES)].astype(np.float64)
    positions = positions[np.isfinite(positions).all(axis=1)]
    if not len(positions):
        return None
    matrix = np.array(obj.matrix_world)
    return positions @ matrix[:3, :3].T + matrix[:3, 3]


# -- Operators --------------------------------------------------------------------


class _ImportOperator(SessionOperator):
    slot = runtime.importing
    lookup_only = False

    def _start_session(self, context: bpy.types.Context) -> ImportSession:
        return _start(context, lookup_only=self.lookup_only)

    def _conclude(self, context: bpy.types.Context, session: ImportSession) -> set[str]:
        state = session.snapshot()
        if state.phase == "decoded":
            state = _build(context, session)
        return _report(self, state)


def _build(context: bpy.types.Context, session: ImportSession) -> ImportState:
    decoded = session.take_result()
    scene = session.snapshot().scene
    if decoded is None or scene is None:
        session.fail("The import finished without a scene to create.")
        return session.snapshot()
    try:
        obj, environment = build_scene(context, scene, decoded)
    except Exception as error:  # noqa: BLE001 - surfaced in the panel, never as a traceback
        session.fail(error_message(error))
        return session.snapshot()
    session.finish(
        object_name=obj.name,
        environment_name=environment.name if environment else "",
        splat_count=decoded.splats.count,
    )
    return session.snapshot()


def _report(operator: bpy.types.Operator, state: ImportState) -> set[str]:
    if state.phase == "found":
        return {"FINISHED"}
    if state.phase == "complete":
        operator.report({"INFO"}, f"Imported {state.scene.credit if state.scene else state.object_name}.")
        if state.note:
            operator.report({"WARNING"}, state.note)
        return {"FINISHED"}
    if state.phase == "cancelled":
        operator.report({"WARNING"}, "Import cancelled.")
        return {"CANCELLED"}
    operator.report({"ERROR"}, state.error or "Import failed.")
    return {"CANCELLED"}


class SUPERSPLAT_OT_import_scene(_ImportOperator, bpy.types.Operator):
    bl_idname = "supersplat.import_scene"
    bl_label = "Import Scene"
    bl_description = (
        "Download the SuperSplat scene a link or scene ID names and add it at the 3D cursor. "
        "Its credit goes in the SuperSplat Attributions text"
    )
    # No REGISTER: redoing would download the scene again.
    bl_options = {"UNDO"}


class SUPERSPLAT_OT_lookup_scene(_ImportOperator, bpy.types.Operator):
    bl_idname = "supersplat.lookup_scene"
    bl_label = "Look Up Scene"
    bl_description = "Show the scene's author, license and levels of detail before importing it"
    bl_options = {"INTERNAL"}
    lookup_only = True


class SUPERSPLAT_OT_cancel_import(CancelOperator, bpy.types.Operator):
    bl_idname = "supersplat.cancel_import"
    bl_label = "Cancel Import"
    bl_description = "Stop the running download. Esc also cancels"
    slot = runtime.importing


class SUPERSPLAT_OT_dismiss_import(bpy.types.Operator):
    bl_idname = "supersplat.dismiss_import"
    bl_label = "Dismiss"
    bl_description = "Clear the last import error"
    bl_options = {"INTERNAL"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        # A found or imported scene stays: the panel still shows it.
        state = runtime.importing.state()
        if state is not None and state.phase in {"failed", "cancelled"}:
            runtime.importing.reset()
        runtime.importing.error = ""
        redraw_sidebars(context)
        return {"FINISHED"}


class SUPERSPLAT_OT_reopen_import(bpy.types.Operator):
    bl_idname = "supersplat.reopen_import"
    bl_label = "Import Again"
    bl_description = "Show the scene's import options again, to import it once more"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        state = runtime.importing.state()
        return state is not None and state.phase == "complete"

    def execute(self, context: bpy.types.Context) -> set[str]:
        runtime.importing.session.reopen()
        redraw_sidebars(context)
        return {"FINISHED"}


def _http_url(url: object) -> str:
    return url if isinstance(url, str) and is_secure_url(url) else ""


class SUPERSPLAT_OT_view_on_supersplat(bpy.types.Operator):
    bl_idname = "supersplat.view_on_supersplat"
    bl_label = "View on SuperSplat"
    bl_description = "Open the active object's SuperSplat scene in a web browser"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        credit = attribution(context.active_object)
        return credit is not None and bool(_http_url(credit.get("viewer_url")))

    def execute(self, context: bpy.types.Context) -> set[str]:
        bpy.ops.wm.url_open(url=_http_url(attribution(context.active_object)["viewer_url"]))
        return {"FINISHED"}


class SUPERSPLAT_OT_view_license(bpy.types.Operator):
    bl_idname = "supersplat.view_license"
    bl_label = "View License"
    bl_description = "Open the license the active object's SuperSplat scene is offered under"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        credit = attribution(context.active_object)
        return credit is not None and bool(license_url(str(credit.get("license", ""))))

    def execute(self, context: bpy.types.Context) -> set[str]:
        bpy.ops.wm.url_open(url=license_url(str(attribution(context.active_object)["license"])))
        return {"FINISHED"}


def credit_summary(credit: dict[str, object]) -> tuple[str, str, str]:
    """(title, "by author", license name) for the panel."""
    username = credit.get("author_username")
    handle = f" (@{username})" if username else ""
    return (
        str(credit.get("title", "")),
        f"by {credit.get('author', '')}{handle}",
        license_name(str(credit.get("license", ""))),
    )


classes = (
    SUPERSPLAT_OT_import_scene,
    SUPERSPLAT_OT_lookup_scene,
    SUPERSPLAT_OT_cancel_import,
    SUPERSPLAT_OT_dismiss_import,
    SUPERSPLAT_OT_reopen_import,
    SUPERSPLAT_OT_view_on_supersplat,
    SUPERSPLAT_OT_view_license,
)


@bpy.app.handlers.persistent
def _forget_import(*_args: object) -> None:
    """Loading a file ends the modal operator that would have built the scene, so drop it."""
    runtime.importing.cancel()
    runtime.importing.session = None
    runtime.importing.error = ""


def _sweep_staging(now: float | None = None) -> None:
    """Remove what imports and publishes left behind when Blender ended mid-transfer.

    Only entries untouched for a day go: another Blender may be using newer ones.
    """
    cutoff = (time.time() if now is None else now) - STALE_STAGING_SECONDS
    try:
        entries = list(staging_dir().iterdir())
    except OSError:
        return
    for entry in entries:
        try:
            if entry.stat().st_mtime > cutoff:
                continue
            if entry.name.startswith("import-") and entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            elif entry.suffix == ".ply" and entry.is_file():
                entry.unlink()
        except OSError:
            continue


def register() -> None:
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.app.handlers.load_pre.append(_forget_import)
    _sweep_staging()


def unregister() -> None:
    if _forget_import in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_forget_import)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
