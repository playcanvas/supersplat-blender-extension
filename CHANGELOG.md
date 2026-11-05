# Changelog

All notable changes to this extension are listed here.

## 0.1.0

First release. Requires Blender 5.3 or later.

### Publish

- Publish the selected Gaussian splat to SuperSplat as an unlisted scene.
- Export a standard 3DGS PLY, with optional title and description.
- Convert Blender's Z-up to the 3DGS convention, including rotations and spherical harmonics.
- Lower the spherical-harmonics degree before upload.
- Show upload progress, with Cancel. Open the result in SuperSplat or copy its link.

### Import by link

- Import a scene from a `superspl.at` link or scene ID, if its owner allows downloads.
- Look a scene up first to see its author, license, format and splat count.
- Pick the detail level of streamed SOG (SSOG) scenes, and whether to import their environment.
- Fold the scene card and options away after an import, with Import Again to reopen them.
- Place the scene upright at the 3D cursor, selected and framed.

### Attribution

- Record the scene ID, author and license on each imported object.
- Show them in the Attribution panel, with View on SuperSplat and View License.
- Collect credits for every imported scene in the `SuperSplat Attributions` text block.

### API key

- Store the key in the login Keychain on macOS.
- Store the key in an owner-only file in the extension's user folder on Windows and Linux.
- Never write the key to Blender preferences.
