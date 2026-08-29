# Building the Windows distribution

This produces a folder a user can unzip and run without installing Python -
`dist/LiveTranslator-kun/LiveTranslator-kun.exe` plus everything it needs
alongside it. It's a "onedir" PyInstaller build, not a single exe - see
`bin/windows/livetranslate.spec`'s header comment for why (short version:
PySide6 is LGPLv3, and onedir keeps its DLLs as swappable files instead of
baking them into one binary).

Must be built **on Windows** - PySide6/pywin32/dxcam/winrt all ship
Windows-only compiled extensions, so this can't be cross-built from Linux.

## Steps

1. On a Windows machine with Python 3.10+ installed:
   ```powershell
   cd bin\windows
   .\build_dist.ps1
   ```
   This creates an isolated venv (`.build-venv`, gitignored), installs
   `requirements.txt` + PyInstaller into it, runs `collect_licenses.py` to
   pull each dependency's own license file into `licenses/`, runs
   PyInstaller, then copies `LICENSE` + `THIRD_PARTY_LICENSES.md` +
   `licenses/` into the built folder and zips it as
   `dist/LiveTranslator-kun-windows-<VERSION>.zip`.

2. Smoke-test before shipping:
   - Run `dist\LiveTranslator-kun\LiveTranslator-kun.exe` directly (not from
     an SSH session - screen capture needs an interactive desktop session,
     see the SSH host notes if you're doing this remotely).
   - Confirm the tray icon appears, Settings opens, and OCR actually produces
     text over a real window (Windows.Media.Ocr needs its language component
     installed as a Windows optional feature - `windows_ocr_lang.py` checks
     for this and `settings_dialog.py` surfaces "Manage OCR languages..." if
     it's missing; a completely clean machine without the English/Japanese
     OCR language pack installed is the realistic worst case to test).
   - Check `%APPDATA%\LiveTranslator-kun\` gets created on first run
     (`settings_store.py`) with no left-over reference to the old ScreenAI
     engine in a stale `settings.json` from before 2026-08-28's removal -
     `settings_dialog.py`/`pipeline_loop.py` fall back to `windows_ocr`
     either way, but worth a look at least once.

3. If PyInstaller can't find `winrt`, `dxcam`, or `providers`/`region_tracker`
   correctly, see the spec file's comments first - the winrt packages are PEP
   420 namespace packages, which is the usual reason PyInstaller misses their
   compiled extension modules.

## What end users need on their machine

Nothing extra to install for OCR/capture (Desktop Duplication API and
Windows.Media.Ocr are both already part of Windows 10/11) - only the
target language's OCR component if it isn't already installed, which the
app's own Settings dialog links out to.

## Verified live (2026-08-28)

Built and smoke-tested end to end on the win10 laptop with the exact pins in
`requirements.txt` (Python 3.9.13, PyInstaller 6.14.2): the onedir build is
~281MB uncompressed / ~114MB zipped (432 files). Launched via a Task
Scheduler task running on the interactive desktop (see the SSH host notes -
a plain SSH session can't reach the desktop session dxcam/display APIs
need), confirmed from `%APPDATA%\LiveTranslator-kun\logs\tray_app.log`:
tray icon shown, OCR blocks discovered and translated, HUD labels rendered,
keybinding detection working, no tracebacks. `dxcam`'s COM interfaces are
defined statically (`dxcam/_libs/dxgi.py`), not via `comtypes.client`
dynamic codegen, so the usual PyInstaller+comtypes `comtypes.gen` frozen-app
gotcha doesn't apply here - confirmed by the absence of any comtypes errors
in a real run, not just by reading the source.

One thing worth knowing about, unrelated to packaging: a self-healing
watchdog (`[watchdog] tick() has not run in ...s - main thread appears
genuinely stuck, forcing process exit`) fired once during this test run and
the app restarted cleanly on its own. Whether that stall itself has a root
cause worth chasing is a separate question from whether the frozen build
works - it does.
