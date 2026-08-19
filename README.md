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
playtranslate-decky/
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
