"""Store (or remove) the PlayCanvas API key in the macOS Keychain from a terminal.

Run it with Blender so the stored key is readable by Blender. The key is read
from stdin (prompted without echo in a terminal), never from arguments:

    Blender --background --factory-startup --quiet --python scripts/store_api_key.py < key.txt
    Blender --background --factory-startup --quiet --python scripts/store_api_key.py -- --clear

Only "stored" or "cleared" is printed.
"""

from __future__ import annotations

import argparse
import getpass
import sys
import threading
from pathlib import Path
from typing import Callable, TextIO

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from supersplat.credentials import (  # noqa: E402
    KEYCHAIN_SERVICE,
    MacKeychainStore,
    SecurityFrameworkBackend,
    normalize_key,
)
from supersplat.errors import CredentialError  # noqa: E402

TIMEOUT_SECONDS = 30.0
TIMEOUT_MESSAGE = (
    f"Timed out waiting for the Keychain. Remove the existing '{KEYCHAIN_SERVICE}' item in "
    "Keychain Access, then run this again."
)


def script_args(argv: list[str]) -> list[str]:
    """Arguments after Blender's `--` separator."""
    return argv[argv.index("--") + 1 :] if "--" in argv else []


def read_key(stdin: TextIO) -> str:
    if stdin.isatty():
        return getpass.getpass("PlayCanvas API key: ")
    return stdin.read()


def run_with_timeout(action: Callable[[], None], timeout: float) -> None:
    """Run a Keychain call on a helper thread so a hidden prompt cannot hang forever."""
    outcome: list[BaseException | None] = []

    def target() -> None:
        try:
            action()
        except BaseException as error:  # noqa: BLE001 - re-raised on the calling thread
            outcome.append(error)
        else:
            outcome.append(None)

    thread = threading.Thread(target=target, daemon=True, name="supersplat-store-key")
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise CredentialError(TIMEOUT_MESSAGE)
    if outcome[0] is not None:
        raise outcome[0]


def run(
    args: list[str],
    store: MacKeychainStore,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    timeout: float = TIMEOUT_SECONDS,
) -> int:
    parser = argparse.ArgumentParser(prog="store_api_key.py", description=__doc__.splitlines()[0])
    parser.add_argument("--clear", action="store_true", help="remove the stored key instead")
    options = parser.parse_args(args)
    try:
        if options.clear:
            run_with_timeout(store.clear, timeout)
            print("cleared", file=stdout)
            return 0
        key = normalize_key(read_key(stdin))

        def replace() -> None:
            # Delete and re-create, so the item is created by Blender.
            store.clear()
            store.save(key)

        run_with_timeout(replace, timeout)
    except CredentialError as error:
        print(f"Error: {error}", file=stderr)
        return 1
    print("stored", file=stdout)
    return 0


def main() -> int:
    if sys.platform != "darwin":
        print("Error: the Keychain helper is for macOS only.", file=sys.stderr)
        return 2
    try:
        import bpy  # noqa: F401
    except ImportError:
        print("Error: run this script with Blender, so the Keychain trusts Blender.", file=sys.stderr)
        return 2
    try:
        store = MacKeychainStore(SecurityFrameworkBackend())
    except OSError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    return run(script_args(sys.argv), store, sys.stdin, sys.stdout, sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
