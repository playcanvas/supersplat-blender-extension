from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import guards
from fake_api import FAKE_API_KEY, FAKE_BASE_URL, FakeSuperSplat, _json
from supersplat.api import HttpResponse
from supersplat.errors import ApiError, ConfigurationError, ProtocolError, UploadCancelled
from supersplat import UPLOAD_CLIENT_ID, __version__
from supersplat.uploader import UploadCallbacks, UploadEngine, UploadJob, part_size_for

PAYLOAD = bytes(range(256)) * 4 + b"tail"  # 1028 bytes


def setUpModule() -> None:
    guards.block_all()


class UploaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "scene.ply"
        self.path.write_bytes(PAYLOAD)
        patcher = patch.object(UploadEngine, "_wait_for_retry", staticmethod(lambda *_a: None))
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def job(self, **overrides) -> UploadJob:  # noqa: ANN003
        values = dict(
            file_path=str(self.path),
            source_format="ply",
            title="Dummy scene",
            description="Dummy description",
            idempotency_key="supersplat-test",
            base_url=FAKE_BASE_URL,
        )
        values.update(overrides)
        return UploadJob(**values)

    def upload(self, fake: FakeSuperSplat, job: UploadJob | None = None, **engine):  # noqa: ANN003
        engine.setdefault("transport", fake)
        return UploadEngine(**engine).upload(job or self.job(), FAKE_API_KEY, threading.Event())

    def test_multipart_upload_follows_spec_flow(self) -> None:
        fake = FakeSuperSplat(part_size=200)
        stages: list[str] = []
        progress = []
        callbacks = UploadCallbacks(on_stage=stages.append, on_progress=progress.append)
        outcome = UploadEngine(transport=fake).upload(
            self.job(), FAKE_API_KEY, threading.Event(), callbacks
        )
        self.assertEqual(fake.violations, [])
        self.assertEqual(fake.uploaded_bytes(), PAYLOAD)
        self.assertEqual([p["partNumber"] for p in fake.completed_parts], [1, 2, 3, 4, 5, 6])
        self.assertEqual(outcome.splat_id, "a1b2c3d4")
        self.assertEqual(outcome.edit_url, "https://example.invalid/scene/a1b2c3d4/edit")
        self.assertEqual(outcome.viewer_url, "https://example.invalid/scene/a1b2c3d4")
        self.assertEqual(progress[-1].fraction, 1.0)
        self.assertIn("Uploading 6 parts...", stages)
        signing = [r.json["parts"] for r in fake.requests if r.route.endswith("part-upload-urls")]
        self.assertEqual(signing, [[1, 2, 3, 4], [5, 6]])
        api_routes = [route for route in fake.routes() if "signed" not in route]
        self.assertEqual(
            api_routes,
            [
                "GET /v1/me",
                "POST /v1/splats/uploads",
                "POST /v1/splats/uploads/{uploadId}/part-upload-urls",
                "POST /v1/splats/uploads/{uploadId}/part-upload-urls",
                "POST /v1/splats/uploads/{uploadId}/complete",
                "GET /v1/splats/{splatId}",
            ],
        )

    def test_create_body_matches_create_upload_request(self) -> None:
        fake = FakeSuperSplat()
        self.upload(fake)
        create = next(r for r in fake.requests if r.route == "/v1/splats/uploads")
        self.assertEqual(
            create.json,
            {
                "sourceFormat": "ply",
                "contentLength": len(PAYLOAD),
                "uploadClient": {"id": UPLOAD_CLIENT_ID, "version": __version__},
                "title": "Dummy scene",
                "description": "Dummy description",
            },
        )
        self.assertEqual(create.headers["Idempotency-Key"], "supersplat-test")

    def test_empty_optional_metadata_is_omitted(self) -> None:
        fake = FakeSuperSplat()
        self.upload(fake, self.job(title="", description=""))
        create = next(r for r in fake.requests if r.route == "/v1/splats/uploads")
        self.assertNotIn("title", create.json)
        self.assertNotIn("description", create.json)

    def test_existing_session_for_idempotency_key_returns_200(self) -> None:
        fake = FakeSuperSplat(create_status=200)
        self.assertEqual(self.upload(fake).splat_id, "a1b2c3d4")
        self.assertEqual(fake.violations, [])

    def test_transient_part_failure_is_retried_with_a_fresh_url(self) -> None:
        fake = FakeSuperSplat(part_size=600)
        fake.put_failures[2] = [HttpResponse(503, b"")]
        self.upload(fake)
        self.assertEqual(fake.uploaded_bytes(), PAYLOAD)
        signing = [r.json["parts"] for r in fake.requests if r.route.endswith("part-upload-urls")]
        self.assertEqual(signing, [[1, 2], [2]])

    def test_permanent_part_failure_stops_the_upload(self) -> None:
        fake = FakeSuperSplat()
        fake.put_failures[1] = [HttpResponse(403, b"")]
        with self.assertRaises(ApiError) as raised:
            self.upload(fake)
        self.assertEqual(raised.exception.status, 403)

    def test_rate_limit_retries_then_succeeds(self) -> None:
        fake = FakeSuperSplat()
        fake.queue("/v1/me", 429, {"error": "Too many requests"})
        self.upload(fake)
        self.assertEqual(fake.routes().count("GET /v1/me"), 2)

    def test_daily_limit_fails_without_retry(self) -> None:
        fake = FakeSuperSplat()
        fake.queue(
            "/v1/splats/uploads",
            429,
            {"error": "Daily publishing limit reached", "code": "daily_publish_limit_reached"},
            {"Retry-After": "3600"},
        )
        with self.assertRaises(ApiError) as raised:
            self.upload(fake)
        self.assertEqual(raised.exception.code, "daily_publish_limit_reached")
        self.assertEqual(fake.routes().count("POST /v1/splats/uploads"), 1)

    def test_invalid_server_responses_raise_protocol_errors(self) -> None:
        cases = [
            {"part_url_overrides": {"uploadId": "upl_other"}},
            {"part_url_overrides": {"partSize": 1}},
            {"complete_overrides": {"editUrl": ""}},
            {"complete_overrides": {"uploadId": "upl_other"}},
            {"splat_overrides": {"id": "zzzzzzzz"}},
            {"session_overrides": {"status": "canceled"}},
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ProtocolError):
                    self.upload(FakeSuperSplat(**overrides))

    def test_cancel_before_start(self) -> None:
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(UploadCancelled):
            UploadEngine(transport=FakeSuperSplat()).upload(self.job(), FAKE_API_KEY, cancel)

    def test_cancel_during_part_upload(self) -> None:
        fake = FakeSuperSplat(part_size=100)
        cancel = threading.Event()

        def on_progress(progress) -> None:  # noqa: ANN001
            if progress.uploaded_bytes >= 200:
                cancel.set()

        with self.assertRaises(UploadCancelled):
            UploadEngine(transport=fake).upload(
                self.job(), FAKE_API_KEY, cancel, UploadCallbacks(on_progress=on_progress)
            )
        self.assertIsNone(fake.completed_parts)

    def test_metadata_limits_from_spec(self) -> None:
        with self.assertRaises(ConfigurationError):
            self.upload(FakeSuperSplat(), self.job(title="x" * 101))
        with self.assertRaises(ConfigurationError):
            self.upload(FakeSuperSplat(), self.job(description="x" * 5001))

    def test_empty_or_missing_file(self) -> None:
        self.path.write_bytes(b"")
        with self.assertRaises(ConfigurationError):
            self.upload(FakeSuperSplat())
        self.path.unlink()
        with self.assertRaises(ConfigurationError):
            self.upload(FakeSuperSplat())


class HelperTests(unittest.TestCase):
    def test_part_size_for_last_part(self) -> None:
        self.assertEqual(part_size_for(1028, 300, 4), 128)
        self.assertEqual(part_size_for(1028, 300, 5), 0)

    def test_json_helper_lowercases_headers(self) -> None:
        self.assertEqual(_json(200, {}, {"Retry-After": "1"}).headers["retry-after"], "1")


if __name__ == "__main__":
    unittest.main()
