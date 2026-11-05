# Contributing

Bug reports and pull requests are welcome.

Set `BLENDER` to a Blender 5.3+ executable if `blender` is not on `PATH`:

```sh
export BLENDER="/Applications/Blender.app/Contents/MacOS/Blender"
```

## Build

```sh
scripts/build.sh
```

This builds `dist/supersplat-<version>.zip` and validates it with Blender. It fails if the
`version` in `blender_manifest.toml` differs from `__version__` in `supersplat/__init__.py`.

Install the zip with **Edit > Preferences > Get Extensions > Install from Disk**.

## Tests

```sh
python3 -m unittest discover -s tests   # unit tests; numpy-dependent modules skip without numpy
scripts/test_blender.sh                 # unit tests with Blender's Python, then headless Blender tests
```

The tests check every request and response of the fake API against `tests/fixtures/openapi.json`,
the API's OpenAPI document without its descriptions and examples. To update it from a new copy of
the document, run `python3 scripts/strip_openapi.py <openapi.json>`.

## SOG and SSOG decoding

`supersplat/sog.py` decodes SOG (`meta.json`) and SSOG (`lod-meta.json`) scenes.
`blender_splat.new_splat_object` turns the result into a Gaussian splat object.

`supersplat/scene_import.py` looks a scene up, downloads it and unpacks it. If a SOG or SSOG
scene fails to decode, it imports the PLY instead.

To decode a local folder that holds a `meta.json` or `lod-meta.json`:

```sh
blender --background --factory-startup \
  --python scripts/sog_scene.py -- <folder> [--level N] [--compare] [--save out.blend]
```

`--compare` also converts the files to PLY with splat-transform (needs Node.js), imports that PLY
with Blender's importer and reports the largest attribute differences.

Rebuild the fixtures in `tests/fixtures/sog/` with `scripts/make_sog_fixtures.sh`.

## Mock API

`scripts/mock_api.py` serves the v1 API, scene files and PLY downloads on `127.0.0.1`. It uses the
test double in `tests/fake_api.py`, so its responses are checked against the same OpenAPI document
as the unit tests.

```sh
python3 scripts/mock_api.py [--port 8765] [--scene-dir <folder>] [--reject-key]
SUPERSPLAT_BASE_URL=http://127.0.0.1:8765/api/supersplat blender
```

It prints the scenes it serves:

- a SOG;
- an SSOG with two levels and an environment;
- a legacy compressed-PLY scene, whose PLY download goes queued, processing, ready;
- a SOG that fails to decode, so the PLY is the fallback;
- a scene that is not downloadable;
- an animated GSAF scene;
- a scene whose PLY download fails.

`--scene-dir` adds a real scene folder as `local001`.

Any API key is accepted. Paste a dummy key such as `mock-key`, never a real one.

Change its behavior while it runs:

```sh
curl -d '{"rejectKey": true}' http://127.0.0.1:8765/mock/settings
```

`rejectKey` answers every call with 401. `throttle` (bytes per second) slows file downloads, to
watch the progress bar or try Cancel.

## End-to-end import

```sh
scripts/e2e_import.sh [output-dir] [--scene-dir <folder>]
```

This builds the extension, starts the mock server and runs `scripts/e2e_import.py` in a Blender
window (not `--background`). It uses a throwaway user folder and a dummy key.

For each case it fills in the link field, runs **Import Scene** and checks the outcome: the object,
its splat count, name, place at the 3D cursor, rotation, credits and framing, or the error the
panel shows. It saves a window screenshot per case, a viewport render per imported scene and
`results.json`. It fails unless every case passes.

## Storing a key from a terminal

On a headless Mac, `scripts/store_api_key.py` stores the key in the Keychain. It reads the key from
stdin.

## Development overrides

Set these in the environment Blender runs in. Leave them unset for normal use.

- `SUPERSPLAT_BASE_URL` points the extension at another API, such as the mock server. Plain http is
  accepted only for `localhost` and `127.0.0.1`.
- `SUPERSPLAT_SITE_HOSTS` adds comma-separated site hosts whose scene links are accepted, as well as
  `superspl.at`.

## Style

- Match the surrounding code.
- Keep user-facing text short. Use American English.
- The extension must only download URLs the SuperSplat API returns.

## License

By contributing you agree that your contributions are licensed under
[MIT](LICENSE).
