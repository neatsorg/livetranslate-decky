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

## Features

- **Real-time auto-translation** - Automatically detects text shown on the
  game screen and displays the translation at the source text's position. It
  keeps tracking and re-translating as the text moves, the screen scrolls, or
  the screen itself changes. You can refresh the screen to re-translate, or
  pause translation temporarily.
- **Region capture mode** - Restrict auto-translation to a specific area of
  the screen, such as a game's subtitle box.
- **Screen-tap translation** - While real-time auto-translation is paused,
  translate only the text at the spot you tap on screen.
- **Recent cache** - When the same line of text appears repeatedly, show the
  cached result instantly instead of re-running OCR and translation.

## Customization

- **Multiple translation engines** - choose between Google Cloud Translate,
  DeepL, Gemini AI, Google Translate, or an AI model via Ollama.
- **Keybindings** - for screen refresh, pause/resume, and tap-translate.
  Each action can be bound to Steam Deck's own controls, a gamepad, or a
  keyboard, and each binding supports long-press and multi-key combos.
- **Legacy OCR** - a TesseractOCR-based engine, built early in development,
  is still available as a fallback. It's unlikely to see active maintenance
  going forward, and using it naturally requires TesseractOCR itself.

## Requirements

- Steam Deck (SteamOS) - experimental support on other gamescope-based Linux
  handhelds/desktops, see [Running on a non-Deck Linux host](#running-on-a-non-deck-linux-host)
- [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader) installed
- distrobox + podman, for the OCR/translation engine container (installed
  automatically - see [Setup](#setup) below)
- Internet connection for the on-device OCR model's one-time download, and
  for any of the cloud translation engines

## Installation

1. Install [Decky Loader](https://decky.xyz) on your Steam Deck if you
   haven't already - see the [official install
   guide](https://deckyloader.org/guide/how-to-install-decky-loader-steam-deck)
   for details.
2. [Click here to download the latest version.](../../releases/latest)
3. In Steam's Quick Access Menu, press the plug icon to open the Decky
   Loader settings you installed in step 1.
4. In Decky Loader's settings, select **General** from the left-hand menu,
   then enable the **Developer Mode** toggle near the bottom.
5. New menu items ("Plugins", "Developer", "Testing", etc.) will appear.
   Choose **Developer**, then press the **ZIP file** button and open the
   file you downloaded to install it - or press the **URL** button and
   paste the URL of the latest zip instead.

## Setup

The plugin runs its OCR/translation engine inside a small container, which
needs a one-time setup:

1. From the plugin's menu, press **OCR Settings**.
2. On the screen that opens, press **Set Up OCR Environment**. This
   installs distrobox/podman if missing, creates the container, and installs
   the Python packages the engine needs. If a step fails (a temporary
   network issue, for example), it's safe to press again - it resumes from
   where it left off. The setup log is shown live while it runs. Once the
   small labels above the button read `distrobox:installed` and
   `container:created`, setup is complete.
3. The default OCR engine (Chrome Screen AI) still needs to be downloaded.
   Press **Download** at the bottom of the same screen. Once the label above
   the button reads `Installed`, it's ready.

Optional, only if you plan to use them:

- **A cloud API key**, if you use Gemini AI, Google Cloud Translate, or
  DeepL. Press **Translation Settings** in the plugin's menu to open the
  engine-selection screen - paid engines generally require an API key,
  entered there. As the author, I'd personally recommend Google Cloud
  Translate the most.
- **Ollama**, if you set the translation engine to Ollama - point it at a
  local or LAN Ollama server. The plugin itself doesn't install or run
  Ollama.

## How to use

1. Press **Start Capture** on the plugin's settings screen to begin
   real-time translation.
2. Your configured keybinding refreshes the subtitle (default: L4) or
   toggles pause/resume (default: hold L4).
3. While paused, hold the tap-translate key (default: L4+L2) and long-press
   the screen to show only the translation for the text at that spot, at the
   bottom of the screen.
4. Change keybindings via the **Key Bindings** button on the settings
   screen.
5. Press **Stop Capture** to end real-time translation. The plugin doesn't
   try to detect whether you're actually in a game before translating, so
   pause it or press **Stop Capture** whenever you don't need translation.

### Region Capture Mode

1. Instead of **Start Capture**, click **Start Region Mode** to start
   auto-translation limited to a specific area of the screen. Useful for
   games where subtitles always appear in the same place, or when
   translating the whole screen is more distracting than helpful.
2. Configure the region to translate via the **Region Mode Config** button
   on the settings screen.

## Troubleshooting

**Nothing is detected / translation never appears**
Check that the Capture Control area on the settings screen shows "running".
If the OCR environment hasn't been set up yet, the OCR tab will say so - see
[Setup](#setup) above.

**A translation engine says the API key is wrong**
Check that the key for that provider was copied correctly into the settings
screen. Gemini, Google Cloud Translate, and DeepL each use their own
separate key.

**OCR Environment setup fails partway through**
Press **Set Up OCR Environment** again - it's idempotent and will pick up
where it left off. If it keeps failing, check the setup log shown in the OCR
tab for the actual error.

### Running on a non-Deck Linux host

The plugin is built and tested against SteamOS/Deck, but has also been
confirmed working on a non-Deck Linux host (gamescope + Decky Loader on
CachyOS), with a few caveats around gamepad/keyboard input:

- **gamescope is required**, to get an environment equivalent to Steam
  Deck's. It's a Wayland micro-compositor developed by Valve, with a rich
  set of features tuned for gaming, used on Steam Deck and on Linux desktops
  running gaming-focused OSes like SteamOS. Running Steam through gamescope
  and having it output to PipeWire is what lets this plugin capture the
  game's video as a stream in the first place - which is genuinely
  impressive. gamescope also has MangoHud's overlay functionality built in,
  and using that to show subtitles was actually the original idea behind
  this plugin. Installation depends on your distribution - on Arch-based
  systems (the same family as SteamOS), this usually works:
  ```bash
  sudo pacman -S gamescope
  ```
- **Decky Loader runs each plugin's backend as an unprivileged user with no
  supplementary groups**, so `/dev/hidraw*`/`/dev/input/event*` may stay
  unreadable even after adding your user to the `input` group. If
  keybindings don't detect your controller/keyboard, add a permissive udev
  rule:
  ```bash
  printf '%s\n%s\n' \
    'SUBSYSTEM=="hidraw", MODE="0666"' \
    'SUBSYSTEM=="input", KERNEL=="event*", MODE="0666"' \
    | sudo tee /etc/udev/rules.d/99-livetranslate-hidraw.rules
  sudo udevadm control --reload
  sudo udevadm trigger --subsystem-match=hidraw --subsystem-match=input
  sudo systemctl restart plugin_loader
  ```
  This makes those devices world-readable/writable on the host - a
  reasonable tradeoff on a single-user desktop, but worth knowing about on a
  shared machine.
- **A USB gamepad bound to the `xpad` kernel driver** (most wired Xbox
  controllers) doesn't expose a hidraw node, so keybindings fall back to
  plain evdev for those - digital buttons only, no analog triggers.
- **Closing an overlay (this plugin's own settings screen) can leave the
  game unable to receive gamepad input** until you click into the game
  window with a mouse. This has only been seen on a desktop Linux host
  running gamescope directly, not on a real Deck; there's no fix from the
  plugin's side yet.
- **Screen-tap translation doesn't work.** Pressing the tap-translate key
  and then clicking the screen sometimes shows no translation, and
  afterward the game window won't regain focus until you click it with the
  mouse. This is likely tied to the overlay-focus issue above.
- **Running gamescope nested inside a Wayland compositor can cause issues
  with gamepad input and overlay display.** gamescope can run nested on top
  of an existing display server/desktop environment, but this can't be
  guaranteed not to cause problems with Decky Loader. My personal
  recommendation is to run gamescope in embedded mode instead - i.e. select
  gamescope as the session at your login screen, so the machine boots
  straight into a dedicated full-screen Steam UI, Deck-style. This differs
  from launching Steam from the desktop and switching to Big Picture mode:
  with no other compositor running underneath, this setup is much less
  prone to the issues above.

## Development

See [docs/BUILDING.md](docs/BUILDING.md) for building from source.

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
