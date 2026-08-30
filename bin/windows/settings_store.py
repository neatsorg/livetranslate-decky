"""Persisted user settings for the Windows port - the equivalent of the
Linux side's translation_settings.json, but this is a standalone Windows app
(no Decky host to hand settings through), so it owns its own file directly
under the conventional per-user %APPDATA% location rather than a plugin
data dir.

Deliberately plain JSON + a dict, no schema library - this mirrors how
translate_server.py/main.py already keep things stdlib-simple on the Linux
side, and the settings surface here is still small.
"""
import json
import os
from pathlib import Path

APP_NAME = "LiveTranslator-kun"

# Per-provider config, matching each TranslationProvider subclass's own
# __init__ kwargs (providers/*.py) - not just "api_keys", since Ollama takes
# a url+model (no key at all) and Gemini takes a model name alongside its key.
DEFAULTS = {
    # None = auto-detect from the OS UI language (see i18n.detect_default_
    # language) - only ever becomes "ja"/"en" once the user picks explicitly
    # via the settings UI, or a script sets it directly.
    "ui_language": None,
    # "google" (the free, unofficial, no-account-needed translate endpoint)
    # rather than "google_cloud" - a fresh install has no API key yet, and
    # PipelineLoop now tolerates an unconfigured provider without crashing
    # (see _configure_from_settings()'s docstring), but defaulting to a
    # provider that actually works with zero setup is a strictly better
    # first-run experience than one that just fails safely. Users who want
    # Google Cloud/DeepL/Gemini/Ollama's better quality can still switch via
    # Settings at any time.
    "provider": "google",
    "provider_config": {
        "dummy": {},
        "google": {},
        "google_cloud": {"api_key": ""},
        "deepl": {"api_key": ""},
        "gemini": {"api_key": "", "model": "gemini-3.6-flash"},
        "ollama": {"url": "http://127.0.0.1:11434", "model": "translategemma"},
    },
    "target_lang": "ja",
    "source_lang": "auto",
    # "windows_ocr" (Windows.Media.Ocr) is the only engine: ~0.19s/frame,
    # confirmed live faster and license-cleaner than the Chrome Screen AI
    # path the Linux side still uses (dropped here 2026-08-28 - see
    # project_playtranslate_windows_port memory). Needs the matching
    # per-language OCR component installed via Windows Settings, see
    # windows_ocr_lang.py.
    "ocr_engine": "windows_ocr",
    # BCP-47 tag for Windows.Media.Ocr specifically - it needs a concrete
    # language (no "auto"), independent of target_lang/source_lang above
    # which describe the *translation* direction, not what OCR should read.
    "windows_ocr_language": "en-US",
    # Locales confirmed installed via this app's own "Manage OCR languages"
    # dialog - see windows_ocr_lang.py's module docstring for why this cache
    # exists instead of just querying DISM live each time (short version:
    # Get-WindowsCapability -Online genuinely requires admin rights under a
    # real UAC-filtered token, confirmed live 2026-08-29 - querying it live
    # for *display* would mean a UAC prompt just to open this dialog). A
    # locale already installed before the user ever used this dialog (e.g.
    # it shipped with the OS) won't show up here until Install is clicked on
    # it once - harmless, since Add-WindowsCapability on an
    # already-installed capability just succeeds immediately.
    "ocr_installed_locales": [],
    # Keyboard/mouse/gamepad bindings - same shape as the Linux side's
    # keybinding_settings.json (see main.py/Keybindings.tsx): a list of
    # {id, command, keys, long_press, threshold_ms}. "keys" holds up to 3
    # input_state.py identifiers (kbd:<vk>, mouse:left, pad:0:A, ...).
    # Default mirrors the Linux default's L4 dual-role (short tap =
    # refresh, long hold = pause/resume) on a single key instead of a
    # dedicated Deck button - F9 (VK code 120), chosen for being unlikely
    # to collide with a game's own keyboard controls.
    "keybindings": [
        {"id": "default_refresh", "command": "refresh", "keys": ["kbd:120"], "long_press": False, "threshold_ms": 900},
        {"id": "default_pause_resume", "command": "pause_resume", "keys": ["kbd:120"], "long_press": True, "threshold_ms": 900},
    ],
    # window_title: substring match against the target game's window title
    # (case-insensitive). Empty = capture the whole monitor (device_idx/
    # output_idx), same as before this setting existed.
    # fixed_roi: null = normal full-frame discovery mode; or
    # {x_pct,y_pct,width_pct,height_pct} to track exactly one user-picked
    # region instead of discovering blocks across the whole capture -
    # matches the Linux side's RoiCrop.tsx / capture_dynamic.py --fixed-roi.
    "capture": {"device_idx": 0, "output_idx": 0, "window_title": "", "fixed_roi": None},
    "hud": {"font_family": "Yu Gothic UI", "font_size": 12},
}


def settings_path():
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / APP_NAME / "settings.json"


def _merge_defaults(data, defaults):
    """Fill in any keys missing from a loaded/older settings file with
    current defaults, recursively - so adding a new setting later doesn't
    require a migration step or break loading an existing file."""
    merged = dict(defaults)
    for key, value in data.items():
        if isinstance(value, dict) and isinstance(defaults.get(key), dict):
            merged[key] = _merge_defaults(value, defaults[key])
        else:
            merged[key] = value
    return merged


def load():
    path = settings_path()
    if not path.exists():
        return dict(DEFAULTS)
    try:
        # utf-8-sig transparently strips a leading BOM if present (harmless
        # if not) - confirmed live that a settings.json hand-edited via
        # PowerShell's `Set-Content -Encoding utf8` gets a BOM, which plain
        # utf-8 + json.loads chokes on, silently falling back to DEFAULTS
        # (including a blank API key) rather than raising anything obvious.
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return dict(DEFAULTS)
    if not isinstance(data, dict):
        # Valid JSON but the wrong shape (e.g. a hand-edit left `[]` or
        # `null` at the top level) - _merge_defaults() assumes a dict and
        # would raise AttributeError on data.items(). Same fallback as an
        # outright parse failure above.
        return dict(DEFAULTS)
    return _merge_defaults(data, DEFAULTS)


def save(settings):
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)
