"""Install/query Windows' per-language OCR components (the separate opt-in
"Language.OCR~~~<locale>~0.0.1.0" Windows capability each language needs -
independent of display/input language; confirmed live 2026-08-26 this
laptop only had ja-JP installed despite being a Japanese-locale machine, and
NORCO's English text needed en-US installed separately).

*Querying* installed/available capabilities turns out to ALSO need admin
rights, same as installing - this module used to assume otherwise ("confirmed
live: works over a plain non-elevated session"), but that "confirmation" was
itself wrong: it was tested over this project's dev SSH session, whose token
turned out to not be properly UAC-filtered the way a real interactive user
session's is (confirmed live 2026-08-29 via `whoami /groups` run from an
actual interactive-session Task Scheduler task: BUILTIN\\Administrators shown
as "use for deny only" + Mandatory Label = Medium - a textbook filtered
standard token - and Get-WindowsCapability -Online, and even the raw
`dism.exe /Online /Get-CapabilityInfo` CLI, both hard-fail with "administrator
privileges required" [error 740] under that real token, no exceptions, not a
timing/race issue at all). A live SSH-launched query from the *same machine*
kept succeeding throughout this investigation - the SSH server's own logon
apparently doesn't go through the normal interactive UAC split-token flow, so
testing over SSH silently validated the wrong security context this whole
time - a real dev-environment/production-environment mismatch, not a Windows
quirk that end users will ever actually see work.

Because of that, this module can't offer a live, unelevated "is X currently
installed" query at all - is_installed() below instead trusts a small local
cache (settings_store.py's "ocr_installed_locales") of locales this app has
itself successfully installed and verified *from within an already-elevated
session* (see install()). A locale already installed before the user ever
opened this app's own OCR-language dialog (e.g. it shipped with the OS) won't
show up as installed until Install is clicked on it once - harmless, since
Add-WindowsCapability on an already-installed capability just succeeds
immediately (confirmed live).

*Installing* a capability needs admin outright - `Add-WindowsCapability`
requires it, so install() triggers a real UAC consent prompt via
ShellExecuteW's "runas" verb rather than trying to run elevated all the time
or bypass consent - the user must see and approve that prompt themselves;
there is no way around that, and there shouldn't be.
"""
import win32con
import win32event
import win32process
from win32comext.shell import shellcon
from win32comext.shell.shell import ShellExecuteEx

import settings_store
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


def is_installed(locale):
    """Trusts settings_store's "ocr_installed_locales" cache rather than
    querying DISM live - see this module's docstring for why a live query
    can't work here at all under a real (non-dev-SSH) user token."""
    return locale in settings_store.load().get("ocr_installed_locales", [])


def _mark_installed(locale, installed):
    settings = settings_store.load()
    locales = set(settings.get("ocr_installed_locales", []))
    if installed:
        locales.add(locale)
    else:
        locales.discard(locale)
    settings["ocr_installed_locales"] = sorted(locales)
    settings_store.save(settings)


def _run_elevated_ps(ps_command, timeout_s):
    """Shared elevation/wait/exit-code plumbing for install() and
    uninstall() - both need the identical UAC/hidden-window dance, just with
    a different PowerShell body and different result messages.

    Returns (exit_code, None) once the elevated process actually exits, or
    (None, (False, message)) if it never got that far (UAC declined or the
    wait itself timed out) - the caller returns that tuple as-is in that case.

    -WindowStyle Hidden, not just ShellExecuteEx's own nShow=SW_HIDE below:
    confirmed live 2026-08-28 that nShow alone still showed a real visible
    console window that kept reappearing after being closed - Windows
    Terminal (when set as the default terminal host, common on a
    personally-configured Windows 10/11 box) creates its own top-level
    window for a hosted console session and doesn't honor the launching
    process's ShowWindow hint. -WindowStyle Hidden is powershell.exe's own
    request for its console, independent of whatever ends up hosting it."""
    ps_args = f"-NoProfile -WindowStyle Hidden -Command \"{ps_command}\""
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
        return None, (False, t("ocr_lang.uac_declined", error=exc))

    handle = proc_info["hProcess"]
    wait_result = win32event.WaitForSingleObject(handle, int(timeout_s * 1000))
    if wait_result == win32event.WAIT_TIMEOUT:
        return None, (False, t("ocr_lang.timeout", timeout=timeout_s))

    return win32process.GetExitCodeProcess(handle), None


def install(locale, timeout_s=600):
    """Triggers a real UAC consent prompt and blocks until the elevated
    Add-WindowsCapability process exits (see _run_elevated_ps()).

    Returns (ok, message). ok=False + a clear message if the user declines
    the UAC prompt, or if the elevated install command itself fails/times
    out.

    The confirmation check (did it actually end up Installed?) runs *inside*
    this same elevated PowerShell session, not via a separate later query -
    see this module's docstring: a non-elevated Get-WindowsCapability -Online
    doesn't just lag briefly here, it flat-out requires admin rights under a
    real user token, always. Checking from within the process that's already
    elevated is the only way to get a real answer at all, not just a
    workaround for a timing issue. On confirmed success, this also updates
    settings_store's "ocr_installed_locales" cache that is_installed() reads
    from - the only place that cache is ever set.
    """
    capability = _capability_name(locale)
    ps_command = (
        f"try {{ Add-WindowsCapability -Online -Name {capability} -ErrorAction Stop | Out-Null }} "
        f"catch {{ exit 2 }}; "
        f"for ($i = 0; $i -lt 6; $i++) {{ "
        f"if ((Get-WindowsCapability -Online -Name {capability}).State -eq 'Installed') {{ exit 0 }}; "
        f"Start-Sleep -Seconds 2 }}; "
        f"exit 1"
    )
    exit_code, early_failure = _run_elevated_ps(ps_command, timeout_s)
    if early_failure is not None:
        return early_failure
    if exit_code == 0:
        _mark_installed(locale, True)
        return True, t("ocr_lang.install_success")
    if exit_code == 1:
        return False, t("ocr_lang.install_unconfirmed")
    return False, t("ocr_lang.install_exit_error", code=exit_code)


def uninstall(locale, timeout_s=600):
    """Mirror of install() for Remove-WindowsCapability - same UAC/hidden-
    window/in-session-confirmation approach and the same reasons (see
    install()'s and this module's docstrings). Added so a user who installed
    a language just to test this dialog (or the wrong one by mistake) has a
    way to actually undo that, short of digging into Windows Settings
    directly - there was previously no way to remove a downloaded language
    component from inside this app at all."""
    capability = _capability_name(locale)
    ps_command = (
        f"try {{ Remove-WindowsCapability -Online -Name {capability} -ErrorAction Stop | Out-Null }} "
        f"catch {{ exit 2 }}; "
        f"for ($i = 0; $i -lt 6; $i++) {{ "
        f"if ((Get-WindowsCapability -Online -Name {capability}).State -ne 'Installed') {{ exit 0 }}; "
        f"Start-Sleep -Seconds 2 }}; "
        f"exit 1"
    )
    exit_code, early_failure = _run_elevated_ps(ps_command, timeout_s)
    if early_failure is not None:
        return early_failure
    if exit_code == 0:
        _mark_installed(locale, False)
        return True, t("ocr_lang.uninstall_success")
    if exit_code == 1:
        return False, t("ocr_lang.uninstall_unconfirmed")
    return False, t("ocr_lang.uninstall_exit_error", code=exit_code)
