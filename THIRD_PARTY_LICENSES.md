# Third-Party Licenses (Windows build)

The Windows distribution of LiveTranslator-kun (the `bin/windows/` tray app)
bundles the following third-party packages inside `dist/LiveTranslator-kun/`.
This project itself is [GPLv3](LICENSE); none of these change that, but each
keeps its own license and copyright, reproduced verbatim under `licenses/<name>/`
in every built distribution (see `bin/windows/collect_licenses.py`, run as
part of the Windows build - see `docs/BUILDING_WINDOWS.md`).

| Package | License | Why it's here |
| --- | --- | --- |
| [PySide6](https://pypi.org/project/PySide6/) (Qt for Python) | LGPLv3 | Tray icon, HUD overlay, and Settings/Keybindings UI |
| [pywin32](https://github.com/mhammond/pywin32) | PSF-2.0 (mixed; some submodules BSD-3-Clause/MIT/LGPL-2.1+ - see its own license files) | Window discovery (`win32gui`), single-instance lock (`win32event`), hidden-launch support |
| [DXcam](https://github.com/ra1nty/DXcam) | MIT | Screen capture via the Desktop Duplication API |
| [NumPy](https://numpy.org/) | BSD-3-Clause | Pulled in by DXcam for frame buffers |
| [opencv-python](https://github.com/opencv/opencv-python) | Apache-2.0 (bundles further third-party components - see its own `LICENSE-3RD-PARTY.txt`) | Pulled in by DXcam (`numpy_processor.py`'s color-space conversion) |
| [comtypes](https://github.com/enthought/comtypes) | MIT | Pulled in by DXcam for its DXGI/D3D11 COM bindings |
| [pywinrt](https://github.com/pywinrt/pywinrt) (`winrt-*` packages) | MIT | Windows.Media.Ocr bindings - the app's only OCR engine |
| [pywin32-ctypes](https://github.com/pywin32-ctypes-org/pywin32-ctypes) | BSD-3-Clause | Pulled in transitively (pyinstaller-hooks-contrib's Windows tooling) |

## Why Chrome Screen AI isn't one of these

The Linux/Decky side of this project (see the main [README](README.md)) uses
Chrome's on-device Screen AI model as its default OCR engine. The Windows
port deliberately does **not** bundle or depend on it - it was dropped in
favor of Windows' own built-in `Windows.Media.Ocr` (see
`bin/windows/settings_store.py`'s `DEFAULTS["ocr_engine"]` comment), both
because it's faster on this platform and because its licensing/redistribution
terms as a Chrome component are a worse fit for a standalone installable
package than an OS API already present on every target machine.

## PySide6 / Qt and LGPLv3

PySide6 is LGPLv3-licensed. The Windows build is deliberately packaged as a
"onedir" folder (`dist/LiveTranslator-kun/`), not a single exe, specifically
so its Qt DLLs remain ordinary files on disk rather than something baked into
one binary - see `bin/windows/livetranslate.spec`'s header comment. Qt's own
LGPL compliance guidance is at
<https://www.qt.io/blog/technically-lgpl-licensed> and
<https://doc.qt.io/qt-6/lgpl.html>.
