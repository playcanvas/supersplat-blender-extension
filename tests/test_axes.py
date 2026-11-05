from __future__ import annotations

import math
import random
import unittest

try:
    import numpy as np
except ImportError:
    raise unittest.SkipTest("needs numpy; run the unit tests with Blender's bundled Python") from None

import guards
from supersplat import axes
from supersplat.axes import (
    BLENDER_TO_3DGS,
    MIRRORED_MESSAGE,
    NON_UNIFORM_SCALE_MESSAGE,
    ExportTransform,
    export_transform,
    transform_splats,
)
from supersplat.errors import ExportError
from supersplat.ply import SplatData


def setUpModule() -> None:
    guards.block_all()


def markers(*positions: tuple[float, float, float], sh_degree: int = 0) -> SplatData:
    count = len(positions)
    sh_columns = (sh_degree + 1) ** 2 - 1
    return SplatData(
        count=count,
        position=[v for p in positions for v in p],
        base=[0.5, 0.5, 0.5, 0.9] * count,
        scale=[0.1, 0.2, 0.3] * count,
        rotation=[1.0, 0.0, 0.0, 0.0] * count,
        sh=[[0.0] * (3 * count) for _ in range(sh_columns)],
    )


def matrix_from_quaternion(q) -> list[list[float]]:  # noqa: ANN001
    w, x, y, z = q
    return [
        [1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
        [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
        [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)],
    ]


M = np.array(BLENDER_TO_3DGS)
BLENDER_TO_3DGS_QUATERNION = axes.quaternion_from_matrix(BLENDER_TO_3DGS)


def quaternion_multiply(a, b) -> tuple[float, float, float, float]:  # noqa: ANN001
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return (
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    )


def unit_quaternion(rng: random.Random) -> tuple[float, float, float, float]:
    q = [rng.gauss(0, 1) for _ in range(4)]
    norm = math.sqrt(sum(c * c for c in q))
    return tuple(c / norm for c in q)  # type: ignore[return-value]


def unit_vector(rng: random.Random) -> tuple[float, float, float]:
    v = [rng.gauss(0, 1) for _ in range(3)]
    norm = math.sqrt(sum(c * c for c in v))
    return tuple(c / norm for c in v)  # type: ignore[return-value]


def matrix4(rotation, scale: float, translation) -> list[list[float]]:  # noqa: ANN001
    rows = [[scale * v for v in row] + [t] for row, t in zip(rotation, translation)]
    return rows + [[0.0, 0.0, 0.0, 1.0]]


class ConventionTests(unittest.TestCase):
    def assertVectorAlmostEqual(self, actual, expected, places: int = 6) -> None:  # noqa: ANN001
        self.assertEqual(len(actual), len(expected))
        for a, e in zip(actual, expected):
            self.assertAlmostEqual(a, e, places=places)

    def test_matrix_is_a_proper_rotation(self) -> None:
        self.assertAlmostEqual(np.linalg.det(M), 1.0)
        np.testing.assert_array_equal(M.T @ M, np.eye(3))

    def test_markers_export_to_the_3dgs_axes(self) -> None:
        data = markers((1, 0, 0), (0, 1, 0), (0, 0, 1))
        out = transform_splats(data, export_transform())
        self.assertVectorAlmostEqual(out.position[0:3], (-1, 0, 0))  # +X -> file -X
        self.assertVectorAlmostEqual(out.position[3:6], (0, 0, -1))  # +Y -> file -Z
        self.assertVectorAlmostEqual(out.position[6:9], (0, -1, 0))  # +Z (up) -> file -Y

    def test_conversion_does_not_mirror(self) -> None:
        x, y, z = M.T  # images of Blender's +X, +Y, +Z
        np.testing.assert_array_equal(np.cross(x, y), z)

    def test_quaternion_matches_the_matrix(self) -> None:
        matrix = matrix_from_quaternion(BLENDER_TO_3DGS_QUATERNION)
        for row, expected in zip(matrix, BLENDER_TO_3DGS):
            self.assertVectorAlmostEqual(row, expected, places=12)

    def test_rotations_compose_and_round_trip_with_the_inverse(self) -> None:
        rng = random.Random(7)
        rotations = [unit_quaternion(rng) for _ in range(8)]
        data = markers(*[(0.0, 0.0, 0.0)] * 8)
        data.rotation = [c for q in rotations for c in q]
        out = transform_splats(data, export_transform())
        w, x, y, z = BLENDER_TO_3DGS_QUATERNION
        inverse = (w, -x, -y, -z)
        for i, original in enumerate(rotations):
            exported = out.rotation[4 * i : 4 * i + 4]
            self.assertAlmostEqual(math.sqrt(sum(c * c for c in exported)), 1.0, places=6)
            # The splat's frame is rotated by M: R(q_file) = M R(q).
            expected = M @ np.array(matrix_from_quaternion(original))
            for row, want in zip(matrix_from_quaternion(exported), expected):
                self.assertVectorAlmostEqual(row, want, places=6)
            restored = quaternion_multiply(inverse, exported)
            self.assertVectorAlmostEqual(restored, original, places=6)

    def test_scales_and_colours_are_unchanged(self) -> None:
        data = markers((1, 2, 3))
        out = transform_splats(data, export_transform())
        self.assertVectorAlmostEqual(out.scale, data.scale)
        self.assertEqual(list(out.base), list(data.base))

    def test_sh_is_rotated_with_the_scene(self) -> None:
        # Colour seen from file direction M d must equal the Blender colour seen from d.
        rng = random.Random(11)
        data = markers((0, 0, 0), sh_degree=3)
        data.sh = [[rng.gauss(0, 1) for _ in range(3)] for _ in range(15)]
        out = transform_splats(data, export_transform())
        for _ in range(20):
            d = unit_vector(rng)
            for channel in range(3):
                before = after = 0.0
                for degree in (1, 2, 3):
                    first = degree * degree - 1
                    basis_before = axes.sh_basis(degree, d)
                    basis_after = axes.sh_basis(degree, M @ np.array(d))
                    for offset in range(2 * degree + 1):
                        before += data.sh[first + offset][channel] * basis_before[offset]
                        after += out.sh[first + offset][channel] * basis_after[offset]
                self.assertAlmostEqual(before, after, places=5)

    def test_sh_rotation_preserves_band_energy(self) -> None:
        rng = random.Random(3)
        data = markers((0, 0, 0), sh_degree=2)
        data.sh = [[rng.gauss(0, 1) for _ in range(3)] for _ in range(8)]
        out = transform_splats(data, export_transform())
        for indices in (range(0, 3), range(3, 8)):
            for channel in range(3):
                energy = sum(data.sh[k][channel] ** 2 for k in indices)
                self.assertAlmostEqual(energy, sum(out.sh[k][channel] ** 2 for k in indices), places=5)

    def test_input_is_not_modified(self) -> None:
        data = markers((1, 2, 3))
        transform_splats(data, export_transform())
        self.assertEqual(list(data.position), [1, 2, 3])


class WorldMatrixTests(unittest.TestCase):
    def test_world_matrix_is_applied_before_the_conversion(self) -> None:
        quarter_turn_z = ((0.0, -1.0, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, 1.0))
        transform = export_transform(matrix4(quarter_turn_z, 2.0, (10.0, 20.0, 30.0)))
        out = transform_splats(markers((1, 0, 0)), transform)
        # World: 2 * (+X turned to +Y) + t = (10, 22, 30); file = (-x, -z, -y).
        for actual, expected in zip(out.position, (-10.0, -30.0, -22.0)):
            self.assertAlmostEqual(actual, expected, places=6)
        for actual, expected in zip(out.scale, (0.2, 0.4, 0.6)):
            self.assertAlmostEqual(actual, expected, places=6)

    def test_convert_axes_off_applies_only_the_world_matrix(self) -> None:
        transform = export_transform(matrix4(axes.IDENTITY, 1.0, (1.0, 2.0, 3.0)), convert_axes=False)
        out = transform_splats(markers((1, 0, 0)), transform)
        self.assertEqual(list(out.position), [2.0, 2.0, 3.0])

    def test_identity_without_conversion_leaves_data_untouched(self) -> None:
        data = markers((1, 2, 3), sh_degree=1)
        transform = export_transform(matrix4(axes.IDENTITY, 1.0, (0.0, 0.0, 0.0)), convert_axes=False)
        self.assertTrue(transform.is_identity)
        self.assertIs(transform_splats(data, transform), data)
        self.assertTrue(ExportTransform().is_identity)

    def test_non_uniform_scale_is_rejected(self) -> None:
        stretched = matrix4(((2.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)), 1.0, (0, 0, 0))
        with self.assertRaises(ExportError) as raised:
            export_transform(stretched)
        self.assertEqual(str(raised.exception), NON_UNIFORM_SCALE_MESSAGE)

    def test_mirroring_and_zero_scale_are_rejected(self) -> None:
        for diagonal in ((-1.0, 1.0, 1.0), (0.0, 1.0, 1.0)):
            matrix = [[diagonal[i] if i == j else 0.0 for j in range(3)] + [0.0] for i in range(3)]
            with self.subTest(diagonal=diagonal), self.assertRaises(ExportError) as raised:
                export_transform(matrix)
            self.assertEqual(str(raised.exception), MIRRORED_MESSAGE)


if __name__ == "__main__":
    unittest.main()
