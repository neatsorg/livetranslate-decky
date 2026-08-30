# PyInstaller spec for the Windows tray app. Build from bin/windows/ with:
#   pyinstaller livetranslate.spec --noconfirm
#
# Produces a one-folder ("onedir") build under dist/LiveTranslator-kun/ -
# not a single exe. That's deliberate, not a limitation: PySide6 is LGPLv3,
# and onedir keeps its DLLs as ordinary files a user could swap out, which is
# the straightforward way to stay license-clean (see ../../THIRD_PARTY_LICENSES.md).
# A single exe would need extra unpack/repack steps to get the same property.
#
# APPDATA%/LiveTranslator-kun/settings.json is created on first run
# (settings_store.py) - nothing in this spec needs to seed it.

import sys
from pathlib import Path

block_cipher = None

# bin/ (providers/, region_tracker.py, translate_server.py, ocr_worker.py)
# and bin/windows/ (the app itself) both need to be on the analysis path -
# the app's own modules already add both to sys.path.insert() at runtime for
# the same reason.
BIN_DIR = Path(SPECPATH).parent
WINDOWS_DIR = Path(SPECPATH)

hiddenimports = [
    # pywin32: win32timezone is imported lazily by pywintypes/win32com for
    # datetime marshalling and is a well-known PyInstaller+pywin32 miss.
    "win32timezone",
]

# pywinrt ships one PyPI package per WinRT namespace, installed as PEP 420
# implicit namespace packages under `winrt.*` (no __init__.py) - PyInstaller's
# static import analysis can miss their compiled .pyd extension modules, so
# collect each namespace this app actually imports explicitly rather than
# trusting Analysis to find them on its own.
datas = []
binaries = []
for pkg in (
    "winrt",
    "winrt.windows.foundation",
    "winrt.windows.globalization",
    "winrt.windows.graphics.imaging",
    "winrt.windows.media.ocr",
    "winrt.windows.security.cryptography",
):
    try:
        from PyInstaller.utils.hooks import collect_all

        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

a = Analysis(
    ["tray_app.py"],
    pathex=[str(BIN_DIR), str(WINDOWS_DIR)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LiveTranslator-kun",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # tray app - equivalent to launching via pythonw.exe
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="LiveTranslator-kun",
)
