from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

try:
    import numpy  # noqa: F401
except ImportError:
    raise unittest.SkipTest("needs numpy; run the unit tests with Blender's bundled Python") from None

import guards
from fake_api import FAKE_API_KEY, FAKE_BASE_URL, FakeSuperSplat
from fake_keychain import FakeKeychain
from splat_fixtures import blender_import, raw_rows, read_3dgs_ply
from supersplat.api import DEFAULT_BASE_URL
from supersplat.axes import export_transform
from supersplat.credentials import (
    KEYCHAIN_ACCOUNT,
    KEYCHAIN_SERVICE,
    KEYCHAIN_WAIT_STATUS,
    MacKeychainStore,
    SessionCredentialStore,
)
from supersplat.errors import ConfigurationError
from supersplat.ply import property_names
from supersplat.publish import (
    PublishRequest,
    PublishSession,
    resolve_base_url,
)


def setUpModule() -> None:
    guards.block_all()


class PublishSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.staging = Path(self.directory.name)

    def request(self, **overrides) -> PublishRequest:  # noqa: ANN003
        values = dict(
            data=blender_import(raw_rows(64, 3), 3),
            title="Dummy splat",
            description="Neutral dummy description",
            credentials=session_store(FAKE_API_KEY),
            base_url=FAKE_BASE_URL,
            staging_dir=self.staging,
            sh_degree=3,
        )
        values.update(overrides)
        return PublishRequest(**values)

    def test_exports_uploads_and_cleans_up(self) -> None:
        fake = FakeSuperSplat(part_size=4096)
        state = PublishSession(self.request(), transport=fake).run()
        self.assertEqual(state.phase, "complete", state.error)
        self.assertEqual(state.viewer_url, "https://example.invalid/scene/a1b2c3d4")
        self.assertEqual(state.edit_url, "https://example.invalid/scene/a1b2c3d4/edit")
        self.assertEqual(state.account, "dummy-user")
        self.assertEqual(state.progress, 1.0)
        self.assertEqual(fake.violations, [])
        uploaded = fake.uploaded_bytes()
        self.assertTrue(uploaded.startswith(b"ply\nformat binary_little_endian 1.0\nelement vertex 64\n"))
        self.assertEqual(state.file_size, len(uploaded))
        exported = Path(self.directory.name) / "uploaded.ply"
        exported.write_bytes(uploaded)
        parsed = read_3dgs_ply(exported)
        self.assertEqual((parsed.count, parsed.names), (64, property_names(3)))
        exported.unlink()
        self.assertEqual(list(self.staging.iterdir()), [])

    def test_uploaded_file_uses_the_3dgs_axes(self) -> None:
        data = blender_import(raw_rows(3, 0), 0)
        data.position = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
        fake = FakeSuperSplat()
        request = self.request(data=data, sh_degree=0, transform=export_transform())
        state = PublishSession(request, transport=fake).run()
        self.assertEqual(state.phase, "complete", state.error)
        exported = Path(self.directory.name) / "uploaded.ply"
        exported.write_bytes(fake.uploaded_bytes())
        parsed = read_3dgs_ply(exported)
        positions = list(zip(parsed.column("x"), parsed.column("y"), parsed.column("z")))
        self.assertEqual(positions, [(-1.0, 0.0, 0.0), (0.0, 0.0, -1.0), (0.0, -1.0, 0.0)])

    def test_sh_degree_setting_controls_export(self) -> None:
        fake = FakeSuperSplat()
        PublishSession(self.request(sh_degree=0), transport=fake).run()
        self.assertNotIn(b"f_rest_0", fake.uploaded_bytes())

    def test_auth_failure_is_reported_and_staging_removed(self) -> None:
        fake = FakeSuperSplat(api_key="the-right-dummy-key")
        state = PublishSession(self.request(), transport=fake).run()
        self.assertEqual(state.phase, "failed")
        self.assertIn("Unauthorized", state.error)
        self.assertIn("Preferences > Add-ons > SuperSplat", state.error)
        self.assertEqual(list(self.staging.iterdir()), [])

    def test_unreachable_api_fails_gracefully(self) -> None:
        state = PublishSession(
            self.request(base_url="https://unreachable.supersplat.test"),
            transport=FakeSuperSplat(),
            retries=0,
        ).run()
        self.assertEqual(state.phase, "failed")
        self.assertIn("Could not reach SuperSplat", state.error)

    def test_export_failure_is_reported(self) -> None:
        data = blender_import(raw_rows(4, 0), 0)
        data.scale = data.scale[:-1]
        state = PublishSession(self.request(data=data), transport=FakeSuperSplat()).run()
        self.assertEqual(state.phase, "failed")
        self.assertIn("Attribute 'scale'", state.error)

    def test_cancel_while_uploading(self) -> None:
        fake = FakeSuperSplat(part_size=1024, put_delay=0.05)
        session = PublishSession(self.request(), transport=fake)
        session.start()
        for _ in range(200):
            if session.snapshot().phase == "uploading" and fake.parts:
                break
            session.join(0.01)
        session.cancel()
        self.assertTrue(session.join(5))
        state = session.snapshot()
        self.assertEqual(state.phase, "cancelled")
        self.assertIsNone(fake.completed_parts)
        self.assertEqual(list(self.staging.iterdir()), [])

    def test_metadata_is_validated_before_starting(self) -> None:
        with self.assertRaises(ConfigurationError):
            PublishSession(self.request(title="t" * 101), transport=FakeSuperSplat())


class KeychainWaitTests(unittest.TestCase):
    """The key is read by the worker, so a Keychain prompt never blocks the caller.

    Waiting for the prompt and then publishing is covered by KeychainPromptTests
    in tests/blender/test_extension.py.
    """

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.staging = Path(self.directory.name)
        self.backend = FakeKeychain()
        self.backend.items[(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)] = FAKE_API_KEY
        self.backend.block_reads()
        self.addCleanup(self.backend.answer_prompt)
        self.fake = FakeSuperSplat()

    def session(self, key_timeout: float = 30) -> PublishSession:
        request = PublishRequest(
            data=blender_import(raw_rows(16, 1), 1),
            title="Dummy splat",
            description="",
            credentials=MacKeychainStore(self.backend),
            base_url=FAKE_BASE_URL,
            staging_dir=self.staging,
            key_timeout=key_timeout,
        )
        return PublishSession(request, transport=self.fake)

    def test_unanswered_prompt_times_out_before_export_or_network(self) -> None:
        state = self.session(key_timeout=0.2).run()
        self.assertEqual(state.phase, "failed")
        self.assertIn("Timed out waiting for access to the API key", state.error)
        self.assertEqual(self.fake.requests, [])
        self.assertEqual(list(self.staging.iterdir()), [])

    def test_cancel_while_waiting_for_keychain(self) -> None:
        session = self.session()
        session.start()
        self.assertTrue(self.backend.read_started.wait(5))
        session.cancel()
        self.assertTrue(session.join(5))
        self.assertEqual(session.snapshot().phase, "cancelled")
        self.assertEqual(self.fake.requests, [])

    def test_session_store_reports_reading_not_keychain(self) -> None:
        request = PublishRequest(
            data=blender_import(raw_rows(4, 0), 0),
            title="Dummy splat",
            description="",
            credentials=session_store(FAKE_API_KEY),
            base_url=FAKE_BASE_URL,
            staging_dir=self.staging,
        )
        self.assertEqual(PublishSession(request).snapshot().status_text, "Reading API key...")


def session_store(api_key: str) -> SessionCredentialStore:
    store = SessionCredentialStore()
    store.save(api_key)
    return store


class SettingsTests(unittest.TestCase):
    def test_base_url_comes_from_the_environment(self) -> None:
        self.assertEqual(resolve_base_url({}), DEFAULT_BASE_URL)
        env = {"SUPERSPLAT_BASE_URL": " https://env.supersplat.test/ "}
        self.assertEqual(resolve_base_url(env), "https://env.supersplat.test")


if __name__ == "__main__":
    unittest.main()
