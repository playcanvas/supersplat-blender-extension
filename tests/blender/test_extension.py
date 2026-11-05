"""Headless checks for the built extension inside Blender 5.3.

Run (after scripts/build.sh) with a throwaway user directory:

    BLENDER_USER_RESOURCES="$(mktemp -d)" blender --background --factory-startup \
        --online-mode --python tests/blender/test_extension.py

Without --online-mode the online-access guard is tested instead of the mocked
publish. No network or Keychain access happens in either mode.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import tomllib
import unittest
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness  # noqa: E402

harness.block_network()
ADDON = harness.install_extension()

from fake_api import FAKE_API_KEY, FIXTURES, FakeSuperSplat, demo_scenes  # noqa: E402
from fake_keychain import FakeKeychain  # noqa: E402
from splat_fixtures import raw_rows, read_3dgs_ply  # noqa: E402
credentials = harness.core("credentials")
sog = harness.core("sog")
publish = harness.core("publish")
MISSING_KEY_MESSAGE = harness.core("credentials").MISSING_KEY_MESSAGE
property_names = harness.core("ply").property_names
write_3dgs_ply = harness.core("ply").write_3dgs_ply

scene_import = harness.core("scene_import")
blender_splat = sys.modules[f"{harness.PACKAGE}.blender_splat"]
operators = sys.modules[f"{harness.PACKAGE}.operators"]
importer = sys.modules[f"{harness.PACKAGE}.importer"]
sessions = sys.modules[f"{harness.PACKAGE}.sessions"]


class RegistrationTests(unittest.TestCase):
    def test_no_top_level_module_policy_violations(self) -> None:
        self.assertFalse([name for name in sys.modules if name == "supersplat" or name.startswith("supersplat.")])

    def test_extension_is_enabled_from_the_user_repository(self) -> None:
        self.assertIn(harness.PACKAGE, bpy.context.preferences.addons)
        self.assertTrue(Path(ADDON.__file__).is_relative_to(Path(bpy.utils.user_resource("EXTENSIONS"))))

    def test_operators_panel_and_preferences_register(self) -> None:
        for name in (
            "publish", "cancel", "dismiss", "open_url", "copy_url", "save_api_key", "clear_api_key",
            "import_scene", "lookup_scene", "cancel_import", "dismiss_import", "reopen_import",
            "view_on_supersplat", "view_license",
        ):
            self.assertTrue(hasattr(bpy.ops.supersplat, name), name)
        self.assertEqual(bpy.ops.supersplat.publish.get_rna_type().name, "Publish to SuperSplat")
        self.assertEqual(bpy.ops.supersplat.import_scene.get_rna_type().name, "Import Scene")
        for panel in (
            bpy.types.VIEW3D_PT_supersplat_setup, bpy.types.VIEW3D_PT_supersplat_import,
            bpy.types.VIEW3D_PT_supersplat_attribution, bpy.types.VIEW3D_PT_supersplat,
        ):
            self.assertEqual((panel.bl_space_type, panel.bl_region_type, panel.bl_category), ("VIEW_3D", "UI", "SuperSplat"))
        prefs = harness.addon_preferences()
        self.assertEqual(prefs.bl_rna.properties["api_key_entry"].subtype, "PASSWORD")
        self.assertTrue(hasattr(bpy.context.scene, "supersplat"))

    def test_package_version_matches_the_installed_manifest(self) -> None:
        installed = Path(ADDON.__file__).parent / "blender_manifest.toml"
        manifest = tomllib.loads(installed.read_text(encoding="utf-8"))
        self.assertEqual(sys.modules[f"{harness.PACKAGE}.supersplat"].__version__, manifest["version"])


class PreferencesTests(unittest.TestCase):
    def test_key_entry_is_never_saved_to_userpref(self) -> None:
        runtime = harness.runtime()
        prefs = harness.addon_preferences()
        prefs.api_key_entry = "dummy-entry-never-persisted"
        self.assertEqual(runtime.key_entry, "dummy-entry-never-persisted")
        bpy.ops.wm.save_userpref()
        userpref = Path(bpy.utils.resource_path("USER")) / "config" / "userpref.blend"
        self.assertTrue(userpref.is_file())
        self.assertNotIn(b"dummy-entry-never-persisted", userpref.read_bytes())

        self.assertEqual(bpy.ops.supersplat.save_api_key(), {"FINISHED"})
        self.assertEqual(runtime.key_entry, "")
        self.assertTrue(runtime.credentials.has_key())
        bpy.ops.wm.save_userpref()
        self.assertNotIn(b"dummy-entry-never-persisted", userpref.read_bytes())
        self.assertEqual(bpy.ops.supersplat.clear_api_key(), {"FINISHED"})
        self.assertFalse(runtime.credentials.has_key())

    def test_setup_notice_shows_only_while_the_key_or_online_access_is_missing(self) -> None:
        runtime = harness.runtime()
        runtime.credentials.clear()
        self.assertTrue(bpy.types.VIEW3D_PT_supersplat_setup.poll(bpy.context))
        runtime.credentials.save("dummy-key")
        self.addCleanup(runtime.credentials.clear)
        self.assertEqual(bpy.types.VIEW3D_PT_supersplat_setup.poll(bpy.context), not bpy.app.online_access)

    def test_saving_an_empty_key_is_rejected(self) -> None:
        harness.runtime().key_entry = "   "
        self.assertEqual(run_reporting_error(bpy.ops.supersplat.save_api_key), "Enter a PlayCanvas API key to save.")


class ExportTests(unittest.TestCase):
    def setUp(self) -> None:
        harness.clear_scene()
        self.obj, self.rows = harness.make_dummy_splat(count=64, sh_degree=3)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def test_blender_importer_produces_a_gaussian_splat(self) -> None:
        self.assertTrue(blender_splat.is_splat_object(self.obj))
        self.assertEqual(len(self.obj.data.points), 64)
        self.assertEqual(blender_splat.available_sh_degree(self.obj.data), 3)

    def test_export_round_trips_original_ply_values(self) -> None:
        data = blender_splat.read_splat(self.obj, bpy.context.evaluated_depsgraph_get())
        path = Path(self.directory.name) / "export.ply"
        write_3dgs_ply(path, data)
        header = path.read_bytes().split(b"end_header\n")[0].decode("ascii").splitlines()
        self.assertEqual(header[:3], ["ply", "format binary_little_endian 1.0", "element vertex 64"])
        parsed = read_3dgs_ply(path)
        self.assertEqual(parsed.count, 64)
        self.assertEqual(parsed.names, property_names(3))
        for name in parsed.names:
            for original, written in zip((row[name] for row in self.rows), parsed.column(name)):
                self.assertAlmostEqual(original, written, delta=2e-3, msg=name)

    def test_reimporting_the_export_reproduces_attributes(self) -> None:
        import numpy as np

        data = blender_splat.read_splat(self.obj, bpy.context.evaluated_depsgraph_get())
        path = Path(self.directory.name) / "reimport.ply"
        write_3dgs_ply(path, data)
        before = set(bpy.data.objects)
        bpy.ops.wm.ply_import(filepath=str(path))
        (copy,) = set(bpy.data.objects) - before
        self.assertEqual(
            sorted(a.name for a in copy.data.attributes), sorted(a.name for a in self.obj.data.attributes)
        )
        again = blender_splat.read_splat(copy, bpy.context.evaluated_depsgraph_get())
        for name in ("position", "base", "scale", "rotation"):
            np.testing.assert_allclose(getattr(again, name), getattr(data, name), atol=1e-5, err_msg=name)
        for original, reimported in zip(data.sh, again.sh):
            np.testing.assert_allclose(reimported, original, atol=1e-6)

    def test_degree_can_be_reduced(self) -> None:
        data = blender_splat.read_splat(self.obj, bpy.context.evaluated_depsgraph_get())
        path = Path(self.directory.name) / "rgb.ply"
        write_3dgs_ply(path, data, sh_degree=0)
        self.assertEqual(read_3dgs_ply(path).names, property_names(0))

    def test_non_splat_objects_are_not_targets(self) -> None:
        bpy.ops.mesh.primitive_cube_add(size=0.2)
        cube = bpy.context.active_object
        harness.select_only(cube)
        self.assertIsNone(blender_splat.find_target(bpy.context))
        harness.select_only(self.obj)
        self.assertEqual(blender_splat.find_target(bpy.context), self.obj)


class PublishGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        harness.clear_scene()
        runtime = harness.runtime()
        runtime.publishing.reset()
        runtime.credentials.clear()
        self.fake = FakeSuperSplat(api_key=FAKE_API_KEY, part_size=4096)
        harness.use_fake_api(self.fake)
        harness.assert_isolated()

    def publish_error(self) -> str:
        """Run the operator and return its ERROR report (scripted calls raise it)."""
        return run_reporting_error(bpy.ops.supersplat.publish)

    @unittest.skipIf(bpy.app.online_access, "run without --online-mode to test this guard")
    def test_offline_mode_blocks_publishing(self) -> None:
        harness.make_dummy_splat()
        harness.runtime().credentials.save(FAKE_API_KEY)
        self.assertEqual(self.publish_error(), sessions.OFFLINE_MESSAGE)
        self.assertEqual(harness.runtime().publishing.error, sessions.OFFLINE_MESSAGE)
        self.assertEqual(self.fake.requests, [])

    @unittest.skipUnless(bpy.app.online_access, "needs --online-mode")
    def test_no_selection_fails_gracefully(self) -> None:
        harness.make_dummy_splat()
        harness.select_only(None)
        self.assertEqual(self.publish_error(), blender_splat.NO_SELECTION_MESSAGE)
        self.assertEqual(harness.runtime().publishing.error, blender_splat.NO_SELECTION_MESSAGE)
        self.assertEqual(self.fake.requests, [])

    @unittest.skipUnless(bpy.app.online_access, "needs --online-mode")
    def test_missing_credentials_fail_gracefully(self) -> None:
        harness.make_dummy_splat()
        self.assertEqual(self.publish_error(), MISSING_KEY_MESSAGE)
        self.assertEqual(harness.runtime().publishing.error, MISSING_KEY_MESSAGE)
        self.assertEqual(self.fake.requests, [])

    @unittest.skipUnless(bpy.app.online_access, "needs --online-mode")
    def test_rejected_key_fails_gracefully(self) -> None:
        harness.make_dummy_splat()
        harness.runtime().credentials.save("dummy-wrong-key")
        self.assertIn("Unauthorized", self.publish_error())
        state = harness.runtime().publishing.state()
        self.assertEqual(state.phase, "failed")
        self.assertIn("Unauthorized", state.error)

    @unittest.skipUnless(bpy.app.online_access, "needs --online-mode")
    def test_mocked_publish_completes(self) -> None:
        harness.make_dummy_splat()
        harness.runtime().credentials.save(FAKE_API_KEY)
        bpy.context.scene.supersplat.title = "Dummy splat"
        self.assertEqual(bpy.ops.supersplat.publish(), {"FINISHED"})
        state = harness.runtime().publishing.state()
        self.assertEqual(state.phase, "complete", state.error)
        self.assertTrue(state.viewer_url.startswith("https://example.invalid/"))
        self.assertEqual(self.fake.violations, [])
        self.assertEqual(read_bytes_count(self.fake.uploaded_bytes()), 64)


class KeychainPromptTests(unittest.TestCase):
    """A Keychain read waiting on a macOS prompt must not block Blender's main thread."""

    def setUp(self) -> None:
        harness.clear_scene()
        runtime = harness.runtime()
        runtime.publishing.reset()
        self.backend = FakeKeychain()
        self.backend.items[(credentials.KEYCHAIN_SERVICE, credentials.KEYCHAIN_ACCOUNT)] = FAKE_API_KEY
        self.backend.block_reads()
        self.addCleanup(self.backend.answer_prompt)
        previous = runtime.credentials
        runtime.credentials = credentials.MacKeychainStore(self.backend)
        self.addCleanup(setattr, runtime, "credentials", previous)
        self.fake = FakeSuperSplat(api_key=FAKE_API_KEY, part_size=4096)
        harness.use_fake_api(self.fake)
        harness.assert_isolated()

    @unittest.skipUnless(bpy.app.online_access, "needs --online-mode")
    def test_publish_starts_without_waiting_for_the_keychain(self) -> None:
        harness.make_dummy_splat()
        session = operators._start_publish(bpy.context)
        self.assertTrue(self.backend.read_started.wait(5))
        self.assertNotEqual(self.backend.read_threads, ["MainThread"])
        state = session.snapshot()
        self.assertEqual((state.phase, state.status_text), ("authorizing", credentials.KEYCHAIN_WAIT_STATUS))
        self.assertEqual(self.fake.requests, [])
        self.backend.answer_prompt()
        self.assertTrue(session.join(10))
        self.assertEqual(session.snapshot().phase, "complete", session.snapshot().error)


class AxisConversionTests(unittest.TestCase):
    """Markers at Blender +X, +Y, +Z land on file -X, -Z, -Y after a publish."""

    def setUp(self) -> None:
        harness.clear_scene()
        runtime = harness.runtime()
        runtime.publishing.reset()
        runtime.credentials.clear()
        runtime.credentials.save(FAKE_API_KEY)
        self.addCleanup(runtime.credentials.clear)
        self.fake = FakeSuperSplat(api_key=FAKE_API_KEY, part_size=4096)
        harness.use_fake_api(self.fake)
        harness.assert_isolated()
        rows = raw_rows(3, 1)
        for row, position in zip(rows, ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))):
            row.update(zip(("x", "y", "z"), position))
        self.obj, _ = harness.make_dummy_splat("Markers", rows=rows, sh_degree=1)
        self.settings = bpy.context.scene.supersplat
        self.addCleanup(setattr, self.settings, "convert_axes", True)

    def published_positions(self) -> list[tuple[float, ...]]:
        self.assertEqual(bpy.ops.supersplat.publish(), {"FINISHED"})
        path = Path(tempfile.mkdtemp()) / "uploaded.ply"
        path.write_bytes(self.fake.uploaded_bytes())
        parsed = read_3dgs_ply(path)
        path.unlink()
        path.parent.rmdir()
        return [tuple(round(v, 5) + 0.0 for v in p) for p in zip(parsed.column("x"), parsed.column("y"), parsed.column("z"))]

    @unittest.skipUnless(bpy.app.online_access, "needs --online-mode")
    def test_markers_are_converted_by_default(self) -> None:
        self.assertTrue(self.settings.convert_axes)
        self.assertEqual(self.published_positions(), [(-1.0, 0.0, 0.0), (0.0, 0.0, -1.0), (0.0, -1.0, 0.0)])

    @unittest.skipUnless(bpy.app.online_access, "needs --online-mode")
    def test_object_transform_is_applied_before_conversion(self) -> None:
        self.obj.location = (0.0, 0.0, 5.0)
        self.obj.scale = (2.0, 2.0, 2.0)
        bpy.context.view_layer.update()
        self.assertEqual(self.published_positions(), [(-2.0, -5.0, 0.0), (0.0, -5.0, -2.0), (0.0, -7.0, 0.0)])


class SogImportTests(unittest.TestCase):
    """Decoded SOG/SSOG fixtures against Blender's import of splat-transform's PLY of the same files."""

    FIXTURES = harness.TESTS_DIR / "fixtures" / "sog"

    def setUp(self) -> None:
        harness.clear_scene()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def import_ply(self, name: str) -> bpy.types.Object:
        before = set(bpy.data.objects)
        self.assertEqual(bpy.ops.wm.ply_import(filepath=str(self.FIXTURES / name)), {"FINISHED"})
        (obj,) = set(bpy.data.objects) - before
        return obj

    def assert_same_splats(self, ours: bpy.types.Object, reference: bpy.types.Object) -> None:
        import numpy as np

        def layout(obj: bpy.types.Object) -> dict[str, tuple[str, str]]:
            return {a.name: (a.data_type, a.domain) for a in obj.data.attributes}

        self.assertTrue(blender_splat.is_splat_object(ours))
        self.assertEqual(layout(ours), layout(reference))
        depsgraph = bpy.context.evaluated_depsgraph_get()
        a, b = (blender_splat.read_splat(obj, depsgraph) for obj in (ours, reference))
        self.assertEqual(a.count, b.count)
        # Exact apart from float32 rounding in the PLY path's logit/sigmoid, exp and normalization.
        for name in ("position", "base", "scale", "rotation"):
            np.testing.assert_allclose(getattr(a, name), getattr(b, name), rtol=1e-6, atol=1e-6, err_msg=name)
        for index, (x, y) in enumerate(zip(a.sh, b.sh)):
            np.testing.assert_array_equal(x, y, f"radiance:sh_{index}")

    def test_sog_matches_the_ply_import(self) -> None:
        for name in ("sh3", "sh0"):
            with self.subTest(name=name):
                scene = sog.load_scene(self.FIXTURES / name)
                obj = blender_splat.new_splat_object(name, scene.splats)
                self.assertEqual(obj.name, name)
                self.assertIn(obj, list(bpy.context.scene.objects))
                self.assert_same_splats(obj, self.import_ply(f"{name}.ply"))

    def test_ssog_level_and_environment_match_the_ply_import(self) -> None:
        scene = sog.load_scene(self.FIXTURES / "lod")
        self.assert_same_splats(blender_splat.new_splat_object("level 0", scene.splats), self.import_ply("lod-0.ply"))
        environment = blender_splat.new_splat_object("environment", scene.environment)
        self.assert_same_splats(environment, self.import_ply("lod-env.ply"))
        coarse = sog.load_scene(self.FIXTURES / "lod", level=1, environment=False)
        self.assert_same_splats(blender_splat.new_splat_object("level 1", coarse.splats), self.import_ply("lod-1.ply"))

    def test_decoded_scene_exports_the_same_ply_as_splat_transform(self) -> None:
        obj = blender_splat.new_splat_object("sh3", sog.load_scene(self.FIXTURES / "sh3").splats)
        # Unlike an operator, a script must resync the view layer before walking its objects.
        bpy.context.view_layer.update()
        harness.select_only(obj)
        self.assertEqual(blender_splat.find_target(bpy.context), obj)
        path = Path(self.directory.name) / "republish.ply"
        write_3dgs_ply(path, blender_splat.read_splat(obj, bpy.context.evaluated_depsgraph_get()))
        ours, theirs = read_3dgs_ply(path), read_3dgs_ply(self.FIXTURES / "sh3.ply")
        self.assertEqual(sorted(ours.names), sorted(theirs.names))
        for name in theirs.names:
            for a, b in zip(ours.column(name), theirs.column(name)):
                self.assertAlmostEqual(a, b, delta=1e-4, msg=name)

    def test_empty_scene_is_rejected(self) -> None:
        empty = harness.core("ply").SplatData(0, [], [], [], [])
        with self.assertRaises(harness.core("errors").DecodeError):
            blender_splat.new_splat_object("empty", empty)
        self.assertEqual(len(bpy.data.objects), 0)


class StagingSweepTests(unittest.TestCase):
    def test_sweep_removes_only_day_old_import_folders_and_publish_files(self) -> None:
        staging = sys.modules[f"{harness.PACKAGE}.runtime"].staging_dir()
        made = {
            "import-old": True, "import-new": True,  # import folders
            "old.ply": False, "new.ply": False,  # publish exports
            "keep-old.txt": False,  # anything else is left alone
        }
        for name, is_dir in made.items():
            path = staging / name
            path.mkdir() if is_dir else path.write_bytes(b"x")
            self.addCleanup(lambda p=path: (os.path.isdir(p) and os.rmdir(p)) or (os.path.isfile(p) and os.remove(p)))
        day_old = time.time() - importer.STALE_STAGING_SECONDS - 60
        for name in ("import-old", "old.ply", "keep-old.txt"):
            os.utime(staging / name, (day_old, day_old))

        importer._sweep_staging()

        self.assertEqual(
            sorted(p.name for p in staging.iterdir() if p.name in made),
            ["import-new", "keep-old.txt", "new.ply"],
        )


class ImportTests(unittest.TestCase):
    """Import by link through the operator the Import Scene button runs, against the fake API."""

    def setUp(self) -> None:
        harness.clear_scene()
        text = bpy.data.texts.get(importer.ATTRIBUTIONS_TEXT)
        if text is not None:
            bpy.data.texts.remove(text)
        runtime = harness.runtime()
        runtime.importing.reset()
        runtime.credentials.clear()
        runtime.credentials.save(FAKE_API_KEY)
        self.addCleanup(runtime.credentials.clear)
        self.fake = FakeSuperSplat(api_key=FAKE_API_KEY, part_size=1 << 20).add_scenes(demo_scenes())
        harness.use_fake_api(self.fake)
        runtime.files = self.fake
        self.addCleanup(setattr, runtime, "files", None)
        harness.assert_isolated()
        self.addCleanup(setattr, scene_import, "POLL_INTERVAL", scene_import.POLL_INTERVAL)
        scene_import.POLL_INTERVAL = 0.0
        self.settings = bpy.context.scene.supersplat
        self.cursor = bpy.context.scene.cursor
        self.addCleanup(reset_import_settings)
        self.addCleanup(runtime.importing.reset)

    def imported(self, link: str) -> bpy.types.Object:
        self.settings.import_link = link
        self.assertEqual(bpy.ops.supersplat.import_scene(), {"FINISHED"})
        state = harness.runtime().importing.state()
        self.assertEqual(state.phase, "complete", state.error)
        self.assertEqual(self.fake.violations, [])
        return bpy.data.objects[state.object_name]

    def import_error(self, link: str) -> str:
        self.settings.import_link = link
        before = set(bpy.data.objects)
        message = run_reporting_error(bpy.ops.supersplat.import_scene)
        self.assertEqual(set(bpy.data.objects), before)
        runtime = harness.runtime()
        state = runtime.importing.state()
        self.assertEqual(runtime.importing.error or state.error, message)
        return message

    @unittest.skipUnless(bpy.app.online_access, "needs --online-mode")
    def test_sog_arrives_upright_at_the_cursor_named_selected_and_credited(self) -> None:
        self.cursor.location = (1.0, 2.0, 3.0)
        obj = self.imported("https://superspl.at/scene/sogdemo1")
        self.assertTrue(blender_splat.is_splat_object(obj))
        self.assertEqual((obj.name, len(obj.data.points)), ("SOG Demo", 300))
        self.assertEqual(tuple(obj.location), (1.0, 2.0, 3.0))
        self.assertEqual([round(v, 6) for v in obj.rotation_euler], [round(-1.5707963, 6), 0.0, round(3.1415927, 6)])
        self.assertEqual((bpy.context.view_layer.objects.active, obj.select_get()), (obj, True))
        credit = importer.attribution(obj)
        self.assertEqual(
            {key: credit[key] for key in ("scene_id", "title", "author", "author_username", "license", "source", "part")},
            {
                "scene_id": "sogdemo1", "title": "SOG Demo", "author": "Dummy Artist", "author_username": "dummy-artist",
                "license": "by", "source": "sog", "part": "scene",
            },
        )
        self.assertEqual(credit["viewer_url"], "https://example.invalid/scene/sogdemo1")
        text = bpy.data.texts[importer.ATTRIBUTIONS_TEXT].as_string()
        self.assertIn("\u201cSOG Demo\u201d by Dummy Artist (@dummy-artist)", text)
        self.assertIn("License: CC BY 4.0, https://creativecommons.org/licenses/by/4.0/", text)
        harness.select_only(obj)
        self.assertTrue(bpy.ops.supersplat.view_on_supersplat.poll())
        self.assertTrue(bpy.ops.supersplat.view_license.poll())

    @unittest.skipUnless(bpy.app.online_access, "needs --online-mode")
    def test_republishing_an_import_gives_back_the_original_file_coordinates(self) -> None:
        obj = self.imported("sogdemo1")
        bpy.context.view_layer.update()
        harness.select_only(obj)
        self.assertEqual(bpy.ops.supersplat.publish(), {"FINISHED"})
        path = Path(tempfile.mkdtemp()) / "uploaded.ply"
        path.write_bytes(self.fake.uploaded_bytes())
        ours, theirs = read_3dgs_ply(path), read_3dgs_ply(FIXTURES / "sh3.ply")
        path.unlink()
        path.parent.rmdir()
        for name in ("x", "y", "z", "rot_0", "rot_1", "rot_2", "rot_3"):
            for a, b in zip(ours.column(name), theirs.column(name)):
                self.assertAlmostEqual(abs(a) if name.startswith("rot") else a, abs(b) if name.startswith("rot") else b, delta=1e-4, msg=name)

    @unittest.skipUnless(bpy.app.online_access, "needs --online-mode")
    def test_ssog_environment_is_a_separate_child_object(self) -> None:
        obj = self.imported("superspl.at/s?id=lodsdemo")
        self.assertEqual(len(obj.data.points), 2100)
        environment = bpy.data.objects["SSOG Demo Environment"]
        self.assertTrue(blender_splat.is_splat_object(environment))
        self.assertEqual((len(environment.data.points), environment.parent), (50, obj))
        self.assertEqual(importer.attribution(environment)["part"], "environment")
        bpy.context.view_layer.update()
        self.assertEqual([round(v, 6) for v in environment.matrix_world.col[3]], [round(v, 6) for v in obj.matrix_world.col[3]])

    @unittest.skipUnless(bpy.app.online_access, "needs --online-mode")
    def test_level_picker_lists_the_levels_a_lookup_found(self) -> None:
        self.settings.import_link = "lodsdemo"
        self.assertEqual([item[0] for item in properties_levels(self.settings)], ["0"])
        self.assertEqual(bpy.ops.supersplat.lookup_scene(), {"FINISHED"})
        self.assertEqual(harness.runtime().importing.state().phase, "found")
        self.assertEqual(
            [item[1] for item in properties_levels(self.settings)],
            ["Finest (2,100 splats)", "Coarsest (400 splats)"],
        )
        self.assertEqual(self.settings.import_level, "0")
        self.settings.import_level = "1"
        self.settings.import_environment = False
        obj = self.imported("lodsdemo")
        self.assertEqual((len(obj.data.points), importer.attribution(obj)["level"]), (400, 1))
        self.assertIsNone(bpy.data.objects.get("SSOG Demo Environment"))
        # The same scene keeps its level; a link to another scene starts again from the finest.
        self.settings.import_link = "https://superspl.at/scene/lodsdemo"
        self.assertEqual(self.settings.import_level, "1")
        self.settings.import_link = "sogdemo1"
        self.assertEqual(self.settings.import_level, "0")

    @unittest.skipUnless(bpy.app.online_access, "needs --online-mode")
    def test_options_fold_away_after_an_import_until_import_again(self) -> None:
        panels = sys.modules[f"{harness.PACKAGE}.panels"]
        runtime = harness.runtime()
        self.assertFalse(bpy.ops.supersplat.reopen_import.poll())
        self.imported("lodsdemo")
        self.assertEqual(self.fake.downloaded_targets(), ["ssog"])
        self.assertTrue(panels.import_collapsed(self.settings, runtime.importing.state()))
        # A link to another scene shows its own options again.
        self.settings.import_link = "sogdemo1"
        self.assertFalse(panels.import_collapsed(self.settings, runtime.importing.state()))
        self.settings.import_link = "lodsdemo"
        self.assertEqual(bpy.ops.supersplat.reopen_import(), {"FINISHED"})
        state = runtime.importing.state()
        self.assertEqual((state.phase, state.scene.id), ("found", "lodsdemo"))
        self.assertFalse(panels.import_collapsed(self.settings, state))
        self.assertEqual(len(properties_levels(self.settings)), 2)

    @unittest.skipUnless(bpy.app.online_access, "needs --online-mode")
    def test_ply_download_matches_blenders_own_ply_import(self) -> None:
        obj = self.imported("plydemo1")
        self.assertEqual(importer.attribution(obj)["source"], "ply")
        before = set(bpy.data.objects)
        self.assertEqual(bpy.ops.wm.ply_import(filepath=str(FIXTURES / "sh3.ply")), {"FINISHED"})
        (reference,) = set(bpy.data.objects) - before
        SogImportTests.assert_same_splats(self, obj, reference)

    @unittest.skipUnless(bpy.app.online_access, "needs --online-mode")
    def test_decoding_failure_falls_back_to_the_ply(self) -> None:
        obj = self.imported("oldsog01")
        self.assertEqual(importer.attribution(obj)["source"], "ply")
        self.assertIn("could not be decoded", harness.runtime().importing.state().note)

    @unittest.skipUnless(bpy.app.online_access, "needs --online-mode")
    def test_crediting_the_same_scene_twice_adds_one_entry(self) -> None:
        first, second = self.imported("sogdemo1"), self.imported("sogdemo1")
        self.assertEqual((first.name, second.name), ("SOG Demo", "SOG Demo.001"))
        text = bpy.data.texts[importer.ATTRIBUTIONS_TEXT].as_string()
        self.assertEqual(text.count("\u201cSOG Demo\u201d by Dummy Artist"), 1)

    @unittest.skipUnless(bpy.app.online_access, "needs --online-mode")
    def test_clear_messages(self) -> None:
        cases = {
            "private1": "\u201cPrivate Demo\u201d by Dummy Artist is not downloadable: its owner has not allowed downloads.",
            "gsafdemo": "is an animated (GSAF) scene. Animated scenes cannot be imported into Blender yet.",
            "failply1": scene_import.PREPARE_FAILED_MESSAGE.format("PLY"),
            "nope0000": "No SuperSplat scene has the ID",
            "https://files.example.test/a/meta.json": scene_import.FILE_LINK_MESSAGE,
            "": scene_import.EMPTY_LINK_MESSAGE,
        }
        for link, message in cases.items():
            with self.subTest(link=link):
                self.assertIn(message, self.import_error(link))
        self.fake.daily_limit_reached = True
        self.assertIn("Your account has reached today's download limit.", self.import_error("plydemo1"))
        self.assertIn("Your account has reached today's download limit.", self.import_error("sogdemo1"))
        self.fake.daily_limit_reached = False
        self.fake.reject_key = True
        self.assertIn("Unauthorized. Check the PlayCanvas API key", self.import_error("sogdemo1"))
        self.fake.reject_key = False
        harness.runtime().credentials.clear()
        requests = len(self.fake.requests)
        self.assertEqual(self.import_error("sogdemo1"), MISSING_KEY_MESSAGE)
        self.assertEqual(len(self.fake.requests), requests)
        self.assertEqual(bpy.ops.supersplat.dismiss_import(), {"FINISHED"})
        self.assertEqual(harness.runtime().importing.error, "")

    @unittest.skipUnless(bpy.app.online_access, "needs --online-mode")
    def test_cancel_stops_a_download_without_creating_objects(self) -> None:
        import io
        import zipfile

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as bundle:
            bundle.writestr("scene.ply", (FIXTURES / "sh3.ply").read_bytes() + b"\0" * 400_000)
        self.fake._archives[("plydemo1", "ply")] = buffer.getvalue()
        self.fake.download_delay = 0.05
        self.settings.import_link = "plydemo1"
        session = importer._start(bpy.context, lookup_only=False)
        deadline = time.monotonic() + 10
        while session.snapshot().phase != "downloading" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(harness.runtime().importing.busy())
        self.assertEqual(bpy.ops.supersplat.cancel_import(), {"FINISHED"})
        self.assertTrue(session.join(10))
        self.assertEqual(session.snapshot().phase, "cancelled")
        self.assertEqual(len(bpy.data.objects), 0)
        self.assertFalse(harness.runtime().importing.busy())

    @unittest.skipUnless(bpy.app.online_access, "needs --online-mode")
    def test_loading_a_file_drops_an_import_waiting_to_be_built(self) -> None:
        self.settings.import_link = "sogdemo1"
        session = importer._start(bpy.context, lookup_only=False)
        self.assertTrue(session.join(10))
        self.assertEqual(session.snapshot().phase, "decoded")
        self.assertTrue(harness.runtime().importing.busy())
        bpy.ops.wm.read_homefile(use_empty=True)
        self.assertIsNone(harness.runtime().importing.session)
        self.assertFalse(harness.runtime().importing.busy())

    @unittest.skipIf(bpy.app.online_access, "run without --online-mode to test this guard")
    def test_offline_mode_blocks_importing(self) -> None:
        self.assertEqual(self.import_error("sogdemo1"), sessions.OFFLINE_MESSAGE)
        self.assertEqual(self.fake.requests, [])


def properties_levels(settings) -> list:  # noqa: ANN001
    return list(sys.modules[f"{harness.PACKAGE}.properties"]._levels(settings, bpy.context))


def reset_import_settings() -> None:
    # Look the scene up again: a test that loads a file frees the old one.
    scene = bpy.context.scene
    scene.supersplat.import_environment = True
    scene.supersplat.import_link = ""
    scene.cursor.location = (0.0, 0.0, 0.0)


def run_reporting_error(operator) -> str:  # noqa: ANN001
    try:
        result = operator()
    except RuntimeError as error:
        return str(error).removeprefix("Error: ").strip()
    raise AssertionError(f"expected an error report, operator returned {result}")


def read_bytes_count(data: bytes) -> int:
    header = data.split(b"end_header\n")[0].decode("ascii")
    return int(next(line.split()[2] for line in header.splitlines() if line.startswith("element vertex")))


def main() -> int:
    print(f"Blender {bpy.app.version_string}; online_access={bpy.app.online_access}")
    print(f"Installed {harness.built_zip().name} as {harness.PACKAGE}")
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    bpy.ops.preferences.addon_disable(module=harness.PACKAGE)
    print("Disabled cleanly:", harness.PACKAGE not in bpy.context.preferences.addons)
    return 0 if result.wasSuccessful() else 1


sys.exit(main())
