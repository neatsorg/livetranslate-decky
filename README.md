# PlayTranslate Decky Plugin

Minimal Decky Loader control panel for the PlayTranslate capture engine.

This first version only starts, stops, and checks the status of the existing
`capture.py` process. OCR, translation, and overlay rendering are intentionally
left for later steps.

## Layout

```text
/home/user/project/
  playtranslate-deck/
    capture.py
    config.json

  playtranslate-decky/
    plugin.json
    main.py
    src/index.tsx
```

The backend searches for the engine in this order:

1. `PLAYTRANSLATE_ENGINE_DIR`
2. `bin/` inside this plugin
3. a sibling `playtranslate-deck` directory
4. `/home/deck/project/playtranslate-deck`
5. `/home/user/project/playtranslate-deck`

## Build

```bash
corepack enable
corepack prepare pnpm@latest --activate
pnpm install
pnpm run build
```

Copy the plugin folder to the Steam Deck's Decky plugin directory after build.
The Deck does not need Node.js or pnpm to run the built plugin.
