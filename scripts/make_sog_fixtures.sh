#!/usr/bin/env bash
# Rebuild the SOG/SSOG test fixtures in tests/fixtures/sog/.
#
# Synthetic splats (tests/splat_fixtures.py) are written as 3DGS PLYs, encoded
# with splat-transform, then decoded back to PLY with splat-transform. The
# decoded PLYs are the reference the SOG decoder is compared against.
#
# Usage: scripts/make_sog_fixtures.sh
# Needs Node.js (npx) and Blender 5.3+ (set BLENDER if it is not on PATH).
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
blender="${BLENDER:-blender}"
splat_transform=(npx --yes @playcanvas/splat-transform@3.10.3 --quiet --overwrite --gpu cpu)
out="$root/tests/fixtures/sog"

user_resources="$(mktemp -d "${TMPDIR:-/tmp}/supersplat-fixtures.XXXXXX")"
work="$(mktemp -d "${TMPDIR:-/tmp}/supersplat-fixtures.XXXXXX")"
trap 'rm -rf "$user_resources" "$work"' EXIT
python="$(BLENDER_USER_RESOURCES="$user_resources" "$blender" --background --factory-startup --quiet \
  --python-expr 'import sys; print("PYTHON=" + sys.executable)' | sed -n 's/^PYTHON=//p')"

(cd "$root" && PYTHONPATH="$root:$root/tests" "$python" - "$work" <<'EOF'
import sys
from splat_fixtures import raw_rows, write_raw_ply

work = sys.argv[1]
for name, count, degree, seed in [
    ("sh3", 300, 3, 1),
    ("sh0", 100, 0, 2),
    ("fine", 2100, 1, 3),
    ("coarse", 400, 1, 4),
    ("env", 50, 1, 5),
]:
    write_raw_ply(f"{work}/{name}.ply", raw_rows(count, degree, seed=seed), degree)
EOF
)

rm -rf "$out"
mkdir -p "$out"
"${splat_transform[@]}" "$work/sh3.ply" "$out/sh3/meta.json"
"${splat_transform[@]}" "$work/sh0.ply" "$out/sh0/meta.json"
# Small chunks so level 0 spans two SOG files.
"${splat_transform[@]}" --lod-chunk-count 1 --lod-chunk-extent 1 --lod-chunk-min 1 --lod-file-count 1 \
  "$work/fine.ply" -l 0 "$work/coarse.ply" -l 1 "$work/env.ply" -l -1 "$out/lod/lod-meta.json"

"${splat_transform[@]}" "$out/sh3/meta.json" "$out/sh3.ply"
"${splat_transform[@]}" "$out/sh0/meta.json" "$out/sh0.ply"
"${splat_transform[@]}" --select-lod 0 "$out/lod/lod-meta.json" "$out/lod-0.ply"
"${splat_transform[@]}" --select-lod 1 "$out/lod/lod-meta.json" "$out/lod-1.ply"
"${splat_transform[@]}" "$out/lod/env/meta.json" "$out/lod-env.ply"
