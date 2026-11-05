"""Convert Blender world space to the 3DGS PLY coordinate convention.

Blender is Z-up and right-handed; its front view looks along +Y. 3DGS PLY
files use the convention of the original training code (Y-down). SuperSplat
displays them rotated 180 degrees about Z, which turns that into Y-up.

BLENDER_TO_3DGS maps a Blender world-space vector into the file:

    file = (-x, -z, -y)

After SuperSplat's flip, Blender's +Z is up and Blender's front view faces
SuperSplat's default camera. The matrix is a proper rotation (det +1; 180
degrees about (0, 1, -1)/sqrt(2)), so nothing is mirrored.

The object's world matrix is applied first, then BLENDER_TO_3DGS:

    position  file = M (s R p + t)
    rotation  q_file = q_M * q_R * q        (w, x, y, z)
    scale     file = s * scale              (each splat's own frame)
    SH        each band rotated by M R

R, s and t are the object's rotation, uniform scale and translation. A splat
is an ellipsoid with one rotation and three axis scales, so non-uniform object
scale, shear and mirroring cannot be expressed per splat and are rejected.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np

from .errors import ExportError
from .ply import SplatData

Matrix3 = tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]

BLENDER_TO_3DGS: Matrix3 = (
    (-1.0, 0.0, 0.0),
    (0.0, 0.0, -1.0),
    (0.0, -1.0, 0.0),
)
IDENTITY: Matrix3 = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))

NON_UNIFORM_SCALE_MESSAGE = (
    "The object has a non-uniform scale or shear, which Gaussian splats cannot represent. "
    "Use the same scale on all three axes to publish."
)
MIRRORED_MESSAGE = (
    "The object has a negative or zero scale (mirrored or flattened), which Gaussian splats "
    "cannot represent. Use a positive scale to publish."
)
_ORTHONORMAL_TOLERANCE = 1e-4


def quaternion_from_matrix(r: Sequence[Sequence[float]]) -> tuple[float, float, float, float]:
    """Unit quaternion (w, x, y, z) of a proper rotation matrix, with w >= 0."""
    trace = r[0][0] + r[1][1] + r[2][2]
    if trace > 0:
        s = math.sqrt(trace + 1.0) * 2
        q = (0.25 * s, (r[2][1] - r[1][2]) / s, (r[0][2] - r[2][0]) / s, (r[1][0] - r[0][1]) / s)
    elif r[0][0] > r[1][1] and r[0][0] > r[2][2]:
        s = math.sqrt(1.0 + r[0][0] - r[1][1] - r[2][2]) * 2
        q = ((r[2][1] - r[1][2]) / s, 0.25 * s, (r[0][1] + r[1][0]) / s, (r[0][2] + r[2][0]) / s)
    elif r[1][1] > r[2][2]:
        s = math.sqrt(1.0 + r[1][1] - r[0][0] - r[2][2]) * 2
        q = ((r[0][2] - r[2][0]) / s, (r[0][1] + r[1][0]) / s, 0.25 * s, (r[1][2] + r[2][1]) / s)
    else:
        s = math.sqrt(1.0 + r[2][2] - r[0][0] - r[1][1]) * 2
        q = ((r[1][0] - r[0][1]) / s, (r[0][2] + r[2][0]) / s, (r[1][2] + r[2][1]) / s, 0.25 * s)
    norm = math.sqrt(sum(c * c for c in q))
    sign = -1.0 if q[0] < 0 else 1.0
    return tuple(sign * c / norm for c in q)  # type: ignore[return-value]


# -- spherical harmonics ----------------------------------------------------------

# Real SH basis exactly as evaluated by the 3DGS reference renderer; f_rest
# coefficient k of each colour channel multiplies basis function k + 1.
_C1 = 0.4886025119029199
_C2 = (1.0925484305920792, -1.0925484305920792, 0.31539156525252005, -1.0925484305920792, 0.5462742152960396)
_C3 = (
    -0.5900435899266435, 2.890611442640554, -0.4570457994644658, 0.3731763325901154,
    -0.4570457994644658, 1.445305721320277, -0.5900435899266435,
)


def sh_basis(degree: int, d: np.ndarray) -> np.ndarray:
    """The 3DGS real SH basis for one band (degree 1-3) at unit directions d (..., 3)."""
    x, y, z = np.moveaxis(np.asarray(d, dtype=np.float64), -1, 0)
    if degree == 1:
        values = [-_C1 * y, _C1 * z, -_C1 * x]
    elif degree == 2:
        xx, yy, zz = x * x, y * y, z * z
        values = [
            _C2[0] * x * y,
            _C2[1] * y * z,
            _C2[2] * (2 * zz - xx - yy),
            _C2[3] * x * z,
            _C2[4] * (xx - yy),
        ]
    elif degree == 3:
        xx, yy, zz = x * x, y * y, z * z
        values = [
            _C3[0] * y * (3 * xx - yy),
            _C3[1] * x * y * z,
            _C3[2] * y * (4 * zz - xx - yy),
            _C3[3] * z * (2 * zz - 3 * xx - 3 * yy),
            _C3[4] * x * (4 * zz - xx - yy),
            _C3[5] * z * (xx - yy),
            _C3[6] * x * (xx - 3 * yy),
        ]
    else:
        raise ValueError(f"Unsupported SH band {degree}.")
    return np.stack(values, axis=-1)


def _sample_directions(count: int = 64) -> np.ndarray:
    """Deterministic, well-spread unit directions (Fibonacci sphere)."""
    i = np.arange(count)
    z = 1.0 - 2.0 * (i + 0.5) / count
    radius = np.sqrt(1.0 - z * z)
    angle = math.pi * (3.0 - math.sqrt(5.0)) * i
    return np.stack([radius * np.cos(angle), radius * np.sin(angle), z], axis=1)


def sh_band_rotation(degree: int, rotation: Sequence[Sequence[float]]) -> np.ndarray:
    """Matrix X with coefficients_new = X @ coefficients_old for one SH band.

    The rotated signal must satisfy f_new(R d) = f_old(d). Each band is closed
    under rotation, so Y(R^T d) = D Y(d); X = D^T is fitted by least squares
    over sample directions (exact up to float rounding).
    """
    directions = _sample_directions()
    a = sh_basis(degree, directions)
    # Rows of `directions @ R` are R^T d.
    b = sh_basis(degree, directions @ np.asarray(rotation, dtype=np.float64))
    return np.linalg.lstsq(a, b, rcond=None)[0]


# -- splat transform --------------------------------------------------------------


@dataclass(frozen=True)
class ExportTransform:
    """Similarity transform applied to every splat: file = s R p + t."""

    rotation: Matrix3 = IDENTITY
    scale: float = 1.0
    translation: tuple[float, float, float] = (0.0, 0.0, 0.0)

    @property
    def is_identity(self) -> bool:
        return self.rotation == IDENTITY and self.scale == 1.0 and self.translation == (0.0, 0.0, 0.0)


def export_transform(
    matrix_world: Sequence[Sequence[float]] | None = None, *, convert_axes: bool = True
) -> ExportTransform:
    """Split a 4x4 (or 3x4) object matrix into rotation, uniform scale and translation,
    then prepend BLENDER_TO_3DGS when `convert_axes` is set."""
    matrix = np.eye(4) if matrix_world is None else np.asarray(matrix_world, dtype=np.float64)
    linear, translation = matrix[:3, :3], matrix[:3, 3]
    det = np.linalg.det(linear)
    if not det > 0:
        raise ExportError(MIRRORED_MESSAGE)
    scale = float(det ** (1.0 / 3.0))
    rotation = linear / scale
    if not np.allclose(rotation.T @ rotation, np.eye(3), rtol=0, atol=_ORTHONORMAL_TOLERANCE):
        raise ExportError(NON_UNIFORM_SCALE_MESSAGE)
    if abs(scale - 1.0) < 1e-9:
        scale = 1.0
    if convert_axes:
        rotation = np.asarray(BLENDER_TO_3DGS) @ rotation
        translation = np.asarray(BLENDER_TO_3DGS) @ translation
    return ExportTransform(
        tuple(tuple(float(v) for v in row) for row in rotation),  # type: ignore[arg-type]
        scale,
        tuple(float(v) for v in translation),  # type: ignore[arg-type]
    )


def transform_splats(data: SplatData, transform: ExportTransform) -> SplatData:
    """Return a copy of `data` with `transform` applied (data is not modified)."""
    if transform.is_identity:
        return data

    def f64(values: Sequence[float], width: int) -> np.ndarray:
        return np.asarray(values, dtype=np.float64).reshape(-1, width)

    def f32(values: np.ndarray) -> np.ndarray:
        return values.astype(np.float32).reshape(-1)

    rotation = np.asarray(transform.rotation)
    position = transform.scale * f64(data.position, 3) @ rotation.T + np.asarray(transform.translation)
    w, x, y, z = quaternion_from_matrix(transform.rotation)
    r = f64(data.rotation, 4)
    rotations = np.stack(
        [
            w * r[:, 0] - x * r[:, 1] - y * r[:, 2] - z * r[:, 3],
            w * r[:, 1] + x * r[:, 0] + y * r[:, 3] - z * r[:, 2],
            w * r[:, 2] - x * r[:, 3] + y * r[:, 0] + z * r[:, 1],
            w * r[:, 3] + x * r[:, 2] - y * r[:, 1] + z * r[:, 0],
        ],
        axis=1,
    )
    sh = list(data.sh)
    for degree in range(1, data.sh_degree + 1):
        indices = range(degree * degree - 1, (degree + 1) ** 2 - 1)
        old = np.stack([f64(data.sh[k], 3) for k in indices])  # (band, points, rgb)
        new = np.einsum("ij,jpc->ipc", sh_band_rotation(degree, rotation), old)
        for row, k in enumerate(indices):
            sh[k] = f32(new[row])
    return SplatData(
        data.count, f32(position), data.base, f32(transform.scale * f64(data.scale, 3)), f32(rotations), sh
    )
