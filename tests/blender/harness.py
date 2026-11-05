"""Shared setup for scripts that run inside Blender against the built extension.

Installs the extension zip into the (temporary) user repository, blocks network
sockets and the real Keychain, and wires the fake SuperSplat API in place of
the HTTP transport.

Isolation is fail-closed: the Security.framework backend is replaced before the
add-on registers, credentials always come from a session or fake-Keychain store
holding a dummy key, and assert_isolated() refuses to continue unless the
transport is the in-memory fake on its `.test` base URL with sockets blocked.
"""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
from pathlib import Path

import bpy

TESTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = TESTS_DIR.parent
EXTENSION_ID = "supersplat"
REPO_ID = "user_default"
PACKAGE = f"bl_ext.{REPO_ID}.{EXTENSION_ID}"
CORE_MODULES = ("errors", "api", "ply", "axes", "uploader", "credentials", "publish", "scene_import")

if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))


def built_zip() -> Path:
    zips = sorted((REPO_ROOT / "dist").glob(f"{EXTENSION_ID}-*.zip"))
    if not zips:
        raise SystemExit("Build the extension first: scripts/build.sh")
    return zips[-1]


class IsolationError(SystemExit):
    """Raised (and exits Blender) when a script is not fully isolated."""


def install_extension(zip_path: Path | None = None) -> object:
    """Install the built zip, block the real Keychain, then enable the add-on."""
    zip_path = zip_path or built_zip()
    result = bpy.ops.extensions.package_install_files(
        filepath=str(zip_path), repo=REPO_ID, enable_on_install=False
    )
    if result != {"FINISHED"}:
        raise RuntimeError(f"Installing {zip_path.name} failed: {result}")
    # Import (without registering) so the Keychain backend is replaced before
    # register() or any panel/preferences draw can create a credential store.
    addon = importlib.import_module(PACKAGE)
    _block_real_keychain()
    _import_helpers_against_installed_core()
    bpy.ops.preferences.addon_enable(module=PACKAGE)
    if PACKAGE not in bpy.context.preferences.addons:
        raise RuntimeError(f"Enabling {PACKAGE} failed")
    assert_no_real_keychain()
    return addon


def core(name: str) -> object:
    """A module of the installed extension's core package, e.g. core("ply")."""
    return importlib.import_module(f"{PACKAGE}.supersplat.{name}")


def _import_helpers_against_installed_core() -> None:
    """Import the test helpers so their `supersplat...` imports bind to the installed copy.

    The aliases are removed again afterwards: Blender reports top-level modules
    that an extension leaves in sys.modules as policy violations.
    """
    for name in CORE_MODULES:
        core(name)
    prefix = f"{PACKAGE}.supersplat"
    aliases = {
        "supersplat" + name[len(prefix) :]: module
        for name, module in sys.modules.items()
        if name == prefix or name.startswith(prefix + ".")
    }
    sys.modules.update(aliases)
    try:
        for helper in ("fake_api", "fake_keychain", "splat_fixtures"):
            importlib.import_module(helper)
    finally:
        for name in aliases:
            sys.modules.pop(name, None)


class _NoKeychain:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise IsolationError("Blender test scripts must not touch the real Keychain.")


def _block_real_keychain() -> None:
    credentials = core("credentials")
    credentials.SecurityFrameworkBackend = _NoKeychain
    runtime().credentials = credentials.SessionCredentialStore()


def assert_no_real_keychain() -> None:
    """Refuse to continue unless credentials come from a session or fake Keychain store."""
    from fake_keychain import FakeKeychain

    credentials = core("credentials")
    store = runtime()._credentials
    if credentials.SecurityFrameworkBackend is not _NoKeychain:
        raise IsolationError("Security.framework backend is not blocked.")
    fake_keychain = isinstance(store, credentials.MacKeychainStore) and isinstance(
        store.backend, FakeKeychain
    )
    if not (isinstance(store, credentials.SessionCredentialStore) or fake_keychain):
        raise IsolationError(f"Refusing to run with credential store {type(store).__name__}.")


def assert_isolated() -> None:
    """Refuse to publish unless credentials, HTTP and sockets are all fakes."""
    import socket

    import guards
    from fake_api import FAKE_BASE_URL, FakeSuperSplat

    assert_no_real_keychain()
    if not isinstance(runtime().transport, FakeSuperSplat):
        raise IsolationError("The HTTP transport is not the in-memory fake.")
    if runtime().files is not None and not isinstance(runtime().files, FakeSuperSplat):
        raise IsolationError("Scene files do not come from the in-memory fake.")
    base_url = core("publish").resolve_base_url()
    if base_url != FAKE_BASE_URL:
        raise IsolationError(f"Refusing to publish to {base_url}; expected the fake {FAKE_BASE_URL}.")
    if socket.create_connection is not guards._blocked or socket.getaddrinfo is not guards._blocked:
        raise IsolationError("Network sockets are not blocked.")


def block_network() -> None:
    import guards

    guards.block_network()


def runtime() -> object:
    return importlib.import_module(f"{PACKAGE}.runtime").runtime


def addon_preferences() -> bpy.types.AddonPreferences:
    return bpy.context.preferences.addons[PACKAGE].preferences


def use_fake_api(fake: object) -> None:
    from fake_api import FAKE_BASE_URL

    runtime().transport = fake
    os.environ["SUPERSPLAT_BASE_URL"] = FAKE_BASE_URL


def clear_scene() -> None:
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)


def make_dummy_splat(
    name: str = "Dummy Splat",
    count: int = 64,
    sh_degree: int = 3,
    rows: list[dict[str, float]] | None = None,
) -> tuple[bpy.types.Object, list[dict[str, float]]]:
    """Write a small 3DGS PLY, import it with Blender's PLY importer and select it."""
    from splat_fixtures import raw_rows, write_raw_ply

    rows = rows or raw_rows(count, sh_degree)
    directory = Path(tempfile.mkdtemp(prefix="supersplat-fixture-"))
    path = directory / "dummy_splat.ply"
    write_raw_ply(path, rows, sh_degree)
    before = set(bpy.data.objects)
    result = bpy.ops.wm.ply_import(filepath=str(path))
    if result != {"FINISHED"}:
        raise RuntimeError(f"PLY import failed: {result}")
    (obj,) = set(bpy.data.objects) - before
    obj.name = name
    path.unlink()
    directory.rmdir()
    select_only(obj)
    return obj, rows


def select_only(obj: bpy.types.Object | None) -> None:
    for other in bpy.context.view_layer.objects:
        other.select_set(False)
    bpy.context.view_layer.objects.active = obj
    if obj is not None:
        obj.select_set(True)
