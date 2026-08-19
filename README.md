# PlayTranslate Decky Plugin (LiveTranslator-kun)

Decky Loader plugin that captures the screen through Gamescope/PipeWire, runs
OCR on it, and overlays a live translation - built for game subtitles on
Steam Deck.

## Features

- **Dynamic Capture** (default): full-frame text discovery and tracking, using
  on-device Chrome Screen AI OCR (Tesseract kept as a debug fallback engine).
  Supports both wide discovery and a single fixed-region crop mode.
- **Legacy fixed-region capture** (`capture.py`): the original per-game
  calibrated-ROI mode, still available and mutually exclusive with Dynamic
  Capture. Not yet fully superseded.
- **Multi-engine translation**: Ollama (local), Gemini, Google Translate,
  Google Cloud Translate, DeepL - with an LRU translation cache.
- **Tap-to-translate**: L4+L2 hold + touchscreen long-press, while paused.
- **Configurable keybindings** for refresh / pause-resume / touch-translate.
- QAM sidebar organized into Capture Control / Settings / Other Settings.

## Layout

This is now a single self-contained folder:

```text
livetranslate-decky/
  plugin.json
  main.py           # Decky plugin backend
  src/index.tsx      # frontend
  bin/                # capture/OCR/translation engine (see bin/README.md)
    capture.py
    capture_dynamic.py
    providers/
    ...
```

`main.py`'s `_candidate_engine_dirs()` looks for the engine in:

1. `PLAYTRANSLATE_ENGINE_DIR` (dev-only override, points at an out-of-tree
   engine checkout)
2. `bin/` inside this plugin (the shipped location)

## Build

```bash
corepack enable
corepack prepare pnpm@latest --activate
pnpm install
pnpm run build
```

Copy the plugin folder to the Steam Deck's Decky plugin directory after build.
The Deck does not need Node.js or pnpm to run the built plugin.

For an existing Deck install, `../deploy/deploy_to_deck.sh` syncs a build
over SSH instead - see that script for details.

## Packaging as a distributable zip

Decky Loader installs a plugin from a zip containing one top-level folder
with these files inside it:

```text
<plugin-folder>/
  plugin.json     # required
  package.json    # required
  dist/index.js   # required - built frontend
  main.py         # required - this plugin uses the Python backend
  bin/            # optional - this plugin's engine scripts
  LICENSE         # required if the license needs it included (MIT does)
  README.md       # optional but recommended
```

```bash
pnpm run release   # build the frontend, then package
# or, if dist/ is already built:
pnpm run package
```

produces `out/livetranslate-decky-<version>.zip`, ready to sideload.

This uses `scripts/package_plugin.sh`, not the official [Decky
CLI](https://github.com/SteamDeckHomebrew/cli)'s `decky plugin build`. That
CLI's build subcommand always runs inside a docker/podman container (it's
built for plugins with a compiled `backend/` tree) - this plugin has nothing
to compile (`main.py` is plain Python), and a container engine isn't
guaranteed to be available wherever this gets packaged from, so the script
just assembles the structure directly instead. Verified against the layout
documented in
[decky-plugin-template](https://github.com/SteamDeckHomebrew/decky-plugin-template).

To sideload the zip on the Deck (or any Decky Loader install) for testing:
QAM -> Decky's own settings (gear icon) -> enable **Developer Mode** ->
Developer tab -> **Install Plugin from Zip**.

**Still open, not needed for a zip-only test install:**

- `plugin.json`'s `publish` block has no `image` - only needed if this ever
  goes through the plugin store or a plugin-browser URL.
- No CI - `pnpm run release` is a local/manual step for now.

## Prerequisites (must be set up before the plugin works)

Unlike `Decky-Translator` (which bundles all its OCR/translation
dependencies as portable Python wheels + a relocatable Python runtime
downloaded via `package.json`'s `remote_binary`), this plugin runs its
OCR/translation engine **inside a distrobox container**. The zip alone is
not enough to run out of the box - the following has to be in place first.

1. **distrobox + podman, and a container named `playtranslate-ocr`.**
   `main.py` always launches `bin/ocr_worker.py` inside a container with
   this exact name (override via the `PLAYTRANSLATE_OCR_BOX` env var) -
   this is required even when the OCR engine is the default Chrome Screen
   AI, not just for the Tesseract fallback.

   **This step is now automated**: open the OCR tab in QAM and press
   **Set Up OCR Environment** (backed by `get_ocr_container_status` /
   `provision_ocr_container` in `main.py`, running
   `bin/setup_ocr_container.sh`). It installs distrobox+podman to
   `~/.local` if missing, creates the `playtranslate-ocr` container
   (Ubuntu 24.04 by default, override with `PLAYTRANSLATE_OCR_IMAGE`) if
   missing, and installs `Pillow`/`protobuf` into it either way. It's
   idempotent, so re-pressing the button after a failure (e.g. a flaky
   download) is safe. The QAM panel polls status every 2s and shows a
   tail of the setup log.

   To do the same thing by hand instead (e.g. over SSH, for debugging):
   ```bash
   PLAYTRANSLATE_OCR_BOX=playtranslate-ocr bash bin/setup_ocr_container.sh
   ```

   Only needed for the Tesseract debug fallback engine (`ocr_settings.engine
   = "tesseract"`), on top of the above - not handled by the automated
   setup, since it's not needed by the default engine:
   ```bash
   distrobox enter playtranslate-ocr -- sudo apt-get install -y tesseract-ocr
   distrobox enter playtranslate-ocr -- pip install --user tesserocr
   ```

2. **GStreamer + PyGObject on the host**, not in distrobox - `capture.py`
   and `capture_dynamic.py` run directly under the Decky-managed Python and
   import `gi.repository.Gst` plus the `pipewiresrc`/`videocrop`/`appsink`
   GStreamer elements. Stock SteamOS ships these already (gamescope itself
   uses PipeWire/GStreamer), so this is normally a non-issue there - verify
   with the commands in [bin/README.md](bin/README.md#steam-deck-dependencies)
   rather than assuming, especially on a non-SteamOS Deck-like distro
   (Bazzite, etc.).

3. **`gamescopectl`** on `PATH` - used for the ROI-crop screenshot feature.
   Ships with gamescope; present on stock SteamOS.

4. **Ollama** (optional) - only if the translation engine is set to
   "Ollama" in QAM settings. The plugin does not install or run Ollama
   itself; point it at a local or LAN Ollama server.

5. **A cloud translation API key** (optional) - only if using Gemini,
   Google Cloud Translate, or DeepL. Entered directly in the QAM settings
   panel, not a file to install.

Nothing above is needed for the Chrome Screen AI OCR model itself - that
~120MB download is fetched on demand by the plugin at runtime (see
`bin/screenai_downloader.py`) once the user clicks "Download" in QAM.
