#!/usr/bin/env bash
# End-to-end check of import-by-link in a real Blender window (not --background),
# against the local mock API. Builds the extension, starts scripts/mock_api.py,
# runs scripts/e2e_import.py in Blender and fails unless every case passed.
#
# Usage: scripts/e2e_import.sh [output-dir] [--scene-dir DIR]
#   output-dir  where screenshots and results.json go (default: a new temp folder)
#   --scene-dir a real SOG or SSOG scene folder to import as one more case
# Set BLENDER to the Blender 5.3+ executable if it is not on PATH, and PORT to
# change the mock server's port (default 8765).
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
blender="${BLENDER:-blender}"
port="${PORT:-8765}"
out=""
scene_dir=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --scene-dir) scene_dir="$2"; shift 2 ;;
    *) out="$1"; shift ;;
  esac
done
out="${out:-$(mktemp -d "${TMPDIR:-/tmp}/supersplat-e2e.XXXXXX")}"
mkdir -p "$out"

"$root/scripts/build.sh" >/dev/null

mock_args=(--port "$port")
real_scene=()
if [[ -n "$scene_dir" ]]; then
  mock_args+=(--scene-dir "$scene_dir")
  real_scene=(--real-scene local001)
fi
python3 "$root/scripts/mock_api.py" "${mock_args[@]}" >"$out/mock.log" 2>&1 &
mock=$!
# Fresh, throwaway Blender user folder: real preferences and extensions are never touched.
user_resources="$(mktemp -d "${TMPDIR:-/tmp}/supersplat-e2e-user.XXXXXX")"
trap 'kill "$mock" 2>/dev/null || true; rm -rf "$user_resources"' EXIT
for _ in $(seq 50); do
  curl -sf "http://127.0.0.1:$port/mock/settings" >/dev/null && break
  sleep 0.1
done

echo "Screenshots and results: $out"
SUPERSPLAT_BASE_URL="http://127.0.0.1:$port/api/supersplat" BLENDER_USER_RESOURCES="$user_resources" \
  "$blender" --factory-startup --online-mode --python "$root/scripts/e2e_import.py" -- \
  --out "$out" --mock "http://127.0.0.1:$port" ${real_scene[@]+"${real_scene[@]}"} >"$out/blender.log" 2>&1 || true
grep '^\[e2e\]' "$out/blender.log" || true

python3 - "$out/results.json" <<'PY'
import json, sys
results = json.load(open(sys.argv[1]))
for case in results["cases"]:
    print(f"{'PASS' if case['passed'] else 'FAIL'}  {case['case']}")
    for failure in case.get("failures", []):
        print(f"      {failure}")
sys.exit(0 if results["passed"] else 1)
PY
