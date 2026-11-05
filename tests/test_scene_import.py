from __future__ import annotations

import dataclasses
import gzip
import io
import json
import shutil
import tempfile
import threading
import unittest
import unittest.mock
import zipfile
from pathlib import Path

try:
    import numpy as np
    import OpenImageIO  # noqa: F401
except ImportError:
    raise unittest.SkipTest("needs numpy and OpenImageIO; run the unit tests with Blender's bundled Python") from None

import guards
from fake_api import FAKE_API_KEY, FAKE_BASE_URL, FIXTURES, FakeSuperSplat, _json, demo_scenes
from splat_fixtures import ply_as_blender
from supersplat import scene_import
from supersplat.credentials import MISSING_KEY_MESSAGE, SessionCredentialStore
from supersplat.errors import ConfigurationError, DecodeError, DownloadHttpError, UploadCancelled
from supersplat.ply import SplatData
from supersplat.scene_import import ImportRequest, ImportSession, SceneInfo, parse_scene_reference


CREATE = "POST /v1/splats/{splatId}/downloads"
POLL = "GET /v1/splats/{splatId}/downloads/{target}"
FILE = "POST /v1/splats/{splatId}/downloads/{target}/file"


def setUpModule() -> None:
    guards.block_all()


def assert_splats_match(test: unittest.TestCase, ours: SplatData, reference: SplatData) -> None:
    test.assertEqual(ours.count, reference.count)
    test.assertEqual(len(ours.sh), len(reference.sh))
    for name in ("position", "base", "scale", "rotation"):
        np.testing.assert_allclose(getattr(ours, name), getattr(reference, name), rtol=1e-6, atol=1e-6, err_msg=name)
    for a, b in zip(ours.sh, reference.sh):
        np.testing.assert_allclose(a, b, atol=1e-7)


class LinkTests(unittest.TestCase):
    def test_scene_links_and_bare_ids(self) -> None:
        for text in (
            "a1b2c3d4",
            "  a1b2c3d4\n",
            "https://superspl.at/scene/a1b2c3d4",
            "https://superspl.at/scene/a1b2c3d4/",
            "http://superspl.at/scene/a1b2c3d4?utm=x#top",
            "superspl.at/scene/a1b2c3d4",
            "www.superspl.at/scene/a1b2c3d4",
            "https://superspl.at/s?id=a1b2c3d4",
            "superspl.at/s?id=a1b2c3d4&from=share",
            "https://SUPERSPL.AT/scene/a1b2c3d4",
        ):
            with self.subTest(text=text):
                self.assertEqual(parse_scene_reference(text), "a1b2c3d4")

    def test_other_environments_hosts_can_be_added(self) -> None:
        self.assertEqual(scene_import.site_hosts({}), scene_import.SITE_HOSTS)
        self.assertIn("staging-site.example.test", scene_import.site_hosts({"SUPERSPLAT_SITE_HOSTS": " Staging-Site.example.test, "}))
        with unittest.mock.patch.dict("os.environ", {"SUPERSPLAT_SITE_HOSTS": "staging-site.example.test"}):
            self.assertEqual(parse_scene_reference("https://staging-site.example.test/scene/a1b2c3d4"), "a1b2c3d4")
        with self.assertRaises(ConfigurationError):
            parse_scene_reference("https://staging-site.example.test/scene/a1b2c3d4")

    def test_everything_else_is_refused(self) -> None:
        cases = {
            "": scene_import.EMPTY_LINK_MESSAGE,
            "   ": scene_import.EMPTY_LINK_MESSAGE,
            "https://example.com/scene/a1b2c3d4": scene_import.INVALID_LINK_MESSAGE,
            "https://superspl.at.evil.test/scene/a1b2c3d4": scene_import.INVALID_LINK_MESSAGE,
            "https://superspl.at/": scene_import.INVALID_LINK_MESSAGE,
            "https://superspl.at/s": scene_import.INVALID_LINK_MESSAGE,
            "https://superspl.at/s?id=a&id=b": scene_import.INVALID_LINK_MESSAGE,
            "https://superspl.at/scene/a1b2%2F..%2Fx": scene_import.INVALID_LINK_MESSAGE,
            "ftp://superspl.at/scene/a1b2c3d4": scene_import.INVALID_LINK_MESSAGE,
            "file:///Users/me/scene/meta.json": scene_import.FILE_LINK_MESSAGE,
            "https://files.example.test/a1b2c3d4/v1/lod-meta.json": scene_import.FILE_LINK_MESSAGE,
            "https://superspl.at/scene/a1b2c3d4/scene.compressed.ply": scene_import.FILE_LINK_MESSAGE,
            "two words": scene_import.INVALID_LINK_MESSAGE,
        }
        for text, message in cases.items():
            with self.subTest(text=text):
                with self.assertRaises(ConfigurationError) as raised:
                    parse_scene_reference(text)
                self.assertEqual(str(raised.exception), message)


class SceneInfoTests(unittest.TestCase):
    def scene(self, scene_id: str) -> SceneInfo:
        fake = FakeSuperSplat().add_scenes(demo_scenes())
        return SceneInfo.from_api(fake._scene_json(fake.scenes[scene_id]))

    def test_downloadable_ssog(self) -> None:
        scene = self.scene("lodsdemo")
        self.assertEqual((scene.content_format, scene.levels, scene.lod_counts), ("ssog", 2, (2100, 400)))
        self.assertEqual((scene.author_name, scene.author_username, scene.license), ("Lod Artist", "lod-artist", "by-sa"))
        self.assertEqual(scene.problem(), "")

    def test_download_target_follows_the_content_format(self) -> None:
        targets = {splat_id: self.scene(splat_id).download_target for splat_id in ("sogdemo1", "lodsdemo", "plydemo1")}
        self.assertEqual(targets, {"sogdemo1": "sog", "lodsdemo": "ssog", "plydemo1": "ply"})

    def test_content_beyond_its_format_is_optional(self) -> None:
        fake = FakeSuperSplat().add_scenes(demo_scenes())
        splat = fake._scene_json(fake.scenes["lodsdemo"])
        self.assertEqual(set(splat["content"]), {"format", "version", "splatCount", "lodCounts"})
        splat["content"] = {"format": "ssog"}
        scene = SceneInfo.from_api(splat)
        self.assertEqual((scene.download_target, scene.levels, scene.splat_count), ("ssog", 1, None))
        splat["content"] = None
        self.assertEqual(SceneInfo.from_api(splat).download_target, "ply")

    def test_problems(self) -> None:
        self.assertIn("animated (GSAF) scene", self.scene("gsafdemo").problem())
        self.assertIn("“Private Demo” by Dummy Artist is not downloadable", self.scene("private1").problem())
        processing = dataclasses.replace(self.scene("private1"), status="processing")
        self.assertIn("still processing", processing.problem())

    def test_credits_and_licenses(self) -> None:
        self.assertEqual(
            scene_import.credit_lines(self.scene("lodsdemo")),
            [
                "“SSOG Demo” by Lod Artist (@lod-artist)",
                "  Scene:   https://example.invalid/scene/lodsdemo",
                "  License: CC BY-SA 4.0, https://creativecommons.org/licenses/by-sa/4.0/",
            ],
        )
        # Not an enum in the spec: codes this version does not know are shown as they are.
        self.assertEqual((scene_import.license_name("cc0"), scene_import.license_url("cc0")), ("CC0", ""))
        self.assertEqual(scene_import.format_count(7_500_000), "7.5M")
        self.assertEqual(scene_import.format_count(466_000), "466k")
        self.assertEqual(scene_import.format_count(2100), "2,100")


class SessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.staging = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.staging, True)
        self.fake = FakeSuperSplat().add_scenes(demo_scenes())
        self.credentials = SessionCredentialStore()
        self.credentials.save(FAKE_API_KEY)

    def run_import(self, splat_id: str, **values) -> tuple[scene_import.ImportState, ImportSession]:  # noqa: ANN003
        values.setdefault("poll_interval", 0.0)
        request = ImportRequest(splat_id, self.credentials, FAKE_BASE_URL, self.staging, **values)
        session = ImportSession(request, transport=self.fake, files=self.fake)
        state = session.run()
        self.assertEqual(self.fake.violations, [])
        self.assertEqual(list(self.staging.iterdir()), [], "staging files are removed")
        return state, session

    def test_an_unexpected_error_is_shown_and_its_traceback_logged(self) -> None:
        with unittest.mock.patch.object(scene_import.sog, "load_scene", side_effect=RuntimeError("boom")), \
                unittest.mock.patch.object(scene_import.ImportSession, "_download_ply", side_effect=RuntimeError("boom")), \
                self.assertLogs(scene_import.log, "ERROR") as logged:
            state, _ = self.run_import("sogdemo1")
        self.assertEqual(state.phase, "failed")
        self.assertIn("Unexpected error: RuntimeError", state.error)
        self.assertIn("Traceback", logged.output[0])

    def test_sog_is_decoded_from_its_download_zip(self) -> None:
        state, session = self.run_import("sogdemo1")
        self.assertEqual((state.phase, state.source, state.note), ("decoded", "sog", ""))
        self.assertEqual(state.scene.title, "SOG Demo")
        result = session.take_result()
        assert_splats_match(self, result.splats, ply_as_blender(FIXTURES / "sh3.ply"))
        self.assertIsNone(session.take_result(), "the result is handed over once")
        self.assertEqual(self.fake.routes(), ["GET /v1/splats/{splatId}", CREATE, POLL, POLL, FILE])
        self.assertEqual(self.fake.requests[1].json, {"target": "sog"})
        # Only the signed ZIP is fetched, never the scene's published files.
        self.assertEqual(self.fake.downloaded_targets(), ["sog"])
        self.assertTrue(self.fake.file_requests[0].endswith("archive.zip?signature=dummy"))

    def test_ssog_finest_level_and_environment(self) -> None:
        state, session = self.run_import("lodsdemo")
        result = session.take_result()
        self.assertEqual((state.source, state.level, result.level), ("ssog", 0, 0))
        assert_splats_match(self, result.splats, ply_as_blender(FIXTURES / "lod-0.ply"))
        assert_splats_match(self, result.environment, ply_as_blender(FIXTURES / "lod-env.ply"))
        self.assertEqual(self.fake.downloaded_targets(), ["ssog"])

    def test_ssog_chosen_level_without_environment(self) -> None:
        state, session = self.run_import("lodsdemo", level=1, environment=False)
        result = session.take_result()
        self.assertEqual(result.level, 1)
        self.assertIsNone(result.environment)
        assert_splats_match(self, result.splats, ply_as_blender(FIXTURES / "lod-1.ply"))
        self.assertEqual(self.fake.downloaded_targets(), ["ssog"])

    def test_a_download_wrapped_in_a_folder_with_plain_json_decodes(self) -> None:
        scene = self.fake.scenes["lodsdemo"]
        self.fake._archives[("lodsdemo", "ssog")] = _zip(
            {f"lodsdemo/{name}": data for name, data in scene.published_files().items()} | {"lodsdemo/LICENSE.txt": b"CC BY-SA"}
        )
        state, session = self.run_import("lodsdemo", level=1)
        self.assertEqual((state.phase, state.source, state.note), ("decoded", "ssog", ""))
        assert_splats_match(self, session.take_result().splats, ply_as_blender(FIXTURES / "lod-1.ply"))

    def test_an_unsafe_download_falls_back_to_the_ply(self) -> None:
        self.fake._archives[("sogdemo1", "sog")] = _zip({"../meta.json": b"{}"})
        state, session = self.run_import("sogdemo1")
        self.assertEqual((state.phase, state.source), ("decoded", "ply"))
        self.assertIn("unsafe file name: '../meta.json'", state.note)
        self.assertEqual(self.fake.downloaded_targets(), ["sog", "ply"])

    def test_a_scene_without_content_details_still_imports(self) -> None:
        original = self.fake._scene_json

        def bare(scene):  # noqa: ANN001, ANN202
            value = original(scene)
            # The least the spec allows: no lodCounts, no splat count.
            value["content"] = {"format": value["content"]["format"], "version": 1, "splatCount": None}
            return value

        self.fake._scene_json = bare
        state, session = self.run_import("lodsdemo", level=1)
        result = session.take_result()
        # Without lodCounts the level picker knows one level, so the finest is decoded.
        self.assertEqual((state.source, result.level, result.splats.count), ("ssog", 0, 2100))

    def test_levels_beyond_the_coarsest_are_clamped(self) -> None:
        state, session = self.run_import("lodsdemo", level=9)
        self.assertEqual((state.level, session.take_result().splats.count), (1, 400))

    def test_legacy_ply_goes_through_the_ply_download(self) -> None:
        statuses: list[str] = []
        original = self.fake._download_json

        def record(scene, target):  # noqa: ANN001, ANN202
            value = original(scene, target)
            statuses.append(value["status"])
            return value

        self.fake._download_json = record
        state, session = self.run_import("plydemo1")
        self.assertEqual((state.phase, state.source), ("decoded", "ply"))
        self.assertEqual(statuses, ["queued", "processing", "ready"])
        self.assertEqual(self.fake.routes(), ["GET /v1/splats/{splatId}", CREATE, POLL, POLL, FILE])
        assert_splats_match(self, session.take_result().splats, ply_as_blender(FIXTURES / "sh3.ply"))
        # The signed URL is fetched without the API key: FileSource takes no headers at all.
        self.assertTrue(self.fake.file_requests[-1].endswith("archive.zip?signature=dummy"))

    def test_decoding_failure_falls_back_to_the_ply(self) -> None:
        state, session = self.run_import("oldsog01")
        self.assertEqual((state.phase, state.source), ("decoded", "ply"))
        self.assertEqual(self.fake.downloaded_targets(), ["sog", "ply"])
        self.assertIn("legacy (version 1) SOG", state.note)
        self.assertIn("PLY of the scene was imported instead", state.note)
        assert_splats_match(self, session.take_result().splats, ply_as_blender(FIXTURES / "sh3.ply"))

    def test_missing_image_support_falls_back_to_the_ply(self) -> None:
        original = scene_import.sog.load_scene

        def no_openimageio(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
            raise ImportError("No module named 'OpenImageIO'")

        scene_import.sog.load_scene = no_openimageio
        self.addCleanup(setattr, scene_import.sog, "load_scene", original)
        state, _ = self.run_import("lodsdemo")
        self.assertEqual(state.source, "ply")
        self.assertIn("finest level, without the environment", state.note)

    def test_when_the_fallback_fails_too_both_reasons_are_given(self) -> None:
        self.fake.scenes["oldsog01"].ply_steps = ("failed",)
        state, _ = self.run_import("oldsog01")
        self.assertEqual(state.phase, "failed")
        self.assertIn(scene_import.PREPARE_FAILED_MESSAGE.format("PLY"), state.error)
        self.assertIn("could not be decoded either: meta.json is a legacy (version 1) SOG", state.error)

    def test_scenes_that_cannot_be_imported_are_never_downloaded(self) -> None:
        for splat_id, message in (
            ("private1", "is not downloadable: its owner has not allowed downloads."),
            ("gsafdemo", "Animated scenes cannot be imported into Blender yet."),
        ):
            with self.subTest(splat_id=splat_id):
                self.fake.requests.clear()
                state, _ = self.run_import(splat_id)
                self.assertEqual(state.phase, "failed")
                self.assertIn(message, state.error)
                self.assertEqual(self.fake.routes(), ["GET /v1/splats/{splatId}"])
                self.assertEqual(self.fake.file_requests, [])

    def test_unknown_scene(self) -> None:
        state, _ = self.run_import("nope0000")
        self.assertEqual(state.error, "No SuperSplat scene has the ID “nope0000”. Check the link.")

    def test_daily_download_limit_is_reported_and_not_retried(self) -> None:
        self.fake.daily_limit_reached = True
        for splat_id in ("sogdemo1", "lodsdemo", "plydemo1"):
            with self.subTest(splat_id=splat_id):
                self.fake.requests.clear()
                state, _ = self.run_import(splat_id)
                self.assertEqual(state.phase, "failed")
                self.assertIn("Your account has reached today's download limit.", state.error)
                self.assertEqual((self.fake.routes().count(CREATE), self.fake.routes().count(FILE)), (1, 1))
                self.assertEqual(self.fake.file_requests, [])

    def test_owner_turning_downloads_off_mid_import(self) -> None:
        for splat_id in ("sogdemo1", "plydemo1"):
            with self.subTest(splat_id=splat_id):
                self.fake.queue("/v1/splats/{splatId}/downloads", 404, {"error": "downloadable splat not found"})
                state, _ = self.run_import(splat_id)
                self.assertIn("is not downloadable", state.error)

    def test_failed_preparation_is_requested_again_then_reported(self) -> None:
        state, _ = self.run_import("failply1")
        self.assertEqual(state.error, scene_import.PREPARE_FAILED_MESSAGE.format("PLY"))
        # Requested again once after it failed.
        self.assertEqual(self.fake.routes().count(CREATE), 2)
        self.assertEqual(self.fake.minted, [])

    def test_a_sog_download_that_cannot_be_prepared_is_reported_not_replaced(self) -> None:
        self.fake.scenes["sogdemo1"].sog_steps = ("queued", "failed")
        state, _ = self.run_import("sogdemo1")
        self.assertEqual(state.error, "SuperSplat could not prepare the SOG download of this scene. Try again later.")
        self.assertEqual([r.json for r in self.fake.requests if r.json], [{"target": "sog"}] * 2)

    def test_a_download_that_is_no_longer_current_is_requested_again(self) -> None:
        self.fake.queue("/v1/splats/{splatId}/downloads/{target}", 404, {"error": "download not found"})
        state, _ = self.run_import("plydemo1")
        self.assertEqual(state.phase, "decoded", state.error)
        self.assertEqual(self.fake.routes().count(CREATE), 2)

    def test_a_file_that_is_not_ready_after_all_is_prepared_again(self) -> None:
        self.fake.queue("/v1/splats/{splatId}/downloads/{target}/file", 409, {"error": "Not ready.", "code": "not_ready"})
        state, _ = self.run_import("sogdemo1")
        self.assertEqual((state.phase, state.source), ("decoded", "sog"), state.error)
        self.assertEqual((self.fake.routes().count(CREATE), self.fake.routes().count(FILE)), (2, 2))

    def test_a_file_of_unknown_size_downloads_without_a_total(self) -> None:
        self.fake.unknown_file_bytes = True
        seen: list[tuple[float | None, str]] = []
        original = scene_import.ImportSession._update

        def record(session, **values):  # noqa: ANN001, ANN003, ANN202
            original(session, **values)
            state = session._state
            if state.phase == "downloading" and "progress_text" in values:
                seen.append((state.progress, state.progress_text))

        scene_import.ImportSession._update = record
        self.addCleanup(setattr, scene_import.ImportSession, "_update", original)
        state, session = self.run_import("lodsdemo")
        self.assertEqual((state.phase, state.source), ("decoded", "ssog"), state.error)
        self.assertTrue(seen and all(progress is None and " / " not in text for progress, text in seen[1:]), seen)

    def test_a_file_that_is_gone_is_prepared_again(self) -> None:
        self.fake.queue("/v1/splats/{splatId}/downloads/{target}/file", 404, {"error": "download file not found"})
        state, _ = self.run_import("plydemo1")
        self.assertEqual(state.phase, "decoded", state.error)
        self.assertEqual((self.fake.routes().count(CREATE), self.fake.routes().count(FILE)), (2, 2))

    def test_each_download_asks_for_one_file_url(self) -> None:
        self.run_import("sogdemo1")
        self.assertEqual(len(self.fake.minted), 1)
        self.fake.minted.clear()
        self.run_import("oldsog01")  # decoding fails, so the PLY is downloaded too
        self.assertEqual([url.split("/")[5] for url in self.fake.minted], ["sog", "ply"])

    def test_a_republish_while_preparing_stops_the_import(self) -> None:
        original = self.fake._scene_json

        def looked_up_before_the_republish(scene):  # noqa: ANN001, ANN202
            value = original(scene)
            value["content"]["version"] = 1
            return value

        self.fake._scene_json = looked_up_before_the_republish
        self.fake.scenes["lodsdemo"].version = 2
        state, _ = self.run_import("lodsdemo")
        self.assertEqual((state.phase, state.error), ("failed", scene_import.REPUBLISHED_MESSAGE))
        self.assertEqual(self.fake.minted, [])

    def test_a_republish_before_the_file_stops_the_import(self) -> None:
        self.fake.queue(
            "/v1/splats/{splatId}/downloads/{target}/file",
            200,
            {"url": "https://downloads.example.invalid/x.zip?signature=dummy", "urlExpiresAt": "2099-01-01T00:00:00.000Z", "bytes": 10, "version": 2},
        )
        state, _ = self.run_import("plydemo1")
        self.assertEqual(state.error, "The scene was republished while importing. Import it again.")
        self.assertEqual(self.fake.file_requests, [])

    def test_an_expired_signed_url_is_replaced_once(self) -> None:
        original = self.fake.download
        calls: list[str] = []

        def expire_first(url, path, limit, **values):  # noqa: ANN001, ANN003, ANN202
            calls.append(url)
            if len(calls) == 1:
                raise DownloadHttpError("Downloading failed with HTTP 403.", 403)
            return original(url, path, limit, **values)

        self.fake.download = expire_first
        state, _ = self.run_import("plydemo1")
        self.assertEqual((state.phase, len(calls)), ("decoded", 2))
        # A new signed URL, for the same prepared download.
        self.assertEqual((self.fake.routes().count(CREATE), self.fake.routes().count(FILE)), (1, 2))

    def test_transient_errors_are_retried(self) -> None:
        self.fake.queue("/v1/splats/{splatId}", 503, {"error": "Service unavailable"})
        original = scene_import.ImportSession._sleep
        scene_import.ImportSession._sleep = lambda _self, _seconds: None
        self.addCleanup(setattr, scene_import.ImportSession, "_sleep", original)
        state, _ = self.run_import("sogdemo1")
        self.assertEqual(state.phase, "decoded")
        self.assertEqual(self.fake.routes().count("GET /v1/splats/{splatId}"), 2)

    def test_any_decoding_failure_falls_back_to_the_ply(self) -> None:
        original = scene_import.sog.load_scene

        def missing_field(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
            raise KeyError("mins")

        scene_import.sog.load_scene = missing_field
        self.addCleanup(setattr, scene_import.sog, "load_scene", original)
        state, _ = self.run_import("sogdemo1")
        self.assertEqual((state.phase, state.source), ("decoded", "ply"))
        self.assertIn("'mins'", state.note)

    def test_a_corrupt_gzip_manifest_falls_back_to_the_ply(self) -> None:
        self.fake.scenes["sogdemo1"].files["meta.json"] = b"\x1f\x8b\x08\x00broken"
        state, _ = self.run_import("sogdemo1")
        self.assertEqual(state.source, "ply")
        self.assertIn("meta.json is not valid gzip", state.note)

    def test_a_gone_download_after_an_expired_url_is_requested_again(self) -> None:
        original = self.fake.download
        calls: list[str] = []

        def expire_first(url, path, limit, **values):  # noqa: ANN001, ANN003, ANN202
            calls.append(url)
            if len(calls) == 1:
                self.fake.queue("/v1/splats/{splatId}/downloads/{target}/file", 404, {"error": "download not found"})
                raise DownloadHttpError("Downloading failed with HTTP 403.", 403)
            return original(url, path, limit, **values)

        self.fake.download = expire_first
        state, _ = self.run_import("plydemo1")
        self.assertEqual((state.phase, len(calls)), ("decoded", 2), state.error)
        self.assertEqual(self.fake.routes().count(CREATE), 2)

    def test_reopen_goes_back_from_an_import_to_its_lookup(self) -> None:
        state, session = self.run_import("lodsdemo")
        session.reopen()
        self.assertEqual(session.snapshot().phase, "decoded", "only a finished import reopens")
        session.take_result()
        session.finish(object_name="SSOG Demo", splat_count=2100)
        session.reopen()
        state = session.snapshot()
        self.assertEqual((state.phase, state.scene.id, state.active), ("found", "lodsdemo", False))

    def test_cancel_drops_a_decoded_scene_not_built_yet(self) -> None:
        state, session = self.run_import("sogdemo1")
        self.assertEqual(state.phase, "decoded")
        session.cancel()
        self.assertEqual(session.snapshot().phase, "cancelled")
        self.assertIsNone(session.take_result())
        self.assertFalse(session.snapshot().active)

    def test_rejected_and_missing_keys(self) -> None:
        self.fake.reject_key = True
        state, _ = self.run_import("sogdemo1")
        self.assertIn("Unauthorized. Check the PlayCanvas API key", state.error)
        self.credentials.clear()
        state, _ = self.run_import("sogdemo1")
        self.assertEqual(state.error, MISSING_KEY_MESSAGE)

    def test_lookup_only_stops_before_downloading(self) -> None:
        state, session = self.run_import("lodsdemo", lookup_only=True)
        self.assertEqual((state.phase, state.scene.lod_counts), ("found", (2100, 400)))
        self.assertFalse(state.active)
        self.assertIsNone(session.take_result())
        self.assertEqual(self.fake.file_requests, [])

    def test_cancel_during_a_download(self) -> None:
        self.fake.download_delay = 0.05
        request = ImportRequest("plydemo1", self.credentials, FAKE_BASE_URL, self.staging, poll_interval=0.0)
        session = ImportSession(request, transport=self.fake, files=self.fake)
        # A multi-chunk archive, so the download is still running when Cancel arrives.
        self.fake._archives[("plydemo1", "ply")] = _zip({"scene.ply": (FIXTURES / "sh3.ply").read_bytes() + b"\0" * 400_000})
        session.start()
        while session.snapshot().phase != "downloading":
            self.assertTrue(session.snapshot().active, session.snapshot().error)
            threading.Event().wait(0.01)
        session.cancel()
        self.assertTrue(session.join(10))
        self.assertEqual(session.snapshot().phase, "cancelled")
        self.assertIsNone(session.take_result())
        self.assertEqual(list(self.staging.iterdir()), [])

    def test_cancel_while_preparing(self) -> None:
        request = ImportRequest("plydemo1", self.credentials, FAKE_BASE_URL, self.staging, poll_interval=30.0)
        session = ImportSession(request, transport=self.fake, files=self.fake)
        session.start()
        while session.snapshot().phase != "preparing" or "(queued)" not in session.snapshot().progress_text:
            threading.Event().wait(0.01)
        state = session.snapshot()
        self.assertEqual((state.status_text, state.progress_text), ("Preparing...", "SuperSplat is packaging the PLY (queued)"))
        session.cancel()
        self.assertTrue(session.join(5))
        self.assertEqual(session.snapshot().phase, "cancelled")


def _zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for name, data in members.items():
            bundle.writestr(name, data)
    return buffer.getvalue()


class UnpackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory)
        self.archive = self.directory / "download.zip"
        self.folder = self.directory / "unpacked"
        self.ply = (FIXTURES / "sh0.ply").read_bytes()

    def unpack(self, data: bytes, limit: int = 1 << 30, file_limit: int = 1 << 30, **values) -> Path:  # noqa: ANN003
        self.archive.write_bytes(data)
        scene_import.unpack_archive(self.archive, self.folder, limit=limit, file_limit=file_limit, **values)
        return self.folder

    def files(self) -> dict[str, bytes]:
        return {p.relative_to(self.folder).as_posix(): p.read_bytes() for p in sorted(self.folder.rglob("*")) if p.is_file()}

    def test_members_are_unpacked_and_gzip_members_decompressed(self) -> None:
        progress: list[float] = []
        self.unpack(
            _zip({"lod-meta.json": gzip.compress(b'{"a": 1}'), "0_0/means_l.webp": b"RIFFxxxxWEBP", "LICENSE.txt": b"CC BY"}),
            on_progress=progress.append,
        )
        self.assertEqual(
            self.files(),
            {"0_0/means_l.webp": b"RIFFxxxxWEBP", "LICENSE.txt": b"CC BY", "lod-meta.json": b'{"a": 1}'},
        )
        self.assertEqual(progress[-1], 1.0)

    def test_unsafe_names_are_refused(self) -> None:
        for name in ("../evil.json", "a/../../evil.json", "/etc/evil.json", "C:/evil.json", "a\\..\\evil.json", "a//b.json", "./a.json"):
            with self.subTest(name=name):
                shutil.rmtree(self.folder, ignore_errors=True)
                with self.assertRaisesRegex(DecodeError, "unsafe file name"):
                    self.unpack(_zip({name: b"{}"}))
                self.assertFalse((self.directory / "evil.json").exists())
        link = zipfile.ZipInfo("meta.json")
        link.external_attr = (0o120777 << 16)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as bundle:
            bundle.writestr(link, "/etc/passwd")
        shutil.rmtree(self.folder, ignore_errors=True)
        with self.assertRaisesRegex(DecodeError, "unsafe file name"):
            self.unpack(buffer.getvalue())

    def test_size_caps(self) -> None:
        with self.assertRaisesRegex(DecodeError, "unpacks to 2.0 KB, over the 1.0 KB limit"):
            self.unpack(_zip({"a.webp": b"x" * 1024, "b.webp": b"x" * 1024}), limit=1024)
        shutil.rmtree(self.folder)
        with self.assertRaisesRegex(DecodeError, "a.webp is 2.0 KB, over the 1.0 KB limit"):
            self.unpack(_zip({"a.webp": b"x" * 2048}), file_limit=1024)
        shutil.rmtree(self.folder)
        # A gzip member larger unpacked than its entry says.
        with self.assertRaisesRegex(DecodeError, "unpacks to more than the download limit"):
            self.unpack(_zip({"meta.json": gzip.compress(b" " * 4096)}), file_limit=1024)

    def test_duplicate_names_are_refused(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as bundle, self.assertWarns(UserWarning):
            bundle.writestr("meta.json", b"{}")
            bundle.writestr("meta.json", b"{}")
        with self.assertRaisesRegex(DecodeError, "holds meta.json twice"):
            self.unpack(buffer.getvalue())

    def test_bad_archives(self) -> None:
        with self.assertRaisesRegex(DecodeError, "not a valid ZIP"):
            self.unpack(b"not a zip")
        shutil.rmtree(self.folder)
        with self.assertRaisesRegex(DecodeError, "meta.json is not valid gzip"):
            self.unpack(_zip({"meta.json": b"\x1f\x8b\x08\x00broken"}))

    def test_a_bare_ply_is_accepted(self) -> None:
        self.unpack(self.ply)
        self.assertEqual(self.files(), {"scene.ply": self.ply})

    def test_cancel(self) -> None:
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(UploadCancelled):
            self.unpack(_zip({"scene.ply": self.ply}), cancel_event=cancel)


class FindTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder)

    def make(self, *names: str) -> None:
        for name in names:
            path = self.folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"{}")

    def test_manifest_at_the_top_or_one_folder_down(self) -> None:
        self.make("scene/meta.json", "scene/means_l.webp")
        self.assertEqual(scene_import.find_manifest(self.folder), self.folder / "scene" / "meta.json")
        self.make("meta.json")
        self.assertEqual(scene_import.find_manifest(self.folder), self.folder / "meta.json")

    def test_lod_meta_wins_over_the_level_meta_files(self) -> None:
        self.make("0_0/meta.json", "1_0/meta.json", "env/meta.json", "lod-meta.json")
        self.assertEqual(scene_import.find_manifest(self.folder), self.folder / "lod-meta.json")

    def test_missing_or_ambiguous_manifests(self) -> None:
        with self.assertRaisesRegex(DecodeError, "neither lod-meta.json nor meta.json"):
            scene_import.find_manifest(self.folder)
        self.make("a/b/meta.json")
        with self.assertRaisesRegex(DecodeError, "neither"):
            scene_import.find_manifest(self.folder)
        self.make("0_0/meta.json", "1_0/meta.json")
        with self.assertRaisesRegex(DecodeError, "holds 2 meta.json files"):
            scene_import.find_manifest(self.folder)

    def test_ply(self) -> None:
        self.make("other.PLY")
        self.assertEqual(scene_import.find_ply(self.folder), self.folder / "other.PLY")
        self.make("scene/scene.ply")
        self.assertEqual(scene_import.find_ply(self.folder), self.folder / "scene" / "scene.ply")
        self.make("scene.ply")
        with self.assertRaisesRegex(DecodeError, "holds 2 PLY files"):
            scene_import.find_ply(self.folder)


class FakeServesTheSpecTests(unittest.TestCase):
    """The download routes of the test double, against the OpenAPI document."""

    def test_post_then_poll_returns_202_then_200_with_location(self) -> None:
        fake = FakeSuperSplat().add_scenes(demo_scenes())
        headers = {"Authorization": f"Bearer {FAKE_API_KEY}", "Content-Type": "application/json"}
        url = f"{FAKE_BASE_URL}/v1/splats/plydemo1/downloads"
        first = fake.send("POST", url, headers, b'{"target":"ply"}', 5)
        again = fake.send("POST", url, headers, b'{"target":"ply"}', 5)
        self.assertEqual((first.status, again.status), (202, 200))
        self.assertEqual(first.headers["location"], "/api/supersplat/v1/splats/plydemo1/downloads/ply")
        self.assertEqual(sorted(json.loads(first.body)), ["bytes", "splatId", "status", "target", "version"])
        # A target the scene has no files for: refused, but a valid request.
        mismatched = fake.send("POST", url, headers, b'{"target":"sog"}', 5)
        self.assertEqual((mismatched.status, fake.violations), (400, []))
        invalid = fake.send("POST", url, headers, b'{"target":"spz"}', 5)
        self.assertEqual(invalid.status, 400)
        self.assertEqual(len(fake.violations), 1)  # the invalid request, recorded as such
        self.assertIn("request", fake.violations[0])
        sog = fake.send("POST", f"{FAKE_BASE_URL}/v1/splats/lodsdemo/downloads", headers, b'{"target":"ssog"}', 5)
        self.assertEqual(sog.status, 202)
        self.assertEqual(sog.headers["location"], "/api/supersplat/v1/splats/lodsdemo/downloads/ssog")
        self.assertEqual(fake.violations[1:], [])

    def test_unknown_key(self) -> None:
        fake = FakeSuperSplat().add_scenes(demo_scenes())
        response = fake.send("GET", f"{FAKE_BASE_URL}/v1/splats/sogdemo1", {"Authorization": "Bearer nope"}, None, 5)
        self.assertEqual(response.status, 401)
        anyone = FakeSuperSplat(api_key=None).add_scenes(demo_scenes())
        response = anyone.send("GET", f"{FAKE_BASE_URL}/v1/splats/sogdemo1", {"Authorization": "Bearer anything"}, None, 5)
        self.assertEqual(response.status, 200)
        self.assertEqual(_json(200, {}).headers["content-type"], "application/json")


if __name__ == "__main__":
    unittest.main()
