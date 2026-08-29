# LiveTranslator-kun

[English](./README.md) | [日本語](./README.ja.md)

Captures the screen, recognizes text with OCR, and overlays the translation
in real time. Currently, only English-to-Japanese translation is supported.

This project is developed with AI-assisted coding. I don't believe any code
was copied from elsewhere, but if that's a concern for you, please refrain
from using it.

[Video Demo](https://www.youtube.com/watch?v=wTeWC3wXl9k)

## Get started

- **[Windows](docs/WINDOWS.md)** - a standalone app, no Python required
- **Steam Deck / Linux** - a [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader)
  plugin, capturing the screen through Gamescope/PipeWire. Most features
  also work on a standard Linux setup, not just a real Deck. (This branch
  is mid-merge with the Windows port - see `main` for the current Linux
  install instructions until that lands.)

## Features

- **Real-time auto-translation** - Automatically detects text shown on the
  game screen and displays the translation at the source text's position. It
  keeps tracking and re-translating as the text moves, the screen scrolls, or
  the screen itself changes. You can refresh the screen to re-translate, or
  pause translation temporarily.
- **Region capture mode** - Restrict auto-translation to a specific area of
  the screen, such as a game's subtitle box.
- **Recent cache** - When the same line of text appears repeatedly, show the
  cached result instantly instead of re-running OCR and translation.

## Customization

- **Multiple translation engines** - choose between Google Cloud Translate,
  DeepL, Gemini AI, Google Translate, or an AI model via Ollama.
- **Keybindings** - for screen refresh and pause/resume. Each action can be
  bound to a keyboard, mouse button, or gamepad, and each binding supports
  long-press and multi-key combos. On Windows, the default is **F9** (tap to
  refresh, hold to pause/resume).

## Development

- Windows: see [docs/BUILDING_WINDOWS.md](docs/BUILDING_WINDOWS.md)
- Steam Deck / Linux: see `docs/BUILDING.md` on `main`

## Support

I have no idea how much this will actually get used, so I don't know how
many bug reports, support requests, or feature requests to expect - or
whether I'll be able to address them. But if something comes up, please open
an [issue](../../issues).

You can also support development via [Ko-fi](https://ko-fi.com/neatsorg).

## Features Planned If Development Continues

- **Pause the game and translate a selected rectangle** - in a sense, a
  throwback feature. Aiming for PCOT.
- **Multi-language support** - add Japanese-to-English translation
  alongside the current English-to-Japanese, then expand the source/target
  language options further.
- **Support beyond games** - seems fairly feasible. Live-translating video
  subtitles, though, isn't realistic at the current speed.

## Likely FAQ

- **Can you support game XXXXX?** This isn't a tool that adds per-game
  support, so no. If the same kind of issue shows up across multiple games,
  there's a chance I'll look into it.
- **Translation quality is weak.** Right now this tool hands off both OCR
  and translation entirely to external engines, so quality mostly depends on
  those getting better over time.
- **Translation is slow.** There's still room to speed things up. If
  development continues, this is something I'd likely work on.
- **Please port it to Mac.** The relative benefit feels small, so I'm not
  very interested.
- **Please add speech translation.** The overhead would be significant, so
  I'm not very interested - though I might try it if a strong enough
  external API shows up.
- **It doesn't work.** If you describe your environment, the game, and the
  situation in a lot of detail, I might be able to figure it out - but I
  can't promise anything. And the more unusual your setup, the harder it
  gets.

## Acknowledgments

- [Valve](https://www.valvesoftware.com/) - for the Steam Deck, a wonderful
  piece of hardware, and for Gamescope. I share their philosophy.
- [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader) -
  massively expanded what's possible on the Steam Deck. I share their
  philosophy.
- [Decky-Translator](https://github.com/cat-in-a-box/Decky-Translator) -
  seeing what was possible with Decky Loader through this project genuinely
  impressed me, and I leaned on its design a lot as a reference.
- [PlayTranslate](https://github.com/dominostars/playtranslate) - watching
  that gameplay video is what made me think "amazing, I want that too -
  let's try something similar on Steam Deck!" That's why traces of the name
  still show up in my code (function names and the like) - though of
  course, no code was copied 😉. The original idea, though, belongs to this
  person. Thanks for a great experience.
- [Google](https://google.com/) - for Translate, the Gemini API, and more.
- [Chromium Projects](https://www.chromium.org/) - Chrome Screen AI is
  amazing.

## License

[GPLv3](LICENSE)

The Windows distribution bundles several third-party libraries (PySide6,
DXcam, pywin32, pywinrt, and others) - see
[THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md) for their licenses.
