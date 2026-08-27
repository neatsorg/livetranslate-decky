"""One-shot smoke test: download the Windows build of Chrome's Screen AI
component from CIPD and inspect what's actually inside the zip - the exact
DLL filename/layout on Windows hasn't been confirmed yet, only that the
`chromium/third_party/screen-ai/windows-amd64` package exists (verified via
a direct ResolveVersion pRPC call from outside this machine).

Not the final downloader (see screenai_downloader.py on the Linux side for
the real progress/cancel/resume-safe version) - just answers "what do we get".
"""
import json
import os
import re
import ssl
import sys
import zipfile
from urllib import error as urlerror
from urllib import request as urlrequest

CIPD_PACKAGE = "chromium/third_party/screen-ai/windows-amd64"
CIPD_VERSION = "latest"
CIPD_HOST = "https://chrome-infra-packages.appspot.com"
PRPC_RESOLVE_URL = f"{CIPD_HOST}/prpc/cipd.Repository/ResolveVersion"
PRPC_GET_URL = f"{CIPD_HOST}/prpc/cipd.Repository/GetInstanceURL"
_PRPC_PREFIX = b")]}'"


def prpc_post(url, payload):
    req = urlrequest.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urlrequest.urlopen(req, timeout=30, context=ssl.create_default_context()) as resp:
        body = resp.read()
    if body.startswith(_PRPC_PREFIX):
        body = body[len(_PRPC_PREFIX):]
    return json.loads(body.decode("utf-8"))


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "screen_ai_win"
    zip_path = out_dir + ".zip"

    print(f"Resolving {CIPD_PACKAGE}@{CIPD_VERSION}...")
    resolved = prpc_post(PRPC_RESOLVE_URL, {"package": CIPD_PACKAGE, "version": CIPD_VERSION})
    instance = resolved["instance"]
    print(f"instance: {instance}")

    url_resp = prpc_post(PRPC_GET_URL, {"package": CIPD_PACKAGE, "instance": instance})
    signed_url = url_resp["signedUrl"]
    print(f"signed URL obtained, downloading to {zip_path}...")

    with urlrequest.urlopen(signed_url, timeout=120, context=ssl.create_default_context()) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        print(f"Content-Length: {total} bytes ({total / 1e6:.1f} MB)")
        downloaded = 0
        with open(zip_path, "wb") as f:
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
        print(f"downloaded {downloaded} bytes")

    print(f"extracting to {out_dir}...")
    os.makedirs(out_dir, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
        zf.extractall(out_dir)

    print(f"=== zip contents ({len(names)} entries) ===")
    for n in names:
        print(n)

    print("=== dll/so files found on disk ===")
    for root, _dirs, files in os.walk(out_dir):
        for name in files:
            if re.search(r"\.(dll|so)$", name, re.IGNORECASE):
                full = os.path.join(root, name)
                print(f"{full}  ({os.path.getsize(full)} bytes)")


if __name__ == "__main__":
    main()
