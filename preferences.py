"""Add-on preferences: PlayCanvas API key entry and storage status."""

from __future__ import annotations

import bpy
from bpy.props import StringProperty

from .panels import draw_online_access_notice
from .runtime import PACKAGE, runtime
from .supersplat.errors import CredentialError

PLAYCANVAS_ACCOUNT_URL = "https://playcanvas.com/account"

# Shown while no key is stored.
KEY_SETUP_STEPS = (
    "1. Click Manage API Keys and sign in to PlayCanvas.",
    "2. Under API Tokens, click Generate Token and copy it.",
    "3. Paste it below and click Save Key.",
)


def _get_key_entry(_self: bpy.types.AddonPreferences) -> str:
    return runtime.key_entry


def _set_key_entry(_self: bpy.types.AddonPreferences, value: str) -> None:
    # Custom get/set keeps the value out of ID properties, so it is never
    # written to userpref.blend.
    runtime.key_entry = value


class SUPERSPLAT_OT_save_api_key(bpy.types.Operator):
    bl_idname = "supersplat.save_api_key"
    bl_label = "Save Key"
    bl_description = "Store the entered PlayCanvas API key"
    bl_options = {"INTERNAL"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        store = runtime.credentials
        try:
            store.save(runtime.key_entry)
        except CredentialError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        runtime.key_entry = ""
        runtime.publishing.error = ""
        self.report({"INFO"}, f"PlayCanvas API key saved ({store.location}).")
        return {"FINISHED"}


class SUPERSPLAT_OT_clear_api_key(bpy.types.Operator):
    bl_idname = "supersplat.clear_api_key"
    bl_label = "Clear Key"
    bl_description = "Remove the stored PlayCanvas API key"
    bl_options = {"INTERNAL"}

    def invoke(self, context: bpy.types.Context, event: bpy.types.Event) -> set[str]:
        return context.window_manager.invoke_confirm(
            self, event, title="Remove the stored API key?", confirm_text="Remove"
        )

    def execute(self, context: bpy.types.Context) -> set[str]:
        runtime.key_entry = ""
        try:
            runtime.credentials.clear()
        except CredentialError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        self.report({"INFO"}, "PlayCanvas API key removed.")
        return {"FINISHED"}


class SuperSplatPreferences(bpy.types.AddonPreferences):
    bl_idname = PACKAGE

    api_key_entry: StringProperty(
        name="PlayCanvas API Key",
        description="Paste a PlayCanvas API token, then press Save Key",
        subtype="PASSWORD",
        get=_get_key_entry,
        set=_set_key_entry,
    )

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        store = runtime.credentials

        box = layout.box()
        box.label(text="PlayCanvas API Key", icon="KEYINGSET")
        try:
            stored = store.has_key()
        except CredentialError as error:
            stored = False
            box.label(text=str(error), icon="ERROR")
        if stored:
            box.label(text=f"Key stored ({store.location})", icon="CHECKMARK")
        else:
            box.label(text="No key stored", icon="BLANK1")
            steps = box.column(align=True)
            for step in KEY_SETUP_STEPS:
                steps.label(text=step, icon="BLANK1")
        if not store.persistent:
            box.label(text="Kept until Blender quits", icon="INFO")

        split = box.split(factor=0.75, align=True)
        split.prop(self, "api_key_entry", text="")
        buttons = split.row(align=True)
        sub = buttons.row(align=True)
        sub.enabled = bool(runtime.key_entry.strip())
        sub.operator(SUPERSPLAT_OT_save_api_key.bl_idname, icon="CHECKMARK")
        sub = buttons.row(align=True)
        sub.enabled = stored
        sub.operator(SUPERSPLAT_OT_clear_api_key.bl_idname, text="", icon="TRASH")
        box.operator(
            "wm.url_open", text="Manage API Keys", icon="URL"
        ).url = PLAYCANVAS_ACCOUNT_URL

        if not bpy.app.online_access:
            draw_online_access_notice(layout.box())


classes = (SUPERSPLAT_OT_save_api_key, SUPERSPLAT_OT_clear_api_key, SuperSplatPreferences)


def register() -> None:
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister() -> None:
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
