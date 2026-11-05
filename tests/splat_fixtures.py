"""Deterministic raw 3DGS rows, Blender's import mapping and a PLY reader, for round-trip tests."""

from __future__ import annotations

import math
import random
import struct
from dataclasses import dataclass
from pathlib import Path

from supersplat.ply import SplatData, coefficients_for_degree, ply_header, property_names


def raw_rows(count: int, sh_degree: int, seed: int = 7) -> list[dict[str, float]]:
    """Plausible 3DGS PLY values (log scales, logit opacity, unit quaternions)."""
    rng = random.Random(seed)
    rows = []
    for index in range(count):
        angle = 2 * math.pi * index / max(count, 1)
        row = {
            "x": math.cos(angle) * (1 + 0.1 * (index % 4)),
            "y": math.sin(angle) * (1 + 0.1 * (index % 4)),
            "z": 0.05 * (index % 8) - 0.2,
            "f_dc_0": rng.uniform(-1.5, 1.5),
            "f_dc_1": rng.uniform(-1.5, 1.5),
            "f_dc_2": rng.uniform(-1.5, 1.5),
            "opacity": rng.uniform(-4.0, 6.0),
            "scale_0": rng.uniform(-5.0, -2.0),
            "scale_1": rng.uniform(-5.0, -2.0),
            "scale_2": rng.uniform(-6.0, -3.0),
        }
        quat = [rng.gauss(0, 1) for _ in range(4)]
        norm = math.sqrt(sum(q * q for q in quat))
        for axis in range(4):
            row[f"rot_{axis}"] = quat[axis] / norm
        for rest in range(3 * coefficients_for_degree(sh_degree)):
            row[f"f_rest_{rest}"] = rng.uniform(-0.3, 0.3)
        rows.append(row)
    return rows


def blender_import(rows: list[dict[str, float]], sh_degree: int) -> SplatData:
    """Apply the attribute mapping observed in Blender 5.3's PLY importer."""
    rest = coefficients_for_degree(sh_degree)
    position, base, scale, rotation = [], [], [], []
    sh: list[list[float]] = [[] for _ in range(rest)]
    for row in rows:
        position += [row["x"], row["y"], row["z"]]
        base += [row["f_dc_0"], row["f_dc_1"], row["f_dc_2"], 1 / (1 + math.exp(-row["opacity"]))]
        scale += [math.exp(row[f"scale_{axis}"]) for axis in range(3)]
        rotation += [row[f"rot_{axis}"] for axis in range(4)]
        for k in range(rest):
            sh[k] += [row[f"f_rest_{k + channel * rest}"] for channel in range(3)]
    return SplatData(len(rows), position, base, scale, rotation, sh)


def write_raw_ply(path: str | Path, rows: list[dict[str, float]], sh_degree: int) -> None:
    """Write raw 3DGS rows as a PLY, as a trainer would."""
    names = property_names(sh_degree)
    row = struct.Struct(f"<{len(names)}f")
    body = b"".join(row.pack(*(values[name] for name in names)) for values in rows)
    Path(path).write_bytes(ply_header(len(rows), sh_degree) + body)


@dataclass
class PlyVertices:
    count: int
    names: list[str]
    rows: list[tuple[float, ...]]

    def column(self, name: str) -> list[float]:
        index = self.names.index(name)
        return [row[index] for row in self.rows]


def read_3dgs_ply(path: str | Path) -> PlyVertices:
    """Read a binary little-endian PLY whose only element is float vertices."""
    raw = Path(path).read_bytes()
    header, body = raw.split(b"end_header\n", 1)
    lines = header.decode("ascii").splitlines()
    assert lines[:2] == ["ply", "format binary_little_endian 1.0"], lines[:2]
    count = next(int(line.split()[2]) for line in lines if line.startswith("element vertex "))
    names = [line.split()[2] for line in lines if line.startswith("property float ")]
    assert len(names) == sum(line.startswith("property ") for line in lines), "non-float property"
    row = struct.Struct(f"<{len(names)}f")
    assert len(body) == count * row.size, f"PLY body is {len(body)} bytes"
    return PlyVertices(count, names, list(row.iter_unpack(body)))


def ply_as_blender(path: str | Path) -> SplatData:
    """Read a 3DGS PLY (any property order) through Blender's import mapping."""
    vertices = read_3dgs_ply(path)
    rows = [dict(zip(vertices.names, row)) for row in vertices.rows]
    rest = sum(name.startswith("f_rest_") for name in vertices.names) // 3
    degree = next(d for d in (3, 2, 1, 0) if coefficients_for_degree(d) <= rest)
    return blender_import(rows, degree)
