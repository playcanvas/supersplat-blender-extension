"""Checks the client against a trimmed copy of the public OpenAPI spec."""

from __future__ import annotations

import tomllib
import unittest
from pathlib import Path

import guards
import spec_check
from supersplat import UPLOAD_CLIENT_ID, __version__
from supersplat.errors import _NON_RETRYABLE_429_CODES
from supersplat.uploader import (
    MAX_DESCRIPTION_LENGTH,
    MAX_FILE_SIZE,
    MAX_MULTIPART_PARTS,
    MAX_TITLE_LENGTH,
    PARALLEL_PARTS,
)

ROOT = Path(__file__).resolve().parent.parent


def setUpModule() -> None:
    guards.block_all()


class SpecConformanceTests(unittest.TestCase):
    def test_limits_match_spec(self) -> None:
        create = spec_check.schema("CreateUploadRequest")["properties"]
        self.assertEqual(create["title"]["maxLength"], MAX_TITLE_LENGTH)
        self.assertEqual(create["description"]["maxLength"], MAX_DESCRIPTION_LENGTH)
        self.assertEqual(create["contentLength"]["anyOf"][0]["maximum"], MAX_FILE_SIZE)
        parts = spec_check.schema("CreateUploadPartUrlsRequest")["properties"]["parts"]
        self.assertLessEqual(PARALLEL_PARTS, parts["maxItems"])
        self.assertEqual(parts["items"]["maximum"], MAX_MULTIPART_PARTS)
        self.assertIn("ply", create["sourceFormat"]["enum"])

    def test_upload_client_identity_matches_spec_pattern(self) -> None:
        client = spec_check.schema("UploadClient")["properties"]
        self.assertRegex(UPLOAD_CLIENT_ID, client["id"]["pattern"])
        self.assertLessEqual(len(__version__), client["version"]["maxLength"])

    def test_error_codes_handled_by_the_client_are_in_the_spec(self) -> None:
        self.assertLessEqual(
            _NON_RETRYABLE_429_CODES | {"publish_in_progress"},
            set(spec_check.schema("TooManyRequestsError")["properties"]["code"]["enum"]),
        )
        self.assertEqual(spec_check.schema("FileTooLargeError")["properties"]["code"]["const"], "file_too_large")

    def test_import_handles_the_formats_and_states_in_the_spec(self) -> None:
        content = spec_check.schema("SplatContent")["properties"]
        self.assertEqual(set(content["format"]["enum"]), {"sog", "ssog", "compressed-ply"})
        download = spec_check.schema("Download")["properties"]
        self.assertEqual(set(download["status"]["enum"]), {"queued", "processing", "ready", "failed"})
        # Downloads are addressed by target; only .../file hands out a URL.
        self.assertEqual(set(download), {"splatId", "target", "version", "status", "bytes"})
        self.assertEqual(set(spec_check.schema("DownloadFile")["required"]), {"url", "urlExpiresAt", "bytes", "version"})
        self.assertIn("409", spec_check.operation("POST", "/v1/splats/{splatId}/downloads/{target}/file")["responses"])
        self.assertEqual(set(spec_check.schema("CreateDownloadRequest")["properties"]["target"]["enum"]), {"ply", "sog", "ssog"})
        self.assertLessEqual(
            {"author", "thumbnails", "downloadable", "license", "content"},
            set(spec_check.schema("Splat")["required"]),
        )
        self.assertIn("daily_download_limit_reached", _NON_RETRYABLE_429_CODES)


class ManifestTests(unittest.TestCase):
    def test_manifest_fields(self) -> None:
        manifest = tomllib.loads((ROOT / "blender_manifest.toml").read_text(encoding="utf-8"))
        # supersplat.__version__ is sent as uploadClient.version and in the User-Agent.
        self.assertEqual(manifest["version"], __version__)
        excluded = manifest["build"]["paths_exclude_pattern"]
        for pattern in ("/tests/", "/scripts/"):
            self.assertIn(pattern, excluded)


if __name__ == "__main__":
    unittest.main()
