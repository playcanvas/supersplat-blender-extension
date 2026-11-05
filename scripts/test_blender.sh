#!/usr/bin/env bash
# Build the zip into dist/, then run the unit tests with Blender's bundled
# Python (which has numpy) and the headless Blender tests against that zip.
#
# Usage: scripts/test_blender.sh
# Set BLENDER to the Blender 5.3+ executable if it is not on PATH.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
blender="${BLENDER:-blender}"
status=0

# The harness installs the newest zip in dist/; rebuild so it is never stale.
BLENDER="$blender" "$root/scripts/build.sh"

# Fresh, throwaway user directory per run: real preferences are never touched.
user_resources="$(mktemp -d "${TMPDIR:-/tmp}/supersplat-test.XXXXXX")"
python="$(BLENDER_USER_RESOURCES="$user_resources" "$blender" --background --factory-startup --quiet \
  --python-expr 'import sys; print("PYTHON=" + sys.executable)' | sed -n 's/^PYTHON=//p')"
rm -rf "$user_resources"
if [[ -z "$python" ]]; then
  echo "Could not find Blender's bundled Python." >&2
  exit 1
fi
echo "== Unit tests ($python)"
(cd "$root" && "$python" -m unittest discover -s tests) || status=1

for mode in --online-mode --offline-mode; do
  user_resources="$(mktemp -d "${TMPDIR:-/tmp}/supersplat-test.XXXXXX")"
  echo "== Blender tests ($mode)"
  BLENDER_USER_RESOURCES="$user_resources" "$blender" --background --factory-startup "$mode" \
    --python-exit-code 1 --python "$root/tests/blender/test_extension.py" || status=1
  rm -rf "$user_resources"
done
exit "$status"
