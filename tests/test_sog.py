from __future__ import annotations

import gzip
import io
import json
import shutil
import tempfile
import threading
import unittest
import urllib.request
import urllib.response
from email.message import Message
from pathlib import Path

try:
    import numpy as np
except ImportError:
    raise unittest.SkipTest("needs numpy; run the unit tests with Blender's bundled Python") from None
try:
    import OpenImageIO as oiio
except ImportError:
    raise unittest.SkipTest("needs OpenImageIO; run the unit tests with Blender's bundled Python") from None

import guards
from splat_fixtures import ply_as_blender
from supersplat import sog
from supersplat.errors import DecodeError, UploadCancelled
from supersplat.ply import SplatData

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "sog"


def setUpModule() -> None:
    guards.block_all()


def assert_splats_match(test: unittest.TestCase, ours: SplatData, reference: SplatData) -> None:
    """Equal up to float32 rounding (opacity and quaternions go through exp/sigmoid/normalize)."""
    test.assertEqual(ours.count, reference.count)
    test.assertEqual(len(ours.sh), len(reference.sh))
    for name in ("position", "base", "scale", "rotation"):
        np.testing.assert_allclose(getattr(ours, name), getattr(reference, name), rtol=1e-6, atol=1e-6, err_msg=name)
    for index, (a, b) in enumerate(zip(ours.sh, reference.sh)):
        np.testing.assert_array_equal(np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32), f"sh_{index}")


class DecodeTests(unittest.TestCase):
    def test_sog_with_degree_3_matches_splat_transform_ply(self) -> None:
        assert_splats_match(self, sog.decode_sog(FIXTURES / "sh3"), ply_as_blender(FIXTURES / "sh3.ply"))

    def test_sog_without_harmonics_matches(self) -> None:
        splats = sog.decode_sog(FIXTURES / "sh0" / "meta.json")
        self.assertEqual(splats.sh, [])
        assert_splats_match(self, splats, ply_as_blender(FIXTURES / "sh0.ply"))

    def test_ssog_levels_and_environment_match(self) -> None:
        scene = sog.load_scene(FIXTURES / "lod")
        self.assertEqual((scene.level, scene.level_counts), (0, [2100, 400]))
        assert_splats_match(self, scene.splats, ply_as_blender(FIXTURES / "lod-0.ply"))
        assert_splats_match(self, scene.environment, ply_as_blender(FIXTURES / "lod-env.ply"))
        coarse = sog.load_scene(FIXTURES / "lod" / "lod-meta.json", level=1, environment=False)
        self.assertIsNone(coarse.environment)
        assert_splats_match(self, coarse.splats, ply_as_blender(FIXTURES / "lod-1.ply"))

    def test_level_0_spans_several_files(self) -> None:
        lod = json.loads((FIXTURES / "lod" / "lod-meta.json").read_text())
        files = sog._level_files(lod, 0, "lod-meta.json")
        self.assertEqual([str(path) for path, _ in files], ["0_0/meta.json", "0_1/meta.json"])
        self.assertEqual(sum(count for _, count in files), 2100)

    def test_plain_sog_scene(self) -> None:
        scene = sog.load_scene(FIXTURES / "sh3")
        self.assertEqual((scene.level, scene.level_counts, scene.environment), (0, [300], None))
        with self.assertRaisesRegex(DecodeError, "only level 0"):
            sog.load_scene(FIXTURES / "sh3", level=1)

    def test_level_out_of_range(self) -> None:
        with self.assertRaisesRegex(DecodeError, "levels 0-1"):
            sog.load_scene(FIXTURES / "lod", level=2)

    def test_folder_without_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(DecodeError, "neither"):
                sog.load_scene(directory)
        with self.assertRaisesRegex(DecodeError, "does not exist"):
            sog.load_scene(FIXTURES / "missing")


class ManifestValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory)
        shutil.copytree(FIXTURES / "sh3", self.directory / "sh3")
        shutil.copytree(FIXTURES / "lod", self.directory / "lod")

    def edit(self, path: Path, change) -> None:
        meta = json.loads(path.read_text())
        change(meta)
        path.write_text(json.dumps(meta))

    def test_legacy_and_unknown_versions_are_rejected(self) -> None:
        meta = self.directory / "sh3" / "meta.json"
        self.edit(meta, lambda m: m.pop("version"))
        with self.assertRaisesRegex(DecodeError, "legacy"):
            sog.decode_sog(meta)
        self.edit(meta, lambda m: m.update(version=3))
        with self.assertRaisesRegex(DecodeError, "unsupported SOG version 3"):
            sog.decode_sog(meta)

    def test_unsafe_file_names_are_rejected(self) -> None:
        meta = self.directory / "sh3" / "meta.json"
        for name in (
            "../sh0/means_l.webp",
            "/etc/means_l.webp",
            "//evil.test/a.webp",
            "https://evil.test/a.webp",
            "a\\b.webp",
            "a//b.webp",
            "%2e%2e/sh0/means_l.webp",
            "means_l.webp?x",
            "means_l.webp#x",
        ):
            with self.subTest(name=name):
                self.edit(meta, lambda m: m["means"].update(files=[name, "means_u.webp"]))
                with self.assertRaisesRegex(DecodeError, "unsafe|invalid"):
                    sog.decode_sog(meta)
        lod = self.directory / "lod" / "lod-meta.json"
        self.edit(lod, lambda m: m.update(environment="../sh3/meta.json"))
        with self.assertRaisesRegex(DecodeError, "unsafe"):
            sog.load_scene(lod)

    def test_count_mismatches_are_rejected(self) -> None:
        lod = self.directory / "lod" / "lod-meta.json"
        self.edit(lod, lambda m: m.update(counts=[2000, 400]))
        with self.assertRaisesRegex(DecodeError, "lists 2000 splats"):
            sog.load_scene(lod)
        self.edit(lod, lambda m: m.update(counts=[2100, 400]))
        self.edit(self.directory / "lod" / "0_1" / "meta.json", lambda m: m.update(count=1))
        with self.assertRaisesRegex(DecodeError, "holds 1 splats"):
            sog.load_scene(lod)

    def test_gzip_manifests_decode(self) -> None:
        meta = self.directory / "sh3" / "meta.json"
        meta.write_bytes(gzip.compress(meta.read_bytes()))
        self.assertEqual(sog.decode_sog(meta).count, 300)


def synthetic_sog(directory: Path, textures: dict[str, np.ndarray], **meta: object) -> tuple[Path, sog.ImageReader]:
    """A meta.json whose textures come from memory instead of WebP files."""
    base = {
        "version": 2,
        "count": 2,
        "means": {"mins": [0, 0, 0], "maxs": [0, 0, 0], "files": ["means_l", "means_u"]},
        "scales": {"codebook": [0.0] * 256, "files": ["scales"]},
        "quats": {"files": ["quats"]},
        "sh0": {"codebook": [0.0] * 256, "files": ["sh0"]},
    }
    base.update(meta)
    path = directory / "meta.json"
    path.write_text(json.dumps(base))
    zeros = np.zeros((1, 2, 4), dtype=np.uint8)
    return path, lambda texture: textures.get(texture.name, zeros)


class TexelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory)

    def test_quaternion_tags(self) -> None:
        # Tag 252 + i: component i is largest and rebuilt; 128 encodes ~0; other tags mean identity.
        quats = np.array([[[128, 128, 128, 254], [128, 128, 128, 7]]], dtype=np.uint8)
        path, reader = synthetic_sog(self.directory, {"quats": quats})
        rotation = np.asarray(sog.decode_sog(path, reader).rotation).reshape(2, 4)
        np.testing.assert_allclose(rotation, [[0, 0, 1, 0], [1, 0, 0, 0]], atol=4e-3)

    def test_opacity_scale_and_colour(self) -> None:
        scales = np.array([[[1, 2, 3, 255], [0, 0, 0, 255]]], dtype=np.uint8)
        sh0 = np.array([[[1, 2, 3, 51], [0, 0, 0, 255]]], dtype=np.uint8)
        codebook = [float(i) / 10 for i in range(256)]
        path, reader = synthetic_sog(
            self.directory,
            {"scales": scales, "sh0": sh0},
            scales={"codebook": codebook, "files": ["scales"]},
            sh0={"codebook": codebook, "files": ["sh0"]},
        )
        splats = sog.decode_sog(path, reader)
        np.testing.assert_allclose(np.asarray(splats.scale)[:3], np.exp([0.1, 0.2, 0.3]), rtol=1e-6)
        np.testing.assert_allclose(np.asarray(splats.base)[:4], [0.1, 0.2, 0.3, 0.2], rtol=1e-6)

    def test_position_log_transform(self) -> None:
        low = np.array([[[0, 255, 0, 255], [255, 0, 0, 255]]], dtype=np.uint8)
        high = np.array([[[0, 255, 128, 255], [255, 0, 0, 255]]], dtype=np.uint8)
        path, reader = synthetic_sog(
            self.directory,
            {"means_l": low, "means_u": high},
            means={"mins": [-1, -1, -1], "maxs": [1, 1, 1], "files": ["means_l", "means_u"]},
        )
        position = np.asarray(sog.decode_sog(path, reader).position).reshape(2, 3)
        e = np.e - 1
        np.testing.assert_allclose(position[:, :2], [[-e, e], [e, -e]], rtol=1e-6)
        self.assertAlmostEqual(position[0, 2], np.expm1((128 * 256) / 65535 * 2 - 1), places=6)

    def test_out_of_range_labels_get_zero_harmonics(self) -> None:
        centroids = np.full((1, 64 * 3, 4), 255, dtype=np.uint8)
        labels = np.array([[[0, 0, 0, 255], [1, 0, 0, 255]]], dtype=np.uint8)
        codebook = [0.0] * 255 + [0.5]
        path, reader = synthetic_sog(
            self.directory,
            {"centroids": centroids, "labels": labels},
            shN={"count": 1, "bands": 1, "codebook": codebook, "files": ["centroids", "labels"]},
        )
        splats = sog.decode_sog(path, reader)
        self.assertEqual(len(splats.sh), 3)
        for column in splats.sh:
            np.testing.assert_array_equal(column, [0.5, 0.5, 0.5, 0, 0, 0])

    def test_malformed_textures_are_rejected(self) -> None:
        path, reader = synthetic_sog(self.directory, {"quats": np.zeros((1, 1, 4), dtype=np.uint8)})
        with self.assertRaisesRegex(DecodeError, "1 texels; expected at least 2"):
            sog.decode_sog(path, reader)
        path, reader = synthetic_sog(
            self.directory,
            {"centroids": np.zeros((1, 64, 4), dtype=np.uint8)},
            shN={"count": 1, "bands": 3, "codebook": [0.0] * 256, "files": ["centroids", "labels"]},
        )
        with self.assertRaisesRegex(DecodeError, "64 texels wide; expected 960"):
            sog.decode_sog(path, reader)


class WebpTests(unittest.TestCase):
    def test_alpha_is_straight_not_premultiplied(self) -> None:
        pixels = np.random.default_rng(3).integers(0, 256, (5, 7, 4), dtype=np.uint8)
        pixels[..., 3] = np.maximum(pixels[..., 3], 1)  # lossless WebP may drop RGB under alpha 0
        pixels[0, 0] = (200, 100, 50, 3)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "texels.webp"
            spec = oiio.ImageSpec(7, 5, 4, "uint8")
            spec.attribute("compression", "lossless")
            spec.attribute("oiio:UnassociatedAlpha", 1)
            output = oiio.ImageOutput.create(str(path))
            output.open(str(path), spec)
            output.write_image(pixels)
            output.close()
            np.testing.assert_array_equal(sog.read_webp(path), pixels)

    def test_unreadable_file_is_a_decode_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "broken.webp"
            path.write_bytes(b"RIFF\x00\x00\x00\x00WEBPjunk")
            with self.assertRaises(DecodeError):
                sog.read_webp(path)


class FakeHttps(urllib.request.HTTPSHandler):
    """Answers every https request with one canned response, without a socket."""

    def __init__(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
        super().__init__()
        self.status, self.body, self.headers = status, body, headers or {}
        self.requests: list[str] = []

    def https_open(self, request: urllib.request.Request) -> urllib.response.addinfourl:
        self.requests.append(request.full_url)
        headers = Message()
        for name, value in self.headers.items():
            headers[name] = value
        response = urllib.response.addinfourl(io.BytesIO(self.body), headers, request.full_url, self.status)
        response.msg = "Fake"
        return response


class HttpDownloadTests(unittest.TestCase):
    URL = "https://storage.example.test/a1b2c3d4/archive.zip?X-Amz-Signature=secret"

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "download.zip"

    def test_streams_to_a_file_with_progress(self) -> None:
        body = bytes(range(256)) * 9000  # just over 2 MiB, more than one read
        seen: list[int] = []
        server = FakeHttps(200, body)
        size = sog.http_download(self.URL, self.path, len(body), on_bytes=seen.append, handlers=[server])
        self.assertEqual((size, self.path.read_bytes(), sum(seen)), (len(body), body, len(body)))
        self.assertGreater(len(seen), 1)
        self.assertEqual(server.requests, [self.URL])

    def test_redirects_are_refused(self) -> None:
        server = FakeHttps(302, headers={"Location": "https://elsewhere.example.test/archive.zip"})
        with self.assertRaisesRegex(DecodeError, "redirected \\(HTTP 302\\); redirects are refused"):
            sog.http_download(self.URL, self.path, 1024, handlers=[server])
        self.assertEqual(server.requests, [self.URL])

    def test_declared_length_over_the_limit(self) -> None:
        declared = FakeHttps(200, b"small", headers={"Content-Length": str(10**12)})
        with self.assertRaises(sog.ResponseTooLarge):
            sog.http_download(self.URL, self.path, 1024, handlers=[declared])

    def test_messages_leave_out_the_signature(self) -> None:
        for server, message in (
            (FakeHttps(403), "failed with HTTP 403"),
            (FakeHttps(302, headers={"Location": "https://elsewhere.example.test/"}), "redirects are refused"),
            (FakeHttps(200, b"0123456789"), "larger than 9 bytes"),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(DecodeError, message) as raised:
                    sog.http_download(self.URL, self.path, 9, handlers=[server])
                self.assertNotIn("secret", str(raised.exception))
        with self.assertRaises(sog.DownloadHttpError) as raised:
            sog.http_download(self.URL, self.path, 9, handlers=[FakeHttps(404)])
        self.assertEqual(raised.exception.status, 404)

    def test_short_bodies_are_errors(self) -> None:
        truncated = FakeHttps(200, b"0123", headers={"Content-Length": "10"})
        with self.assertRaisesRegex(DecodeError, "stopped after 4 of 10 bytes"):
            sog.http_download(self.URL, self.path, 100, handlers=[truncated])

    def test_refuses_insecure_urls_and_honours_cancel(self) -> None:
        with self.assertRaisesRegex(DecodeError, "only be downloaded over https"):
            sog.http_download("http://storage.example.test/a.zip", self.path, 10)
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(UploadCancelled):
            sog.http_download(self.URL, self.path, 10, cancel_event=cancel, handlers=[FakeHttps(200, b"data")])


if __name__ == "__main__":
    unittest.main()
