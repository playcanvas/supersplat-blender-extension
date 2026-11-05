#!/usr/bin/env bash
# Build and validate the extension zip into dist/.
#
# Usage: scripts/build.sh
# Set BLENDER to the Blender 5.3+ executable if it is not on PATH.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
blender="${BLENDER:-blender}"

# Isolate Blender's user config so the build never touches real preferences.
user_resources="$(mktemp -d "${TMPDIR:-/tmp}/supersplat-build.XXXXXX")"
trap 'rm -rf "$user_resources"' EXIT
export BLENDER_USER_RESOURCES="$user_resources"

cd "$root"
# uploadClient.version comes from supersplat/__init__.py; refuse to ship a zip
# whose manifest says something else.
manifest_version="$(sed -n 's/^version = "\(.*\)"$/\1/p' blender_manifest.toml)"
package_version="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' supersplat/__init__.py)"
if [[ -z "$manifest_version" || "$manifest_version" != "$package_version" ]]; then
  echo "Version mismatch: blender_manifest.toml has '$manifest_version'," \
    "supersplat/__init__.py has '$package_version'." >&2
  exit 1
fi

rm -rf dist
mkdir -p dist
"$blender" --factory-startup --command extension build --source-dir . --output-dir dist
for zip in dist/*.zip; do
  "$blender" --factory-startup --command extension validate "$zip"
done
