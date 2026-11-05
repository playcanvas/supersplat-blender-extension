"""scripts/store_api_key.py with the fake Keychain; the real Keychain stays blocked."""

from __future__ import annotations

import importlib.util
import io
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import guards
from fake_keychain import FakeKeychain
from supersplat import credentials
from supersplat.credentials import KEYCHAIN_ACCOUNT, KEYCHAIN_SERVICE, MacKeychainStore

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "store_api_key.py"
DUMMY_KEY = "dummy-api-key-5678"
ITEM = (KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
helper: types.ModuleType


def setUpModule() -> None:
    global helper
    guards.block_all()
    spec = importlib.util.spec_from_file_location("store_api_key", SCRIPT)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    # The script imported the backend by name; make sure it is the blocked one.
    helper.SecurityFrameworkBackend = credentials.SecurityFrameworkBackend


class Stdin(io.StringIO):
    def __init__(self, text: str, tty: bool = False) -> None:
        super().__init__(text)
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


class StoreApiKeyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = FakeKeychain()
        self.store = MacKeychainStore(self.backend)
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()

    def run_helper(self, args: list[str], stdin: str = "", tty: bool = False) -> int:
        return helper.run(args, self.store, Stdin(stdin, tty), self.stdout, self.stderr, timeout=5)

    def test_stores_the_normalized_key_by_recreating_the_item(self) -> None:
        # Deleting first makes Blender the item's creator, so it reads without a prompt.
        self.backend.items[ITEM] = "dummy-old-key"
        self.assertEqual(self.run_helper([], f"  {DUMMY_KEY}\n"), 0)
        self.assertEqual(self.backend.items, {ITEM: DUMMY_KEY})
        self.assertEqual(self.backend.calls, ["delete", "store"])
        self.assertEqual(self.stdout.getvalue(), "stored\n")
        self.assertEqual(self.stderr.getvalue(), "")

    def test_clear_removes_the_item(self) -> None:
        self.backend.items[ITEM] = DUMMY_KEY
        self.assertEqual(self.run_helper(["--clear"]), 0)
        self.assertEqual(self.backend.items, {})
        self.assertEqual(self.stdout.getvalue(), "cleared\n")

    def test_key_is_never_echoed_printed_or_taken_as_an_argument(self) -> None:
        with patch.object(helper.getpass, "getpass", return_value=DUMMY_KEY) as prompt:
            self.assertEqual(self.run_helper([], tty=True), 0)
        prompt.assert_called_once()
        self.run_helper([], "dummy key with spaces")
        output = self.stdout.getvalue() + self.stderr.getvalue()
        self.assertNotIn(DUMMY_KEY, output)
        self.assertNotIn("dummy key with spaces", output)
        self.backend.items.clear()
        with self.assertRaises(SystemExit), patch("sys.stderr", io.StringIO()):
            self.run_helper([DUMMY_KEY], "")
        self.assertEqual(self.backend.items, {})

    def test_empty_or_invalid_input_is_rejected(self) -> None:
        for text in ("", "  \n", "dummy key with spaces"):
            with self.subTest(text=text):
                self.assertEqual(self.run_helper([], text), 1)
        self.assertEqual(self.backend.items, {})
        self.assertNotIn("store", self.backend.calls)

    def test_a_hidden_prompt_times_out_with_guidance(self) -> None:
        release = threading.Event()
        self.addCleanup(release.set)

        class BlockedStore:
            def clear(self) -> None:
                release.wait(10)

        result = helper.run([], BlockedStore(), Stdin(DUMMY_KEY), self.stdout, self.stderr, timeout=0.2)
        self.assertEqual(result, 1)
        self.assertIn("Remove the existing 'SuperSplat Blender Extension' item", self.stderr.getvalue())


class MainTests(unittest.TestCase):
    def test_runs_only_in_blender_on_macos_and_reads_args_after_the_separator(self) -> None:
        with patch.object(helper.sys, "platform", "linux"), patch("sys.stderr", io.StringIO()):
            self.assertEqual(helper.main(), 2)
        with patch.object(helper.sys, "platform", "darwin"), patch.dict(sys.modules, {"bpy": None}):
            with patch("sys.stderr", io.StringIO()) as stderr:
                self.assertEqual(helper.main(), 2)
        self.assertIn("run this script with Blender", stderr.getvalue())
        backend = FakeKeychain()
        blender = ["Blender", "--background", "--python", "store_api_key.py"]
        for argv, stdin, items in ((blender, DUMMY_KEY, {ITEM: DUMMY_KEY}), (blender + ["--", "--clear"], "", {})):
            with (
                self.subTest(argv=argv),
                patch.object(helper.sys, "platform", "darwin"),
                patch.dict(sys.modules, {"bpy": types.ModuleType("bpy")}),
                patch.object(helper, "SecurityFrameworkBackend", lambda: backend),
                patch.object(helper.sys, "argv", argv),
                patch("sys.stdin", Stdin(stdin)),
                patch("sys.stdout", io.StringIO()),
            ):
                self.assertEqual(helper.main(), 0)
                self.assertEqual(backend.items, items)


if __name__ == "__main__":
    unittest.main()
