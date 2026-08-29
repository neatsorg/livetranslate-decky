# LiveTranslator-kun

[English](./README.md) | [日本語](./README.ja.md)

A [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader) plugin
that captures the screen through Gamescope/PipeWire, recognizes text with
OCR, and overlays the translation in real time.

It's built primarily for translating game subtitles on Steam Deck, but most
of its features also work on a standard Linux setup. Currently, only
English-to-Japanese translation is supported.

This project is developed with AI-assisted coding. I don't believe any code
was copied from elsewhere, but if that's a concern for you, please refrain
from using it.

[Video Demo](https://www.youtube.com/watch?v=wTeWC3wXl9k)

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
  long-press and multi-key combos. Default: **F9** (tap to refresh, hold to
  pause/resume).

## Requirements

- Windows 10 or Windows 11
- Nothing else to install up front - the download is a self-contained app,
  no separate Python install needed
- The target OCR language's Windows OCR component (installed from inside
  the app - see [Setup](#setup) below), and an internet connection for that
  one-time download
- Internet connection for the default (free) translation engine, or for any
  of the other cloud translation engines

## Installation

1. [Click here to download the latest Windows
   version.](../../releases/latest) -
   `LiveTranslator-kun-windows-<version>.zip`
2. Extract the zip anywhere you like - it doesn't need to go in
   `Program Files`, and there's no traditional installer.
3. Run `LiveTranslator-kun.exe` inside the extracted folder. A tray icon
   appears (in the system tray, or under the small "^" overflow arrow next
   to the clock) - right-click it for **Settings...**, and to start/pause
   capture.

## Setup

- **Windows SmartScreen warning on first run**: this app isn't code-signed,
  so the first time you run `LiveTranslator-kun.exe`, Windows will likely
  show a blue **"Windows protected your PC"** screen. Click **More info**,
  then **Run anyway** - this is a one-time thing per download.
- **OCR language component**: Windows' built-in OCR needs the target
  language's OCR component installed as a separate Windows feature - this
  is the language of the *on-screen text you want to translate* (e.g.
  English for most games), independent of your display language and of the
  translation target language. Right-click the tray icon → **Settings...**
  → next to **OCR engine**, press **Manage OCR languages...**, then
  **Install** next to the right language. This triggers a real Windows
  administrator (UAC) prompt - approve it. It needs an internet connection
  the first time, and can take a little while.

Optional, only if you plan to use them:

- **A cloud API key**, if you use Gemini AI, Google Cloud Translate, or
  DeepL. In **Settings...**, switch **Translation engine** to the one you
  want - each one's own API key field appears once selected. The default
  engine (Google Translate) needs no key or account at all. As the author,
  I'd personally recommend Google Cloud Translate the most among the paid
  options.
- **Ollama**, if you set the translation engine to Ollama - point **URL** at
  a local or LAN Ollama server. The app itself doesn't install or run
  Ollama.

## How to use

1. Right-click the tray icon and choose **Start Capture** to begin
   real-time translation - the app starts paused, so nothing happens until
   you do this (or use the pause/resume keybinding below). The same menu
   item turns into **Pause** once running, so you can use it to stop again
   too.
2. The default keybinding is **F9**: tap it to refresh (re-detect the
   current screen), hold it to toggle pause/resume - the same effect as the
   tray menu item above.
3. Change keybindings via **Settings...** → **Configure keybindings...**.
4. Leaving **Target window title** blank in Settings captures the whole
   screen; typing part of a window's title (e.g. a game's name) restricts
   capture to just that window instead - press **Refresh list** to pick
   from currently open windows.
5. Choose **Exit** from the tray menu to quit. The app doesn't try to detect
   whether you're actually in a game before translating, so pause it or
   exit whenever you don't need translation.

### Capture Region (Fixed ROI)

1. In **Settings...**, press **Configure capture region (fixed ROI)...**.
2. Take a screenshot preview, then set **Left**/**Top**/**Width**/**Height**
   to the area you want to track - useful for games where subtitles always
   appear in the same place, or when translating the whole screen is more
   distracting than helpful.
3. Press **Save && enable** to turn it on, or **Disable fixed ROI** to go
   back to scanning the whole captured area.

## Troubleshooting

**Windows blocked the app from running**
See [Setup](#setup) above - click **More info**, then **Run anyway** on the
SmartScreen warning.

**Nothing is detected / translation never appears**
Right-click the tray icon - if the menu says **Start Capture**, capture is
currently paused; choose it to begin. If the translation engine or the OCR
engine isn't configured yet, a tray notification says so when the app
starts - open **Settings...** to fix it. Also check that **Target window
title** in Settings actually matches an open, non-minimized window, or is
left blank to capture the whole screen.

**A translation engine says the API key is wrong**
Check that the key for that provider was copied correctly into
**Settings...**. Gemini, Google Cloud Translate, and DeepL each use their
own separate key.

**OCR language install fails, or a language shows "Not installed" that
you're sure you already installed**
Press **Install** again next to that language in **Manage OCR
languages...** - it's safe to retry. It needs an internet connection and a
real Windows administrator (UAC) approval each time.

## Development

See [docs/BUILDING_WINDOWS.md](docs/BUILDING_WINDOWS.md) for building from source.

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
- **Windows port** - low priority, since I'm generally in the "if you're
  gaming, use Linux" camp - but Windows.Media.Ocr is impressive, so maybe
  someday.
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
- **How do I open the Quick Access Menu with a gamepad?** Home button + A
  (for an Xbox-style button layout). On a keyboard, it's Ctrl+2.

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
