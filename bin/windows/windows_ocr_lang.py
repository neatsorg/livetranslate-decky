"""Install/query Windows' per-language OCR components (the separate opt-in
"Language.OCR~~~<locale>~0.0.1.0" Windows capability each language needs -
independent of display/input language; confirmed live 2026-08-26 this
laptop only had ja-JP installed despite being a Japanese-locale machine, and
NORCO's English text needed en-US installed separately).

Querying installed/available capabilities does NOT need elevation (confirmed
live: works over a plain non-elevated session). *Installing* one does -
`Add-WindowsCapability` requires admin, so install() triggers a real UAC
consent prompt via ShellExecuteW's "runas" verb rather than trying to run
elevated all the time or bypass consent - the user must see and approve that
prompt themselves; there is no way around that, and there shouldn't be.
"""
import subprocess
import time

import win32con
import win32event
import win32process
from win32comext.shell import shellcon
from win32comext.shell.shell import ShellExecuteEx

from i18n import t

# Locale tags worth offering in the UI - not every locale DISM knows about,
# just the common ones a translation target language is likely to need.
# Display names come from i18n.t("locale.<tag>") - see i18n.py's catalog -
# not a dict here, so there's only one place each locale's label lives.
KNOWN_LOCALES = ["en-US", "ja-JP", "ko-KR", "zh-CN", "zh-TW", "fr-FR", "de-DE", "es-ES"]


def locale_label(locale):
    return t(f"locale.{locale}")


def _capability_name(locale):
    return f"Language.OCR~~~{locale}~0.0.1.0"


def list_states():
    """{locale: "Installed"|"NotPresent"|...} for every locale in
    KNOWN_LOCALES currently known to DISM (locales DISM doesn't recognize on
    this Windows build/edition are simply omitted)."""
    ps_command = (
        "Get-WindowsCapability -Online | "
        "Where-Object { $_.Name -like 'Language.OCR*' } | "
        "ForEach-Object { \"$($_.Name)=$($_.State)\" }"
    )
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps_command],
        capture_output=True, text=True, timeout=30,
    )
    states = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if "=" not in line:
            continue
        name, state = line.split("=", 1)
        for locale in KNOWN_LOCALES:
            if name == _capability_name(locale):
                states[locale] = state
    return states


def is_installed(locale):
    return list_states().get(locale) == "Installed"


def install(locale, timeout_s=600):
    """Triggers a real UAC consent prompt (ShellExecuteEx with the "runas"
    verb) and blocks until the elevated Add-WindowsCapability process exits.

    Returns (ok, message). ok=False + a clear message if the user declines
    the UAC prompt (ERROR_CANCELLED / access-denied-style failures from
    ShellExecuteEx itself, before the elevated process even starts) or if
    the elevated install command itself fails/times out.
    """
    capability = _capability_name(locale)
    ps_args = (
        f"-NoProfile -Command \"Add-WindowsCapability -Online -Name {capability}\""
    )
    try:
        proc_info = ShellExecuteEx(
            nShow=win32con.SW_HIDE,
            fMask=shellcon.SEE_MASK_NOCLOSEPROCESS,
            lpVerb="runas",
            lpFile="powershell.exe",
            lpParameters=ps_args,
        )
    except Exception as exc:
        # Confirmed shape for a declined UAC prompt: pywin32 raises here
        # rather than returning a failure code - error 1223 is
        # ERROR_CANCELLED ("The operation was canceled by the user").
        return False, t("ocr_lang.uac_declined", error=exc)

    handle = proc_info["hProcess"]
    wait_result = win32event.WaitForSingleObject(handle, int(timeout_s * 1000))
    if wait_result == win32event.WAIT_TIMEOUT:
        return False, t("ocr_lang.timeout", timeout=timeout_s)

    exit_code = win32process.GetExitCodeProcess(handle)
    if exit_code != 0:
        return False, t("ocr_lang.exit_error", code=exit_code)

    # Confirmed live: DISM's queryable state can lag a moment behind the
    # installer process actually exiting - an immediate single check can
    # still report NotPresent for a capability that finished installing
    # correctly seconds earlier. Retry briefly before calling it a failure.
    for _attempt in range(6):
        if is_installed(locale):
            return True, t("ocr_lang.install_success")
        time.sleep(2)
    return False, t("ocr_lang.install_unconfirmed")
