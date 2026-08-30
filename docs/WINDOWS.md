← [Back to the main README](../README.md)

# LiveTranslator-kun for Windows

Captures the screen, recognizes text with OCR, and overlays the translation
in real time. Currently, only English-to-Japanese translation is supported.
The original is for Steam Deck/Linux; this Windows version is a port of it.

This project is developed with AI-assisted coding. I don't believe any code
was copied from elsewhere, but if that's a concern for you, please refrain
from using it.

[Video Demo](https://www.youtube.com/watch?v=wTeWC3wXl9k)

*A packaged Windows build (the .exe) made by the author is distributed
separately, on an external site, for a small fee - basically covering the
effort of building and packaging it :)
If you'd rather use it for free, please build it from source.*

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
- The target OCR language's Windows OCR component (installed from inside
  the app - see [Setup](#setup) below)
- Internet connection for downloads and for using a cloud translation
  engine

## Installing from the zip

1. [Download the latest Windows version from the external distribution
   site](https://bunkei.tech/videogame-computing-20260830-705/).
2. Extract the downloaded zip anywhere you like - any folder works.
3. Run `LiveTranslator-kun.exe` inside the extracted folder. If a warning
   appears, see [Setup](#setup) below. Once the app starts, a tray icon
   appears (next to the desktop clock, or under the small "^" overflow
   arrow). Right-click it for **Settings...**, and to start/pause capture.
   Setting it up first is recommended.

## Building it yourself

See [BUILDING_WINDOWS.md](BUILDING_WINDOWS.md) for building from source.

## Setup

- **Windows SmartScreen warning on first run**: this app isn't code-signed,
  so the first time you run `LiveTranslator-kun.exe`, a blue **"Windows
  protected your PC"** screen may appear. There's nothing to worry about -
  click **More info**, then **Run** to proceed. This only needs doing once,
  right after extracting the zip.
- **OCR language component**: before using the app, you need to install the
  OCR component for whichever language you want to translate *from*.
  Right-click the tray icon → **Settings...**. In the Settings window, press
  **Manage OCR languages...** next to **OCR engine**, then click **Install**
  next to the language you want. A Windows administrator (UAC) prompt
  appears - approve it, and the OCR language component finishes downloading
  shortly after. Depending on your connection, this can take a little while.

Optional, only if you plan to use them:

- **A cloud API key**, if you use Gemini AI, Google Cloud Translate, or
  DeepL. Switching **Translation engine** in **Settings...** reveals each
  engine's own API key field - enter it there to use that engine. See each
  service's own help documentation for how to get a key. As the author, I'd
  personally recommend Google Cloud Translate the most among the paid
  options. The default engine (Google Translate) is free, so it needs no
  key or account at all.
- **Ollama**, if you set the translation engine to Ollama - point **URL**
  at a local or LAN Ollama server. Installing, configuring, and running
  Ollama itself is outside this app's support - please handle that
  yourself.

## How to use

1. The app starts paused. Right-click the tray icon and choose
   **Settings...** to open the Settings window.
2. In **Target window title** in Settings, pick the title of the active
   window you want to translate. If nothing matches, you can type it
   yourself (e.g. part of the game's name) - leave it blank to make the
   whole screen the translation target. Press **Refresh list** to refresh
   the list of currently open windows. Once the target window is set, save
   and close Settings.
3. Choose **Start Capture** from the tray menu to lift the pause and begin
   real-time translation. While it's running, that same menu item reads
   **Pause** - choosing it stops translation and clears the translated text
   from the screen.
4. Instead of using the tray menu, holding **F9** (the default) toggles
   pause/resume. Pressing **F9** once, without holding it, triggers a
   reload instead - the screen gets re-scanned and the translation is
   redrawn.
5. Change keybindings via the **Configure keybindings...** button in
   Settings.
6. Triggering reload while paused shows the translation just once - handy
   for games where the always-on real-time mode is more distracting than
   helpful.
7. Choose **Exit** from the tray menu to quit the app. Use pause and exit
   as fits the situation.

### Capture Region (Fixed ROI)

1. To restrict translation to a specific area of the screen, press
   **Configure capture region (fixed ROI)...** in Settings.
2. Click **Retake screenshot** to show a preview, then set
   **Left**/**Top**/**Width**/**Height** to the area you want to translate -
   useful for games where subtitles always appear in the same place, or
   when translating the whole screen is more distracting than helpful.
3. Press **Save && enable** to turn region-limited translation on. Press
   **Disable fixed ROI** to turn it off again and go back to scanning the
   whole screen.

## Troubleshooting

**Windows blocks it from starting**
See [Setup](#setup) above - clicking **More info**, then **Run**, on the
SmartScreen warning should let it start.

**Nothing is detected / translation never appears**
- Right-click the tray icon - if the menu says **Start Capture**, it's
  currently paused; choose it to start real-time translation.
- If the translation engine or the OCR engine isn't configured yet, a tray
  notification says so when the app starts. See [Setup](#setup) above to
  configure it.
- Also check that **Target window title** in Settings actually matches a
  window that's open and currently in the foreground (active), or is left
  blank to capture the whole screen.

**A translation engine says the API key is wrong**
Check that the relevant key was copied correctly into Settings. Gemini,
Google Cloud Translate, and DeepL each use their own separate key.

**OCR language install fails, or a language shows "Not installed" that
you're sure you already installed**
In **Manage OCR languages...**, press **Install** again next to that
language to re-download it.

## Known limitations of the Windows version

1. Exclusive fullscreen mode isn't supported - run the game windowed.
2. Multiple monitors aren't currently supported. If you use more than one
   display, run the game you want to translate on display "1" (the main
   display) - a game on another monitor, or moved to one, can't be
   detected for translation.
3. The tap-to-translate-only-what-you-tap feature that exists on the Linux
   version doesn't exist here.
4. When several translated subtitles show up close together in a narrow
   area, they can rarely overlap. This is close to a bug, but the practical
   impact is minor, so it's left unfixed for now.

These limitations may get addressed if enough people end up using the
Windows version - nothing's decided yet :)
