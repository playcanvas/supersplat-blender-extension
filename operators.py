"""Publish, cancel and result operators, and shortcuts into Preferences."""

from __future__ import annotations

import bpy
from bpy.props import EnumProperty

from . import blender_splat
from .runtime import PACKAGE, runtime, staging_dir
from .sessions import OFFLINE_MESSAGE, CancelOperator, SessionOperator, redraw_sidebars
from .supersplat.credentials import MISSING_KEY_MESSAGE
from .supersplat.errors import ConfigurationError, SuperSplatError
from .supersplat.publish import PublishRequest, PublishSession, PublishState, resolve_base_url


def _start_publish(context: bpy.types.Context) -> PublishSession:
    """Validate on the main thread, then start the background session.

    The key itself is read by the session's worker thread: a Keychain read can
    wait on a macOS access prompt, which must not freeze Blender.
    """
    if runtime.publishing.busy():
        raise SuperSplatError("A publish is already running.")
    if not bpy.app.online_access:
        raise SuperSplatError(OFFLINE_MESSAGE)
    obj = blender_splat.find_target(context)
    if obj is None:
        raise SuperSplatError(blender_splat.NO_SELECTION_MESSAGE)
    # Existence check only; it never reads the secret, so it cannot prompt.
    if not runtime.credentials.has_key():
        raise ConfigurationError(MISSING_KEY_MESSAGE)
    settings = context.scene.supersplat
    transform = blender_splat.transform_for(obj, settings.convert_axes)
    data = blender_splat.read_splat(obj, context.evaluated_depsgraph_get())
    request = PublishRequest(
        data=data,
        title=settings.title.strip() or obj.name,
        description=settings.description.strip(),
        credentials=runtime.credentials,
        base_url=resolve_base_url(),
        staging_dir=staging_dir(),
        sh_degree=int(settings.sh_degree),
        transform=transform,
    )
    session = PublishSession(request, transport=runtime.transport)
    runtime.publishing.error = ""
    runtime.publishing.session = session
    session.start()
    return session


def _report_outcome(operator: bpy.types.Operator, state: PublishState) -> set[str]:
    if state.phase == "complete":
        operator.report({"INFO"}, f"Published to SuperSplat: {state.viewer_url or state.edit_url}")
        return {"FINISHED"}
    if state.phase == "cancelled":
        operator.report({"WARNING"}, "Publish cancelled.")
        return {"CANCELLED"}
    operator.report({"ERROR"}, state.error or "Publish failed.")
    return {"CANCELLED"}


class SUPERSPLAT_OT_publish(SessionOperator, bpy.types.Operator):
    bl_idname = "supersplat.publish"
    bl_label = "Publish to SuperSplat"
    bl_description = "Export the selected Gaussian splat as PLY and upload it to SuperSplat as an unlisted scene"
    slot = runtime.publishing

    def _start_session(self, context: bpy.types.Context) -> PublishSession:
        return _start_publish(context)

    def _conclude(self, context: bpy.types.Context, session: PublishSession) -> set[str]:
        return _report_outcome(self, session.snapshot())


class SUPERSPLAT_OT_cancel(CancelOperator, bpy.types.Operator):
    bl_idname = "supersplat.cancel"
    bl_label = "Cancel Publish"
    bl_description = "Stop the running export or upload. Esc also cancels"
    slot = runtime.publishing


class SUPERSPLAT_OT_dismiss(bpy.types.Operator):
    bl_idname = "supersplat.dismiss"
    bl_label = "Publish Another"
    bl_description = "Clear the last publish result or error"
    bl_options = {"INTERNAL"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        runtime.publishing.reset()
        redraw_sidebars(context)
        return {"FINISHED"}


_URL_TARGETS = (
    ("EDITOR", "Editor", "Open the scene in the SuperSplat editor"),
    ("VIEWER", "Viewer", "Open the scene in the SuperSplat viewer"),
)


def _result_url(target: str) -> str:
    state = runtime.publishing.state()
    if state is None or state.phase != "complete":
        return ""
    return state.edit_url if target == "EDITOR" else state.viewer_url


class SUPERSPLAT_OT_open_url(bpy.types.Operator):
    bl_idname = "supersplat.open_url"
    bl_label = "Open in SuperSplat"
    bl_description = "Open the published scene in a web browser"
    bl_options = {"INTERNAL"}

    target: EnumProperty(items=_URL_TARGETS, default="VIEWER")

    def execute(self, context: bpy.types.Context) -> set[str]:
        url = _result_url(self.target)
        if not url:
            self.report({"WARNING"}, "There is no published scene to open.")
            return {"CANCELLED"}
        bpy.ops.wm.url_open(url=url)
        return {"FINISHED"}


class SUPERSPLAT_OT_copy_url(bpy.types.Operator):
    bl_idname = "supersplat.copy_url"
    bl_label = "Copy Link"
    bl_description = "Copy the link to the published scene"
    bl_options = {"INTERNAL"}

    target: EnumProperty(items=_URL_TARGETS, default="VIEWER")

    def execute(self, context: bpy.types.Context) -> set[str]:
        url = _result_url(self.target)
        if not url:
            self.report({"WARNING"}, "There is no published scene to copy.")
            return {"CANCELLED"}
        context.window_manager.clipboard = url
        self.report({"INFO"}, "Link copied to the clipboard.")
        return {"FINISHED"}


class SUPERSPLAT_OT_open_preferences(bpy.types.Operator):
    bl_idname = "supersplat.open_preferences"
    bl_label = "Open Preferences"
    bl_description = "Open the SuperSplat preferences to add a PlayCanvas API key"
    bl_options = {"INTERNAL"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        bpy.ops.preferences.addon_show(module=PACKAGE)
        return {"FINISHED"}


class SUPERSPLAT_OT_open_network_preferences(bpy.types.Operator):
    bl_idname = "supersplat.open_network_preferences"
    bl_label = "Open Network Preferences"
    bl_description = (
        "Open Preferences at System, where Allow Online Access is. "
        "Importing and publishing need it"
    )
    bl_options = {"INTERNAL"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        # Only opens the setting: an extension must never turn online access on itself.
        context.preferences.active_section = "SYSTEM"
        bpy.ops.screen.userpref_show()
        return {"FINISHED"}


classes = (
    SUPERSPLAT_OT_publish,
    SUPERSPLAT_OT_cancel,
    SUPERSPLAT_OT_dismiss,
    SUPERSPLAT_OT_open_url,
    SUPERSPLAT_OT_copy_url,
    SUPERSPLAT_OT_open_preferences,
    SUPERSPLAT_OT_open_network_preferences,
)


def register() -> None:
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister() -> None:
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
