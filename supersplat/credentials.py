"""API key storage.

macOS: a generic password in the user's default (login) Keychain, written and
read through Security.framework via ctypes, so the key never appears in a
process argument list. Other platforms: a plain-text file in the extension's
user folder, readable only by the user where the OS supports it, as other
Blender add-ons keep their tokens. Without a key file the key is kept in memory
for the current Blender session only. The key is never written to Blender
preferences.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import sys
import threading
import time
import weakref
from pathlib import Path
from typing import Protocol

from .errors import ConfigurationError, CredentialError, UploadCancelled

KEYCHAIN_SERVICE = "SuperSplat Blender Extension"
KEYCHAIN_ACCOUNT = "api-key"

MISSING_KEY_MESSAGE = (
    "No PlayCanvas API key is stored. Add one in Preferences > Add-ons > SuperSplat."
)

# Reading the key can wait on a macOS access prompt, so allow time to answer it.
KEY_READ_TIMEOUT = 120.0
KEY_READ_TIMEOUT_MESSAGE = (
    "Timed out waiting for access to the API key in the Keychain. "
    "Allow Blender to access it when macOS asks, then publish again."
)
KEYCHAIN_WAIT_STATUS = "Waiting for Keychain access..."


def key_read_status(store: CredentialStore) -> str:
    """Status while a session reads the key; a persistent store may wait on a macOS prompt."""
    return KEYCHAIN_WAIT_STATUS if store.persistent else "Reading API key..."


class CredentialStore(Protocol):
    persistent: bool
    location: str

    def load(self) -> str: ...

    def save(self, api_key: str) -> None: ...

    def clear(self) -> None: ...

    def has_key(self) -> bool: ...


class _PendingRead:
    """One store.load() call running on its own daemon thread."""

    def __init__(self, store: CredentialStore) -> None:
        self.done = threading.Event()
        self.value = ""
        self.error: BaseException | None = None
        threading.Thread(
            target=self._run, args=(store,), daemon=True, name="supersplat-key-read"
        ).start()

    def _run(self, store: CredentialStore) -> None:
        try:
            self.value = store.load()
        except BaseException as error:  # noqa: BLE001 - handed to the waiting thread
            self.error = error
        finally:
            self.done.set()


_pending_reads: weakref.WeakKeyDictionary[object, _PendingRead] = weakref.WeakKeyDictionary()
_pending_lock = threading.Lock()


def read_api_key(
    store: CredentialStore,
    *,
    timeout: float = KEY_READ_TIMEOUT,
    cancel_event: threading.Event | None = None,
) -> str:
    """Load the key on a helper thread, giving up after `timeout` seconds.

    A blocked Keychain read cannot be interrupted. After a timeout or cancel the
    read keeps waiting on its prompt, and the next call waits for that same read
    instead of starting a second prompt.
    """
    with _pending_lock:
        pending = _pending_reads.get(store)
        if pending is None or pending.done.is_set():
            pending = _PendingRead(store)
            _pending_reads[store] = pending
    deadline = time.monotonic() + timeout
    while not pending.done.wait(0.05):
        if cancel_event is not None and cancel_event.is_set():
            raise UploadCancelled("Publish cancelled.")
        if time.monotonic() >= deadline:
            raise CredentialError(KEY_READ_TIMEOUT_MESSAGE)
    with _pending_lock:
        if _pending_reads.get(store) is pending:
            del _pending_reads[store]
    if isinstance(pending.error, CredentialError):
        raise pending.error
    if pending.error is not None:
        raise CredentialError(f"Reading the API key failed: {pending.error}") from pending.error
    if not pending.value:
        raise ConfigurationError(MISSING_KEY_MESSAGE)
    return pending.value


def normalize_key(api_key: str) -> str:
    value = api_key.strip()
    if not value:
        raise CredentialError("Enter a PlayCanvas API key to save.")
    if any(ch.isspace() for ch in value):
        raise CredentialError("The API key must not contain spaces or line breaks.")
    return value


class SessionCredentialStore:
    """Keeps the key in process memory; it is gone when Blender quits."""

    persistent = False
    location = "this Blender session only"

    def __init__(self) -> None:
        self._value = ""

    def load(self) -> str:
        return self._value

    def save(self, api_key: str) -> None:
        self._value = normalize_key(api_key)

    def clear(self) -> None:
        self._value = ""

    def has_key(self) -> bool:
        return bool(self._value)


class FileCredentialStore:
    """Keeps the key in a file of its own, replaced whole on every save."""

    persistent = True
    location = "your Blender user folder"

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return ""
        except OSError as error:
            raise CredentialError(f"Could not read the stored API key: {error.strerror or error}.") from error

    def save(self, api_key: str) -> None:
        value = normalize_key(api_key)
        temp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # 0o600 applies on POSIX; on Windows the user profile folder is already private.
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(value)
            os.replace(temp, self.path)
        except OSError as error:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            raise CredentialError(f"Could not store the API key: {error.strerror or error}.") from error

    def clear(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError as error:
            raise CredentialError(f"Could not remove the stored API key: {error.strerror or error}.") from error

    def has_key(self) -> bool:
        try:
            return self.path.stat().st_size > 0
        except OSError:
            return False


class KeychainBackend(Protocol):
    def exists(self, service: str, account: str) -> bool: ...

    def find(self, service: str, account: str) -> str | None: ...

    def store(self, service: str, account: str, secret: str) -> None: ...

    def delete(self, service: str, account: str) -> bool: ...


class MacKeychainStore:
    """Login Keychain storage.

    Backend calls are made without holding the lock: a read can wait on a macOS
    access prompt, and has_key() is called from UI drawing code.
    """

    persistent = True
    location = "macOS Keychain"

    def __init__(
        self,
        backend: KeychainBackend,
        service: str = KEYCHAIN_SERVICE,
        account: str = KEYCHAIN_ACCOUNT,
    ) -> None:
        self.backend = backend
        self.service = service
        self.account = account
        self._lock = threading.Lock()
        self._known: bool | None = None

    def load(self) -> str:
        value = self.backend.find(self.service, self.account) or ""
        with self._lock:
            self._known = bool(value)
        return value

    def save(self, api_key: str) -> None:
        value = normalize_key(api_key)
        self.backend.store(self.service, self.account, value)
        with self._lock:
            self._known = True

    def clear(self) -> None:
        self.backend.delete(self.service, self.account)
        with self._lock:
            self._known = False

    def has_key(self) -> bool:
        """Existence check that never reads the secret, cached after the first call."""
        with self._lock:
            known = self._known
        if known is None:
            known = self.backend.exists(self.service, self.account)
            with self._lock:
                if self._known is None:
                    self._known = known
        return known


def default_credential_store(platform: str | None = None, key_file: Path | None = None) -> CredentialStore:
    """The Keychain on macOS, else `key_file`, else memory for this session only."""
    platform = sys.platform if platform is None else platform
    if platform == "darwin":
        try:
            return MacKeychainStore(SecurityFrameworkBackend())
        except OSError:
            pass
    if key_file is not None:
        return FileCredentialStore(key_file)
    return SessionCredentialStore()


# -- Security.framework --------------------------------------------------------

_ERR_SEC_ITEM_NOT_FOUND = -25300
_STATUS_MESSAGES = {
    -128: "Keychain access was cancelled.",
    -25293: "Keychain access was denied, or the Keychain could not be unlocked.",
    -25308: "The Keychain is locked and cannot be unlocked from here.",
    -25291: "No Keychain is available.",
}


class SecurityFrameworkBackend:
    """Generic-password access through the Security.framework C API."""

    def __init__(self, keychain: ctypes.c_void_p | None = None) -> None:
        path = ctypes.util.find_library("Security")
        cf_path = ctypes.util.find_library("CoreFoundation")
        if not path or not cf_path:
            raise OSError("Security.framework is unavailable")
        self._sec = ctypes.cdll.LoadLibrary(path)
        self._cf = ctypes.cdll.LoadLibrary(cf_path)
        # None selects the default keychain search list (the login Keychain).
        self._keychain = keychain

        u32 = ctypes.c_uint32
        vp = ctypes.c_void_p
        self._sec.SecKeychainFindGenericPassword.argtypes = [
            vp, u32, ctypes.c_char_p, u32, ctypes.c_char_p,
            ctypes.POINTER(u32), ctypes.POINTER(vp), ctypes.POINTER(vp),
        ]
        self._sec.SecKeychainFindGenericPassword.restype = ctypes.c_int32
        self._sec.SecKeychainAddGenericPassword.argtypes = [
            vp, u32, ctypes.c_char_p, u32, ctypes.c_char_p, u32, ctypes.c_char_p,
            ctypes.POINTER(vp),
        ]
        self._sec.SecKeychainAddGenericPassword.restype = ctypes.c_int32
        self._sec.SecKeychainItemModifyAttributesAndData.argtypes = [vp, vp, u32, ctypes.c_char_p]
        self._sec.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
        self._sec.SecKeychainItemDelete.argtypes = [vp]
        self._sec.SecKeychainItemDelete.restype = ctypes.c_int32
        self._sec.SecKeychainItemFreeContent.argtypes = [vp, vp]
        self._sec.SecKeychainItemFreeContent.restype = ctypes.c_int32
        self._cf.CFRelease.argtypes = [vp]
        self._cf.CFRelease.restype = None

    def _find(self, service: str, account: str, *, want_data: bool) -> tuple[ctypes.c_void_p, bytes | None]:
        s = service.encode("utf-8")
        a = account.encode("utf-8")
        length = ctypes.c_uint32(0)
        data = ctypes.c_void_p()
        item = ctypes.c_void_p()
        status = self._sec.SecKeychainFindGenericPassword(
            self._keychain, len(s), s, len(a), a,
            ctypes.byref(length) if want_data else None,
            ctypes.byref(data) if want_data else None,
            ctypes.byref(item),
        )
        if status == _ERR_SEC_ITEM_NOT_FOUND:
            return ctypes.c_void_p(), None
        _check(status)
        secret = None
        if want_data:
            try:
                secret = ctypes.string_at(data, length.value)
            finally:
                self._sec.SecKeychainItemFreeContent(None, data)
        return item, secret

    def exists(self, service: str, account: str) -> bool:
        item, _ = self._find(service, account, want_data=False)
        if item:
            self._cf.CFRelease(item)
            return True
        return False

    def find(self, service: str, account: str) -> str | None:
        item, secret = self._find(service, account, want_data=True)
        if item:
            self._cf.CFRelease(item)
        if secret is None:
            return None
        return secret.decode("utf-8", errors="replace").strip()

    def store(self, service: str, account: str, secret: str) -> None:
        data = secret.encode("utf-8")
        item, _ = self._find(service, account, want_data=False)
        if item:
            try:
                _check(self._sec.SecKeychainItemModifyAttributesAndData(item, None, len(data), data))
            finally:
                self._cf.CFRelease(item)
            return
        s = service.encode("utf-8")
        a = account.encode("utf-8")
        new_item = ctypes.c_void_p()
        _check(
            self._sec.SecKeychainAddGenericPassword(
                self._keychain, len(s), s, len(a), a, len(data), data, ctypes.byref(new_item)
            )
        )
        if new_item:
            self._cf.CFRelease(new_item)

    def delete(self, service: str, account: str) -> bool:
        item, _ = self._find(service, account, want_data=False)
        if not item:
            return False
        try:
            _check(self._sec.SecKeychainItemDelete(item))
        finally:
            self._cf.CFRelease(item)
        return True


def _check(status: int) -> None:
    if status != 0:
        raise CredentialError(_STATUS_MESSAGES.get(status, f"Keychain error {status}."))
