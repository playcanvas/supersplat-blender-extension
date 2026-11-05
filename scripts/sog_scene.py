"""Import a local SuperSplat scene folder (SOG or SSOG) into Blender and measure it.

Run inside Blender 5.3+:

    blender --background --factory-startup \\
        --python scripts/sog_scene.py -- FOLDER [options]

FOLDER holds a SOG meta.json or an SSOG lod-meta.json and the files it lists
(for SSOG, at least those of --level and the environment). It is only read.

--compare also converts the same files to PLY with splat-transform (needs
Node.js), imports that PLY with Blender's importer and reports the largest
attribute differences. --save writes the result to a .blend file.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import bpy
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
SPLAT_TRANSFORM = ["npx", "--yes", "@playcanvas/splat-transform@3.10.3", "--quiet", "--overwrite"]


def load_extension_package():
    """Import the repository as a package without installing or registering it."""
    spec = importlib.util.spec_from_file_location(
        "supersplat_dev", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    package = importlib.util.module_from_spec(spec)
    sys.modules["supersplat_dev"] = package
    spec.loader.exec_module(package)
    return (
        importlib.import_module("supersplat_dev.blender_splat"),
        importlib.import_module("supersplat_dev.supersplat.sog"),
        importlib.import_module("supersplat_dev.supersplat.ply"),
    )


blender_splat, sog, ply = load_extension_package()


def parse_args() -> argparse.Namespace:
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser(prog="sog_scene.py")
    parser.add_argument("folder", type=Path)
    parser.add_argument("--level", type=int, default=0)
    parser.add_argument("--no-environment", action="store_true")
    parser.add_argument("--compare", action="store_true")
    parser.add_argument("--save", type=Path)
    return parser.parse_args(argv)


def peak_rss_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


def ply_size(data) -> int:
    degree = data.sh_degree
    return len(ply.ply_header(data.count, degree)) + data.count * 4 * len(ply.property_names(degree))


def selected_files(manifest: Path, level: int, environment: bool) -> list[Path]:
    """Files a decode of `level` reads (manifests and textures)."""
    def sog_files(meta_path: Path) -> list[Path]:
        meta = json.loads(meta_path.read_bytes())
        return [meta_path] + [meta_path.parent / str(name) for name in sog._sog_files(meta, meta_path.name)]

    if manifest.name == sog.META:
        return sog_files(manifest)
    lod = json.loads(manifest.read_bytes())
    files = [manifest]
    for relative, _ in sog._level_files(lod, level, manifest.name):
        files += sog_files(manifest.parent / str(relative))
    if environment and lod.get("environment"):
        files += sog_files(manifest.parent / lod["environment"])
    return files


def level_only_manifest(manifest: Path, level: int, work: Path) -> Path:
    """A lod-meta.json in `work` holding only `level`, so splat-transform reads
    just that level's files (it otherwise opens files of other levels too).
    Its file names point back into the scene folder, which is left untouched."""
    lod = json.loads(manifest.read_bytes())
    files = sog._level_files(lod, level, manifest.name)
    index = {str(path): i for i, (path, _) in enumerate(files)}

    def trim(node: dict) -> dict:
        out = {"bound": node.get("bound")}
        ref = (node.get("lods") or {}).get(str(level))
        if ref is not None:
            out["lods"] = {"0": {**ref, "file": index[lod["filenames"][ref["file"]]]}}
        if node.get("children"):
            out["children"] = [trim(child) for child in node["children"]]
        return out

    trimmed = {
        "version": lod.get("version", 1),
        "lodLevels": 1,
        "counts": [sum(count for _, count in files)],
        "filenames": [os.path.relpath(manifest.parent / str(path), work) for path, _ in files],
        "tree": trim(lod["tree"]),
    }
    path = work / f"level-{level}.lod-meta.json"
    path.write_text(json.dumps(trimmed), encoding="utf-8")
    return path


def compare(obj, reference: Path, label: str) -> dict:
    """Import `reference` with Blender's PLY importer and diff it against `obj`."""
    started = time.perf_counter()
    before = set(bpy.data.objects)
    if bpy.ops.wm.ply_import(filepath=str(reference)) != {"FINISHED"}:
        raise SystemExit(f"PLY import of {reference} failed")
    (imported,) = set(bpy.data.objects) - before
    import_seconds = time.perf_counter() - started
    imported.name = f"{label} (PLY import)"

    def attribute_types(o):
        return {a.name: (a.data_type, a.domain) for a in o.data.attributes}

    depsgraph = bpy.context.evaluated_depsgraph_get()
    ours, theirs = (blender_splat.read_splat(o, depsgraph) for o in (obj, imported))
    columns = {"position": "position", "radiance:base": "base", "scale": "scale", "rotation": "rotation"}
    diffs = {}
    for name, field in columns.items():
        a, b = (np.asarray(getattr(d, field), dtype=np.float64) for d in (ours, theirs))
        diffs[name] = float(np.max(np.abs(a - b))) if a.size else 0.0
    for index, (a, b) in enumerate(zip(ours.sh, theirs.sh)):
        diffs[f"radiance:sh_{index}"] = float(np.max(np.abs(np.asarray(a) - np.asarray(b))))
    result = {
        "count_ours": ours.count,
        "count_ply": theirs.count,
        "attributes_match": attribute_types(obj) == attribute_types(imported),
        "max_abs_diff": diffs,
        "ply_bytes": reference.stat().st_size,
        "ply_import_seconds": round(import_seconds, 2),
    }
    bpy.data.objects.remove(imported, do_unlink=True)
    return result


def main() -> None:
    args = parse_args()
    if not args.folder.is_dir():
        raise SystemExit(f"{args.folder} is not a folder.")
    name = args.folder.resolve().name
    report: dict = {"scene": name, "level": args.level}
    environment = not args.no_environment
    manifest = sog._manifest(args.folder)
    report["file_bytes"] = sum(path.stat().st_size for path in selected_files(manifest, args.level, environment))

    image_seconds = 0.0

    def timed_read(path: Path) -> np.ndarray:
        nonlocal image_seconds
        started = time.perf_counter()
        pixels = sog.read_webp(path)
        image_seconds += time.perf_counter() - started
        return pixels

    started = time.perf_counter()
    scene = sog.load_scene(manifest, args.level, environment=environment, read_image=timed_read)
    report["decode_seconds"] = round(time.perf_counter() - started, 2)
    report["webp_seconds_summed_over_threads"] = round(image_seconds, 2)
    report["level_counts"] = scene.level_counts
    report["count"] = scene.splats.count
    report["sh_degree"] = scene.splats.sh_degree
    report["environment_count"] = scene.environment.count if scene.environment else 0
    report["equivalent_ply_bytes"] = ply_size(scene.splats) + (ply_size(scene.environment) if scene.environment else 0)

    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    started = time.perf_counter()
    main_obj = blender_splat.new_splat_object(f"{name} level {args.level}", scene.splats)
    env_obj = blender_splat.new_splat_object(f"{name} environment", scene.environment) if scene.environment else None
    report["build_seconds"] = round(time.perf_counter() - started, 2)
    del scene
    report["peak_rss_mb_after_build"] = round(peak_rss_mb())

    if args.compare:
        with tempfile.TemporaryDirectory(prefix="supersplat-compare-") as work:
            reference = Path(work) / "reference.ply"
            lod_level = manifest.name == sog.LOD_META
            source = level_only_manifest(manifest, args.level, Path(work)) if lod_level else manifest
            command = SPLAT_TRANSFORM + [str(source), str(reference)]
            started = time.perf_counter()
            subprocess.run(command, check=True)
            report["splat_transform_seconds"] = round(time.perf_counter() - started, 2)
            report["compare"] = compare(main_obj, reference, "level")
            if env_obj is not None:
                lod = json.loads(manifest.read_bytes())
                env_reference = Path(work) / "environment.ply"
                subprocess.run(SPLAT_TRANSFORM + [str(manifest.parent / lod["environment"]), str(env_reference)], check=True)
                report["compare_environment"] = compare(env_obj, env_reference, "environment")

    if args.save:
        bpy.ops.wm.save_as_mainfile(filepath=str(args.save.resolve()))
    print("SOG_REPORT " + json.dumps(report))


main()
