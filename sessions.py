"""Follow a background publish or import session from a modal operator."""

from __future__ import annotations

import logging

import bpy

from .runtime import SessionSlot
from .supersplat.errors import SuperSplatError

OFFLINE_MESSAGE = "Online access is off. Turn on Allow Online Access in Preferences > System > Network."

log = logging.getLogger(__name__)


def redraw_sidebars(context: bpy.types.Context) -> None:
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                for region in area.regions:
                    if region.type == "UI":
                        region.tag_redraw()


def error_message(error: Exception) -> str:
    """The message the panel shows; unexpected errors also log their traceback."""
    if isinstance(error, SuperSplatError):
        return str(error)
    log.exception("Unexpected SuperSplat error")
    return f"Unexpected error: {error.__class__.__name__}: {error}"


class SessionOperator:
    """Starts a session in its slot, then follows it with a timer until it ends.

    Subclasses set `slot` and implement `_start_session` (validate on the main thread and
    start the worker) and `_conclude` (act on the final state, report it).
    """

    slot: SessionSlot
    _timer = None
    _session = None

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return not cls.slot.busy()

    def _start_session(self, context: bpy.types.Context):  # noqa: ANN202 - a PublishSession or ImportSession
        raise NotImplementedError

    def _conclude(self, context: bpy.types.Context, session) -> set[str]:  # noqa: ANN001
        raise NotImplementedError

    def _begin(self, context: bpy.types.Context) -> bool:
        try:
            self._session = self._start_session(context)
        except Exception as error:  # noqa: BLE001 - reported to the user; the traceback goes to the log
            self.slot.error = error_message(error)
            self.report({"ERROR"}, self.slot.error)
            redraw_sidebars(context)
            return False
        return True

    def execute(self, context: bpy.types.Context) -> set[str]:
        # Blocking path for scripts and background mode.
        if not self._begin(context):
            return {"CANCELLED"}
        self._session.join()
        return self._finish(context)

    def invoke(self, context: bpy.types.Context, event: bpy.types.Event) -> set[str]:
        if not self._begin(context):
            return {"CANCELLED"}
        wm = context.window_manager
        self._timer = wm.event_timer_add(0.1, window=context.window)
        wm.modal_handler_add(self)
        redraw_sidebars(context)
        return {"RUNNING_MODAL"}

    def modal(self, context: bpy.types.Context, event: bpy.types.Event) -> set[str]:
        active = self._session.snapshot().active
        if event.type == "ESC" and event.value == "PRESS" and active:
            self._session.cancel()
            return {"RUNNING_MODAL"}
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        redraw_sidebars(context)
        if active:
            return {"PASS_THROUGH"}
        self._stop_timer(context)
        return self._finish(context)

    def cancel(self, context: bpy.types.Context) -> None:
        # Blender ended the modal operator (its window closed, or another file loaded):
        # nothing is left to follow the session, so stop it rather than leave it running.
        self._stop_timer(context)
        if self._session is not None:
            self._session.cancel()
        redraw_sidebars(context)

    def _finish(self, context: bpy.types.Context) -> set[str]:
        result = self._conclude(context, self._session)
        redraw_sidebars(context)
        return result

    def _stop_timer(self, context: bpy.types.Context) -> None:
        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None


class CancelOperator:
    """Stops the session running in `slot`."""

    slot: SessionSlot
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return cls.slot.busy()

    def execute(self, context: bpy.types.Context) -> set[str]:
        self.slot.cancel()
        return {"FINISHED"}
