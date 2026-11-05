from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

try:
    import numpy  # noqa: F401
except ImportError:
    raise unittest.SkipTest("needs numpy; run the unit tests with Blender's bundled Python") from None

import guards
from splat_fixtures import blender_import, raw_rows, read_3dgs_ply
from supersplat import ply
from supersplat.errors import DecodeError, ExportError, UploadCancelled
from supersplat.ply import (
    CHUNK_ROWS,
    SplatData,
    degree_for_coefficients,
    ply_header,
    property_names,
    write_3dgs_ply,
)


def setUpModule() -> None:
    guards.block_all()


class PlyWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "out.ply"

    def test_round_trip_inverts_blender_import_mapping(self) -> None:
        for degree in (0, 1, 2, 3):
            with self.subTest(degree=degree):
                self.assert_round_trip(degree)

    def assert_round_trip(self, degree: int) -> None:
        rows = raw_rows(64, degree)
        size = write_3dgs_ply(self.path, blender_import(rows, degree))
        names = property_names(degree)
        self.assertEqual(size, len(ply_header(64, degree)) + 64 * 4 * len(names))
        parsed = read_3dgs_ply(self.path)
        self.assertEqual(parsed.count, 64)
        self.assertEqual(parsed.names, names)
        for name in names:
            for original, written in zip((row[name] for row in rows), parsed.column(name)):
                self.assertAlmostEqual(original, written, places=4, msg=name)

    def test_header_is_standard_3dgs_layout(self) -> None:
        header = ply_header(5, 1).decode("ascii").splitlines()
        self.assertEqual(header[:3], ["ply", "format binary_little_endian 1.0", "element vertex 5"])
        self.assertEqual(header[-1], "end_header")
        self.assertEqual(
            [line.split()[-1] for line in header[3:-1]],
            ["x", "y", "z", "f_dc_0", "f_dc_1", "f_dc_2"]
            + [f"f_rest_{i}" for i in range(9)]
            + ["opacity", "scale_0", "scale_1", "scale_2", "rot_0", "rot_1", "rot_2", "rot_3"],
        )

    def test_requested_degree_is_clamped_to_available(self) -> None:
        rows = raw_rows(4, 1)
        write_3dgs_ply(self.path, blender_import(rows, 1), sh_degree=3)
        self.assertEqual(len(read_3dgs_ply(self.path).names), len(property_names(1)))
        write_3dgs_ply(self.path, blender_import(rows, 1), sh_degree=0)
        self.assertEqual(read_3dgs_ply(self.path).names, property_names(0))

    def test_degree_from_coefficient_count(self) -> None:
        self.assertEqual(
            [degree_for_coefficients(n) for n in (0, 2, 3, 7, 8, 14, 15, 20)],
            [0, 0, 1, 1, 2, 2, 3, 3],
        )

    def test_extreme_alpha_and_zero_scale_stay_finite(self) -> None:
        data = SplatData(2, [0.0] * 6, [0, 0, 0, 0.0, 0, 0, 0, 1.0], [0.0] * 6, [1, 0, 0, 0] * 2)
        write_3dgs_ply(self.path, data)
        opacity = read_3dgs_ply(self.path).column("opacity")
        self.assertAlmostEqual(opacity[0], -13.8155, places=3)
        self.assertAlmostEqual(opacity[1], 13.8155, places=3)
        self.assertTrue(all(abs(v) < 100 for v in read_3dgs_ply(self.path).column("scale_0")))

    def test_invalid_columns_are_export_errors(self) -> None:
        with self.assertRaises(ExportError):
            write_3dgs_ply(self.path, SplatData(0, [], [], [], []))
        with self.assertRaises(ExportError):
            write_3dgs_ply(self.path, SplatData(1, [0.0] * 3, [0.0] * 3, [0.0] * 3, [1, 0, 0, 0]))
        self.assertFalse(self.path.exists())

    def test_cancel_removes_partial_file(self) -> None:
        count = CHUNK_ROWS + 1
        data = SplatData(count, [0.0] * 3 * count, [0.5] * 4 * count, [1.0] * 3 * count, [1.0, 0, 0, 0] * count)
        cancel = threading.Event()
        with self.assertRaises(UploadCancelled):
            write_3dgs_ply(self.path, data, cancel_event=cancel, on_progress=lambda _f: cancel.set())
        self.assertFalse(self.path.exists())


class PlyReaderTests(unittest.TestCase):
    """supersplat.ply.read_3dgs_ply against Blender's import mapping (splat_fixtures.blender_import)."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "in.ply"

    def write(self, header: list[str], body: bytes) -> Path:
        self.path.write_bytes(("\n".join(["ply", *header, "end_header"]) + "\n").encode("ascii") + body)
        return self.path

    def assert_matches(self, ours: SplatData, reference: SplatData) -> None:
        self.assertEqual(ours.count, reference.count)
        self.assertEqual(len(ours.sh), len(reference.sh))
        for name in ("position", "base", "scale", "rotation"):
            numpy.testing.assert_allclose(getattr(ours, name), getattr(reference, name), rtol=1e-6, atol=1e-6, err_msg=name)
        for a, b in zip(ours.sh, reference.sh):
            numpy.testing.assert_allclose(a, b, atol=1e-7)

    def test_reads_every_degree_like_blender(self) -> None:
        for degree in (0, 1, 2, 3):
            with self.subTest(degree=degree):
                rows = raw_rows(40, degree)
                from splat_fixtures import write_raw_ply

                write_raw_ply(self.path, rows, degree)
                self.assert_matches(ply.read_3dgs_ply(self.path), blender_import(rows, degree))

    def test_any_property_order_type_and_byte_order(self) -> None:
        rows = raw_rows(10, 1)
        names = property_names(1)[::-1]
        for order, fmt in (("<", "binary_little_endian"), (">", "binary_big_endian")):
            with self.subTest(fmt=fmt):
                dtype = numpy.dtype([(name, order + ("f8" if name.startswith("f_rest") else "f4")) for name in names])
                body = numpy.array([tuple(row[name] for name in names) for row in rows], dtype=dtype).tobytes()
                header = [f"format {fmt} 1.0", "comment made by a test", "element vertex 10"]
                header += [f"property {'double' if n.startswith('f_rest') else 'float'} {n}" for n in names]
                self.assert_matches(ply.read_3dgs_ply(self.write(header, body)), blender_import(rows, 1))

    def test_skips_elements_before_the_vertices(self) -> None:
        rows = raw_rows(5, 0)
        names = property_names(0)
        body = b"\x07" * 3 * 4 + numpy.array([tuple(r[n] for n in names) for r in rows], dtype="<f4").tobytes()
        header = ["format binary_little_endian 1.0", "element chunk 3", "property uint packed"]
        header += ["element vertex 5"] + [f"property float {n}" for n in names]
        self.assert_matches(ply.read_3dgs_ply(self.write(header, body)), blender_import(rows, 0))

    def test_zero_quaternions_become_identity(self) -> None:
        rows = raw_rows(2, 0)
        for axis in range(4):
            rows[0][f"rot_{axis}"] = 0.0
        from splat_fixtures import write_raw_ply

        write_raw_ply(self.path, rows, 0)
        self.assertEqual(list(ply.read_3dgs_ply(self.path).rotation[:4]), [1.0, 0.0, 0.0, 0.0])

    def test_rejects_files_that_are_not_binary_splat_plys(self) -> None:
        cases = {
            "not a PLY": (b"hello", None),
            "only binary PLY": (None, ["format ascii 1.0", "element vertex 1", "property float x"]),
            "lacks": (None, ["format binary_little_endian 1.0", "element vertex 1", "property float x"]),
            "no vertex element": (None, ["format binary_little_endian 1.0", "element face 1", "property float x"]),
            "list properties": (None, ["format binary_little_endian 1.0", "element vertex 1", "property list uchar int idx"]),
            "unsupported PLY header line": (None, ["format binary_little_endian 1.0", "element vertex 1", "property half x"]),
        }
        for message, (raw, header) in cases.items():
            with self.subTest(message=message):
                if raw is not None:
                    self.path.write_bytes(raw)
                else:
                    self.write(header, b"")
                with self.assertRaisesRegex(DecodeError, message):
                    ply.read_3dgs_ply(self.path)

    def test_truncated_file(self) -> None:
        self.path.write_bytes(ply_header(10, 0) + b"\x00" * 100)
        with self.assertRaisesRegex(DecodeError, "truncated"):
            ply.read_3dgs_ply(self.path)

    def test_cancel_and_progress(self) -> None:
        from splat_fixtures import write_raw_ply

        write_raw_ply(self.path, raw_rows(4, 0), 0)
        progress: list[float] = []
        ply.read_3dgs_ply(self.path, on_progress=progress.append)
        self.assertEqual(progress, [1.0])
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(UploadCancelled):
            ply.read_3dgs_ply(self.path, cancel_event=cancel)


if __name__ == "__main__":
    unittest.main()
