from __future__ import annotations

import shutil
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import guards
from fake_keychain import FakeKeychain
from supersplat import credentials
from supersplat.credentials import (
    KEYCHAIN_ACCOUNT,
    KEYCHAIN_SERVICE,
    MISSING_KEY_MESSAGE,
    FileCredentialStore,
    MacKeychainStore,
    SessionCredentialStore,
    default_credential_store,
    read_api_key,
)
from supersplat.errors import ConfigurationError, CredentialError, UploadCancelled

DUMMY_KEY = "dummy-api-key-1234"


def setUpModule() -> None:
    guards.block_all()


class MacKeychainStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = FakeKeychain()
        self.store = MacKeychainStore(self.backend)

    def test_save_replace_load_clear(self) -> None:
        self.assertFalse(self.store.has_key())
        self.store.save(f"  {DUMMY_KEY}\n")
        self.assertEqual(self.backend.items, {(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT): DUMMY_KEY})
        self.assertTrue(self.store.has_key())
        self.assertEqual(self.store.load(), DUMMY_KEY)
        self.store.save("dummy-new")
        self.assertEqual(self.store.load(), "dummy-new")
        self.store.clear()
        self.assertFalse(self.store.has_key())
        self.assertEqual(self.store.load(), "")
        self.assertEqual(self.backend.items, {})

    def test_has_key_never_reads_the_secret_and_is_cached(self) -> None:
        self.backend.items[(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)] = DUMMY_KEY
        self.assertTrue(self.store.has_key())
        self.assertTrue(self.store.has_key())
        self.assertEqual(self.backend.calls, ["exists"])

    def test_rejects_empty_or_whitespace_keys(self) -> None:
        for value in ("", "   ", "dummy key", "dummy\nkey"):
            with self.subTest(value=value), self.assertRaises(CredentialError):
                self.store.save(value)
        self.assertNotIn("store", self.backend.calls)

    def test_backend_errors_propagate_as_credential_errors(self) -> None:
        self.backend.fail_with = CredentialError("The Keychain could not be unlocked.")
        with self.assertRaises(CredentialError):
            self.store.save(DUMMY_KEY)
        with self.assertRaises(CredentialError):
            self.store.load()

    def test_missing_key_error(self) -> None:
        with self.assertRaises(ConfigurationError) as raised:
            read_api_key(self.store, timeout=5)
        self.assertEqual(str(raised.exception), MISSING_KEY_MESSAGE)
        self.store.save(DUMMY_KEY)
        self.assertEqual(read_api_key(self.store, timeout=5), DUMMY_KEY)


class KeyReadTests(unittest.TestCase):
    """read_api_key with a Keychain read that is slow or waits on an access prompt."""

    def setUp(self) -> None:
        self.backend = FakeKeychain()
        self.backend.items[(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)] = DUMMY_KEY
        self.store = MacKeychainStore(self.backend)
        self.addCleanup(self.backend.answer_prompt)

    def test_reads_the_key_on_a_helper_thread(self) -> None:
        self.assertEqual(read_api_key(self.store, timeout=5), DUMMY_KEY)
        self.assertEqual(self.backend.read_threads, ["supersplat-key-read"])

    def test_slow_read_within_the_timeout_returns_the_key(self) -> None:
        self.backend.read_delay = 0.3
        self.assertEqual(read_api_key(self.store, timeout=5), DUMMY_KEY)

    def test_blocked_read_times_out_with_guidance(self) -> None:
        self.backend.block_reads()
        with self.assertRaises(CredentialError) as raised:
            read_api_key(self.store, timeout=0.3)
        message = str(raised.exception)
        self.assertIn("Timed out waiting for access to the API key in the Keychain", message)

    def test_has_key_stays_responsive_while_a_read_is_blocked(self) -> None:
        self.backend.block_reads()
        reader = threading.Thread(target=self._read_ignoring_errors, daemon=True)
        reader.start()
        self.assertTrue(self.backend.read_started.wait(5))
        result: list[bool] = []
        check = threading.Thread(target=lambda: result.append(self.store.has_key()), daemon=True)
        check.start()
        check.join(1)
        self.assertFalse(check.is_alive(), "has_key() blocked behind the pending Keychain read")
        self.assertEqual(result, [True])
        self.backend.answer_prompt()
        reader.join(5)

    def test_retry_after_timeout_waits_for_the_same_prompt(self) -> None:
        self.backend.block_reads()
        for _ in range(2):
            with self.assertRaises(CredentialError):
                read_api_key(self.store, timeout=0.1)
        self.backend.answer_prompt()
        self.assertEqual(read_api_key(self.store, timeout=5), DUMMY_KEY)
        self.assertEqual(self.backend.calls.count("find"), 1)

    def test_cancel_while_waiting(self) -> None:
        self.backend.block_reads()
        cancel = threading.Event()
        threading.Timer(0.1, cancel.set).start()
        with self.assertRaises(UploadCancelled):
            read_api_key(self.store, timeout=30, cancel_event=cancel)

    def test_denied_access_is_reported(self) -> None:
        self.backend.fail_with = CredentialError("Keychain access was denied.")
        with self.assertRaises(CredentialError) as raised:
            read_api_key(self.store, timeout=5)
        self.assertEqual(str(raised.exception), "Keychain access was denied.")

    def test_unexpected_backend_errors_become_credential_errors(self) -> None:
        self.backend.fail_with = RuntimeError("boom")  # type: ignore[assignment]
        with self.assertRaises(CredentialError) as raised:
            read_api_key(self.store, timeout=5)
        self.assertIn("boom", str(raised.exception))

    def _read_ignoring_errors(self) -> None:
        try:
            read_api_key(self.store, timeout=5)
        except CredentialError:
            pass


class SessionStoreTests(unittest.TestCase):
    def test_session_store_keeps_key_in_memory_only(self) -> None:
        store = SessionCredentialStore()
        self.assertFalse(store.persistent)
        self.assertIn("session", store.location)
        store.save(DUMMY_KEY)
        self.assertEqual(store.load(), DUMMY_KEY)
        self.assertEqual(SessionCredentialStore().load(), "")  # new session, nothing persisted
        store.clear()
        self.assertFalse(store.has_key())
        with self.assertRaises(ConfigurationError):
            read_api_key(store, timeout=5)


class FileStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        self.path = self.folder / "user" / "api-key"

    def test_key_survives_a_new_store_on_the_same_file(self) -> None:
        store = FileCredentialStore(self.path)
        self.assertTrue(store.persistent)
        self.assertFalse(store.has_key())
        self.assertEqual(store.load(), "")
        store.save(f"  {DUMMY_KEY}\n")
        self.assertTrue(store.has_key())
        self.assertEqual(FileCredentialStore(self.path).load(), DUMMY_KEY)  # Blender restarted
        self.assertEqual(read_api_key(FileCredentialStore(self.path), timeout=5), DUMMY_KEY)

    def test_save_replaces_the_key_and_leaves_no_temp_file(self) -> None:
        store = FileCredentialStore(self.path)
        store.save(DUMMY_KEY)
        store.save("another-dummy-key")
        self.assertEqual(store.load(), "another-dummy-key")
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), ["api-key"])

    @unittest.skipIf(sys.platform == "win32", "POSIX permissions")
    def test_file_is_readable_by_its_owner_only(self) -> None:
        FileCredentialStore(self.path).save(DUMMY_KEY)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_clear_removes_the_file(self) -> None:
        store = FileCredentialStore(self.path)
        store.save(DUMMY_KEY)
        store.clear()
        store.clear()  # clearing twice is fine
        self.assertFalse(self.path.exists())
        self.assertFalse(store.has_key())
        with self.assertRaises(ConfigurationError):
            read_api_key(store, timeout=5)

    def test_invalid_key_is_rejected_before_anything_is_written(self) -> None:
        store = FileCredentialStore(self.path)
        with self.assertRaises(CredentialError):
            store.save("has a space")
        self.assertFalse(self.path.exists())

    def test_unwritable_folder_raises_credential_error(self) -> None:
        blocker = self.folder / "blocker"
        blocker.write_text("not a folder", encoding="utf-8")
        with self.assertRaises(CredentialError):
            FileCredentialStore(blocker / "api-key").save(DUMMY_KEY)


class DefaultStoreTests(unittest.TestCase):
    def test_non_macos_platforms_use_the_key_file_or_a_session(self) -> None:
        for platform in ("linux", "win32"):
            with self.subTest(platform=platform):
                self.assertIsInstance(default_credential_store(platform), SessionCredentialStore)
                store = default_credential_store(platform, key_file=Path("api-key"))
                self.assertIsInstance(store, FileCredentialStore)
                self.assertTrue(store.persistent)

    def test_macos_uses_the_keychain_even_with_a_key_file(self) -> None:
        with patch.object(credentials, "SecurityFrameworkBackend", FakeKeychain):
            for key_file in (None, Path("api-key")):
                with self.subTest(key_file=key_file):
                    store = default_credential_store("darwin", key_file=key_file)
                    self.assertIsInstance(store, MacKeychainStore)
                    self.assertTrue(store.persistent)

    def test_macos_without_security_framework_uses_the_key_file_or_a_session(self) -> None:
        def unavailable() -> None:
            raise OSError("Security.framework is unavailable")

        with patch.object(credentials, "SecurityFrameworkBackend", unavailable):
            self.assertIsInstance(default_credential_store("darwin"), SessionCredentialStore)
            store = default_credential_store("darwin", key_file=Path("api-key"))
            self.assertIsInstance(store, FileCredentialStore)


if __name__ == "__main__":
    unittest.main()
