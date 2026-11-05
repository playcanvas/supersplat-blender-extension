"""Process-wide extension state shared by operators, panels and preferences."""

from __future__ import annotations

from pathlib import Path

import bpy

from .supersplat.api import Transport
from .supersplat.credentials import CredentialStore, default_credential_store
from .supersplat.publish import PublishSession, PublishState
from .supersplat.scene_import import FileSource, ImportSession, ImportState

PACKAGE = __package__


class SessionSlot:
    """The one publish or import session at a time, and the error of one that never started."""

    def __init__(self, *, holding_phases: frozenset[str] = frozenset()) -> None:
        self.session: PublishSession | ImportSession | None = None
        # Error from a session that never started (no selection, bad link, no key, ...).
        self.error = ""
        # Phases after the worker finished that still keep a new session from starting.
        self._holding_phases = holding_phases

    def state(self) -> PublishState | ImportState | None:
        return self.session.snapshot() if self.session is not None else None

    def busy(self) -> bool:
        state = self.state()
        return state is not None and (state.active or state.phase in self._holding_phases)

    def cancel(self) -> None:
        if self.session is not None:
            self.session.cancel()

    def reset(self) -> None:
        if not self.busy():
            self.session = None
        self.error = ""

    def shutdown(self) -> None:
        if self.session is not None:
            self.session.cancel()
            self.session.join(timeout=2.0)
        self.session = None


class Runtime:
    def __init__(self) -> None:
        self.publishing = SessionSlot()
        # A decoded import waits for the main thread to build its objects.
        self.importing = SessionSlot(holding_phases=frozenset({"decoded"}))
        # Key typed into the preferences field, held only until Save or Clear.
        self.key_entry = ""
        # HTTP transport override; None uses urllib. Tests install a fake here.
        self.transport: Transport | None = None
        # Download ZIP fetches from signed URLs; None uses urllib.
        self.files: FileSource | None = None
        self._credentials: CredentialStore | None = None

    @property
    def credentials(self) -> CredentialStore:
        if self._credentials is None:
            self._credentials = default_credential_store(key_file=api_key_file())
        return self._credentials

    @credentials.setter
    def credentials(self, store: CredentialStore) -> None:
        self._credentials = store

    def shutdown(self) -> None:
        self.publishing.shutdown()
        self.importing.shutdown()
        self.key_entry = ""


runtime = Runtime()


def api_key_file() -> Path:
    """Where the API key is kept on platforms without the macOS Keychain."""
    return Path(bpy.utils.extension_path_user(PACKAGE, create=True)) / "api-key"


def staging_dir() -> Path:
    return Path(bpy.utils.extension_path_user(PACKAGE, path="staging", create=True))
