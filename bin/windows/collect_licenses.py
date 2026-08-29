"""Copies the license file of each bundled third-party dependency into
licenses/<distribution>/ so the packaged distribution can ship them verbatim
alongside THIRD_PARTY_LICENSES.md, instead of hand-copied license text here
going stale as dependency versions change.

Run after `pip install -r requirements.txt` in the same environment that
will be handed to PyInstaller, from bin/windows/:

    python collect_licenses.py [output_dir]

output_dir defaults to ./licenses (bin/windows/licenses/), which
build_dist.ps1 copies into the final dist/LiveTranslator-kun/ folder.
"""
import sys
from importlib import metadata
from pathlib import Path

# Every PyPI distribution actually installed for this app - see
# requirements.txt's direct deps plus what they pull in transitively.
DISTRIBUTIONS = [
    "PySide6",
    "PySide6-Essentials",
    "shiboken6",
    "pywin32",
    "dxcam",
    "numpy",
    "opencv-python",
    "comtypes",
    "winrt-runtime",
    "winrt-Windows.Foundation",
    "winrt-Windows.Foundation.Collections",
    "winrt-Windows.Globalization",
    "winrt-Windows.Graphics.Imaging",
    "winrt-Windows.Media.Ocr",
    "winrt-Windows.Security.Cryptography",
    "winrt-Windows.Storage.Streams",
    "pywin32-ctypes",
]

LICENSE_FILENAMES = {"LICENSE", "LICENSE.txt", "LICENSE.md", "COPYING", "COPYING.txt"}

# The winrt-* packages (pywinrt) declare "MIT" via their PyPI classifier but
# don't ship a LICENSE file inside their own dist-info (confirmed live
# 2026-08-28 - every one of them hit the "no LICENSE file shipped" case
# below). Fetched verbatim from https://github.com/pywinrt/pywinrt/blob/main/LICENSE
# on 2026-08-28 as a fallback so the shipped distribution still carries the
# actual text instead of just a classifier and a pointer to go look it up.
PYWINRT_LICENSE_TEXT = '''MIT License

Copyright (c) Microsoft Corporation. All rights reserved.
Copyright (c) 2021-2025 David Lechner <david@pybricks.com>

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE
'''


def _normalize(name):
    # PEP 503 normalization - matches "winrt-Windows.Media.Ocr" (our name)
    # against dist-info Name fields like "winrt_windows_media_ocr" (the
    # installed form). Needed because plain importlib.metadata.distribution()
    # on Python 3.9 does not reliably normalize dots/underscores/hyphens
    # against each other (confirmed live 2026-08-28: it silently missed
    # every winrt-* package, which very much are installed).
    import re

    return re.sub(r"[-_.]+", "-", name).lower()


def main():
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "licenses"
    out_dir.mkdir(parents=True, exist_ok=True)

    installed = {_normalize(d.metadata["Name"]): d for d in metadata.distributions() if d.metadata["Name"]}

    missing = []
    for dist_name in DISTRIBUTIONS:
        dist = installed.get(_normalize(dist_name))
        if dist is None:
            missing.append(dist_name)
            continue

        dest = out_dir / dist_name
        dest.mkdir(exist_ok=True)

        found_license_file = False
        for f in dist.files or []:
            if f.name in LICENSE_FILENAMES or "LICENSE" in f.name.upper():
                src = dist.locate_file(f)
                if Path(src).is_file():
                    (dest / f.name).write_bytes(Path(src).read_bytes())
                    found_license_file = True

        license_text = dist.metadata.get("License", "") or ""
        classifiers = [c for c in dist.metadata.get_all("Classifier", []) if "License" in c]
        (dest / "PACKAGE_INFO.txt").write_text(
            f"{dist_name} {dist.version}\n"
            f"License field: {license_text}\n"
            f"Classifiers: {classifiers}\n",
            encoding="utf-8",
        )

        if not found_license_file and dist_name.lower().startswith("winrt"):
            (dest / "LICENSE").write_text(PYWINRT_LICENSE_TEXT, encoding="utf-8")
            found_license_file = True

        if not found_license_file:
            print(f"[collect_licenses] no LICENSE file shipped in {dist_name}'s dist-info "
                  f"- see its PACKAGE_INFO.txt and check the project's repo directly")

    if missing:
        print(f"[collect_licenses] not installed, skipped: {', '.join(missing)}")

    print(f"[collect_licenses] wrote license info for "
          f"{len(DISTRIBUTIONS) - len(missing)}/{len(DISTRIBUTIONS)} packages to {out_dir}")


if __name__ == "__main__":
    main()
