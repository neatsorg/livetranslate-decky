# Building LiveTranslator-kun from source

This is developer-facing documentation for building the plugin from source.
If you just want to install and use the plugin, see the
[README](../README.md) instead.

## Layout

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
