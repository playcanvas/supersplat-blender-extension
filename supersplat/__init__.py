"""Blender-independent core: API client, uploader, credentials and PLY export."""

# Keep in sync with `version` in blender_manifest.toml; the unit tests,
# scripts/build.sh and the Blender tests fail if the two differ.
__version__ = "0.1.0"

UPLOAD_CLIENT_ID = "supersplat-blender-extension"
USER_AGENT = f"{UPLOAD_CLIENT_ID}/{__version__}"
