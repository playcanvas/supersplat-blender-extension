"""View3D > Sidebar > SuperSplat panels: setup notices, Import, Attribution and Publish."""

from __future__ import annotations

import bpy

from . import blender_splat
from .importer import (
    SUPERSPLAT_OT_cancel_import,
    SUPERSPLAT_OT_dismiss_import,
    SUPERSPLAT_OT_import_scene,
    SUPERSPLAT_OT_reopen_import,
    SUPERSPLAT_OT_lookup_scene,
    SUPERSPLAT_OT_view_license,
    SUPERSPLAT_OT_view_on_supersplat,
    attribution,
    credit_summary,
)
from .operators import (
    SUPERSPLAT_OT_cancel,
    SUPERSPLAT_OT_copy_url,
    SUPERSPLAT_OT_dismiss,
    SUPERSPLAT_OT_open_network_preferences,
    SUPERSPLAT_OT_open_preferences,
    SUPERSPLAT_OT_open_url,
    SUPERSPLAT_OT_publish,
)
from .properties import looked_up_scene
from .runtime import runtime
from .supersplat.api import format_bytes
from .supersplat.credentials import KEYCHAIN_WAIT_STATUS
from .supersplat.errors import CredentialError, ExportError
from .supersplat.scene_import import format_count, license_name

_FORMAT_LABELS = {
    "sog": "SOG",
    "ssog": "Streamed SOG",
    "compressed-ply": "Legacy PLY",
}


def _wrapped(layout: bpy.types.UILayout, text: str, icon: str = "NONE", width: int = 34) -> None:
    """Draw long messages over several label rows."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    col = layout.column(align=True)
    for index, line in enumerate(lines):
        if icon == "NONE":
            col.label(text=line)
        else:
            col.label(text=line, icon=icon if index == 0 else "BLANK1")


def _has_key() -> bool:
    try:
        return runtime.credentials.has_key()
    except CredentialError:
        return False


def _notice(layout: bpy.types.UILayout, text: str, operator: str, button: str) -> None:
    row = layout.split(factor=0.62)
    row.label(text=text, icon="ERROR")
    row.operator(operator, text=button)


def draw_online_access_notice(layout: bpy.types.UILayout) -> None:
    _notice(layout, "Online access is off", SUPERSPLAT_OT_open_network_preferences.bl_idname, "Network\u2026")


def _draw_session_status(layout: bpy.types.UILayout, state) -> None:  # noqa: ANN001
    """Status, progress bar and detail line of a running publish or import."""
    if state.phase == "authorizing":
        layout.label(text=state.status_text, icon="LOCKED")
        if state.status_text == KEYCHAIN_WAIT_STATUS:
            _wrapped(layout, "If macOS asks, allow Blender to access the API key.", icon="INFO")
    elif state.progress is None:
        layout.label(text=state.status_text, icon="TIME")
    else:
        layout.progress(
            factor=state.progress,
            type="BAR",
            text=f"{state.status_text}  {round(state.progress * 100)}%",
        )
    if state.progress_text:
        layout.label(text=state.progress_text)


class VIEW3D_PT_supersplat_setup(bpy.types.Panel):
    """Add-on-wide problems that stop importing and publishing, shown above every panel."""

    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "SuperSplat"
    bl_label = "Setup"
    bl_options = {"HIDE_HEADER"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return not bpy.app.online_access or not _has_key()

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        if not bpy.app.online_access:
            draw_online_access_notice(layout)
        if not _has_key():
            _notice(layout, "API key not set", SUPERSPLAT_OT_open_preferences.bl_idname, "Set Up\u2026")


def import_collapsed(settings: bpy.types.PropertyGroup, state) -> bool:  # noqa: ANN001
    """After an import, its scene card and options fold away until Import Again.

    Left open they read as if changing them would change the imported object.
    """
    return (
        state is not None
        and state.phase == "complete"
        and not runtime.importing.error
        and looked_up_scene(settings) is not None
    )


class VIEW3D_PT_supersplat_import(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "SuperSplat"
    bl_label = "Import"

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        settings = context.scene.supersplat
        state = runtime.importing.state()
        if state is not None and (state.active or state.phase == "decoded") and not state.lookup_only:
            self._draw_progress(layout, state)
            return

        row = layout.row(align=True)
        row.prop(settings, "import_link", text="", placeholder="Link or ID", icon="URL")
        row.operator(SUPERSPLAT_OT_lookup_scene.bl_idname, text="", icon="VIEWZOOM")
        if state is not None and state.active:
            row = layout.row(align=True)
            row.label(text=state.status_text, icon="TIME")
            row.operator(SUPERSPLAT_OT_cancel_import.bl_idname, text="", icon="CANCEL")

        if import_collapsed(settings, state):
            box = self._draw_imported(layout, state)
            box.operator(SUPERSPLAT_OT_reopen_import.bl_idname, text="Import Again\u2026", icon="IMPORT")
            return

        scene = looked_up_scene(settings)
        if scene is not None:
            self._draw_scene(layout, settings, scene)

        message = runtime.importing.error or (state.error if state and state.phase == "failed" else "")
        if message:
            box = layout.box()
            box.alert = True
            _wrapped(box, message, icon="ERROR")
            box.operator(SUPERSPLAT_OT_dismiss_import.bl_idname, text="Dismiss")
        elif state is not None and state.phase == "cancelled":
            layout.label(text="Import cancelled", icon="INFO")
        elif state is not None and state.phase == "complete":
            self._draw_imported(layout, state)

        row = layout.row()
        row.scale_y = 1.5
        row.operator(SUPERSPLAT_OT_import_scene.bl_idname, icon="IMPORT")

    def _draw_imported(self, layout, state) -> bpy.types.UILayout:
        box = layout.box()
        box.label(text=f"Imported {state.object_name}", icon="CHECKMARK")
        detail = f"{state.splat_count:,} splats"
        if state.environment_name:
            detail += " + environment"
        box.label(text=detail, icon="BLANK1")
        if state.note:
            _wrapped(box, state.note, icon="INFO")
        return box

    def _draw_scene(self, layout, settings, scene) -> None:
        box = layout.box()
        col = box.column(align=True)
        col.label(text=scene.title, icon="OUTLINER_OB_POINTCLOUD")
        col.label(text=f"by {scene.author_name}", icon="USER")
        if scene.downloadable:
            col.label(text=license_name(scene.license), icon="TEXT")
            kind = _FORMAT_LABELS.get(scene.content_format, "PLY download")
            if scene.splat_count:
                kind += f" ({format_count(scene.splat_count)} splats)"
            col.label(text=kind, icon="BLANK1")
        if scene.content_format == "ssog" and not scene.problem():
            if scene.levels > 1:
                box.prop(settings, "import_level")
            box.prop(settings, "import_environment")

    def _draw_progress(self, layout, state) -> None:
        title = state.scene.title if state.scene else state.splat_id
        layout.label(text=f"Importing {title}", icon="IMPORT")
        if state.scene is not None:
            layout.label(text=f"by {state.scene.author_name}", icon="USER")
        _draw_session_status(layout, state)
        if state.note:
            _wrapped(layout, state.note, icon="INFO")
        layout.operator(SUPERSPLAT_OT_cancel_import.bl_idname, icon="CANCEL")


class VIEW3D_PT_supersplat_attribution(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "SuperSplat"
    bl_label = "Attribution"

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return attribution(context.active_object) is not None

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        credit = attribution(context.active_object)
        title, author, license = credit_summary(credit)
        col = layout.column(align=True)
        col.label(text=title, icon="OUTLINER_OB_POINTCLOUD")
        col.label(text=author, icon="USER")
        col.label(text=f"License: {license}", icon="TEXT")
        if credit.get("part") == "environment":
            col.label(text="Scene environment", icon="WORLD")
        col = layout.column(align=True)
        col.scale_y = 1.2
        col.operator(SUPERSPLAT_OT_view_on_supersplat.bl_idname, icon="URL")
        col.operator(SUPERSPLAT_OT_view_license.bl_idname, icon="TEXT")


class VIEW3D_PT_supersplat(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "SuperSplat"
    bl_label = "Publish"

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        state = runtime.publishing.state()
        if state is not None and state.active:
            self._draw_progress(layout, state)
        elif state is not None and state.phase == "complete":
            self._draw_result(layout, state)
        else:
            self._draw_form(context, layout, state)

    def _draw_form(self, context, layout, state) -> None:
        settings = context.scene.supersplat
        box = layout.box()
        obj = blender_splat.find_target(context)
        if obj is None:
            box.label(text="No Gaussian splat selected", icon="INFO")
        else:
            box.label(text=obj.name, icon="OUTLINER_OB_POINTCLOUD")
            degree = blender_splat.available_sh_degree(obj.data)
            box.label(text=f"{len(obj.data.points):,} splats, SH degree {degree}", icon="BLANK1")
            try:
                blender_splat.transform_for(obj, settings.convert_axes)
            except ExportError as error:
                _wrapped(box, str(error), icon="ERROR")

        col = layout.column(align=True)
        col.label(text="Title")
        col.prop(settings, "title", text="", placeholder=obj.name if obj else "Scene title")
        col.label(text="Description")
        col.prop(settings, "description", text="", placeholder="Optional")

        header, body = layout.panel("supersplat_advanced", default_closed=True)
        header.label(text="Advanced")
        if body is not None:
            body.label(text="Format: PLY")
            body.prop(settings, "sh_degree")
            body.prop(settings, "convert_axes")

        message = runtime.publishing.error or (state.error if state and state.phase == "failed" else "")
        if message:
            box = layout.box()
            box.alert = True
            _wrapped(box, message, icon="ERROR")
            box.operator(SUPERSPLAT_OT_dismiss.bl_idname, text="Dismiss")
        elif state is not None and state.phase == "cancelled":
            layout.label(text="Publish cancelled", icon="INFO")

        row = layout.row()
        row.scale_y = 1.5
        row.operator(SUPERSPLAT_OT_publish.bl_idname, icon="EXPORT")
        layout.label(text="Uploads are unlisted", icon="HIDE_ON")

    def _draw_progress(self, layout, state) -> None:
        layout.label(text=f"Publishing {state.title}", icon="EXPORT")
        _draw_session_status(layout, state)
        if state.account:
            layout.label(text=f"Account: {state.account}", icon="USER")
        layout.operator(SUPERSPLAT_OT_cancel.bl_idname, icon="CANCEL")

    def _draw_result(self, layout, state) -> None:
        layout.label(text="Upload complete", icon="CHECKMARK")
        layout.label(text="Processing on SuperSplat", icon="BLANK1")
        box = layout.box()
        box.label(text=state.title, icon="OUTLINER_OB_POINTCLOUD")
        if state.file_size:
            box.label(text=f"{format_bytes(state.file_size)} PLY uploaded", icon="BLANK1")
        box.label(text=state.viewer_url, icon="URL")

        col = layout.column(align=True)
        col.scale_y = 1.3
        col.operator(SUPERSPLAT_OT_open_url.bl_idname, text="Continue in SuperSplat", icon="EDITMODE_HLT").target = "EDITOR"
        col.operator(SUPERSPLAT_OT_open_url.bl_idname, text="Open Viewer", icon="HIDE_OFF").target = "VIEWER"
        row = layout.row(align=True)
        row.operator(SUPERSPLAT_OT_copy_url.bl_idname, icon="COPYDOWN").target = "VIEWER"
        row.operator(SUPERSPLAT_OT_dismiss.bl_idname, icon="LOOP_BACK")


classes = (
    VIEW3D_PT_supersplat_setup,
    VIEW3D_PT_supersplat_import,
    VIEW3D_PT_supersplat_attribution,
    VIEW3D_PT_supersplat,
)


def register() -> None:
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister() -> None:
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
