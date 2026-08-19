#!/usr/bin/env python3
"""Fetches libchromescreenai.so + its TFLite models from Google's CIPD
server (the same public distribution point Chromium itself pulls this
component from at build time). Not bundled in this repo - it's ~120MB of
prebuilt binary + model weights, downloaded on demand into a models
directory the caller supplies (main.py points this at data_dir/models so it
survives plugin reinstalls the same way translation_settings.json does).

CIPD is Chromium's package distribution service, not a private Google API -
same idea as pulling a release asset off GitHub. There is no formal support
contract for it though, so treat this as an unofficial-but-public
dependency: the URL scheme or package path could change upstream.

Uses urllib only (no requests dependency) - this module runs inside
main.py, the Decky plugin backend process, whose Python environment can't
be assumed to have third-party packages installed (unlike the distrobox
this bin/ directory's other scripts run in, which already pulls in
Pillow/tesserocr etc. for ocr_worker.py).
"""
import json
import logging
import os
import re
import shutil
import threading
import zipfile
from typing import Dict, Optional
from urllib import error as urlerror
from urllib import request as urlrequest

logger = logging.getLogger(__name__)

# CIPD's Linux build of this package is x86_64-only, which covers both the
# Steam Deck (Zen2) and the dev/aegis boxes this project runs on.
CIPD_PACKAGE = "chromium/third_party/screen-ai/linux"
CIPD_VERSION = "latest"  # tracks Chrome Stable; pin to a hexdigest to freeze
CIPD_HOST = "https://chrome-infra-packages.appspot.com"
PRPC_RESOLVE_URL = f"{CIPD_HOST}/prpc/cipd.Repository/ResolveVersion"
PRPC_GET_URL = f"{CIPD_HOST}/prpc/cipd.Repository/GetInstanceURL"

# pRPC (Chrome infra's JSON-over-HTTP RPC convention) prefixes every
# response body with this to defeat JSON-hijacking in browser contexts;
# harmless here but must be stripped before json.loads.
_PRPC_PREFIX = b")]}'"

MODEL_DIR_NAME = "screen_ai"
RESOURCES_SUBDIR = "resources"  # the CIPD zip nests everything under this
REQUIRED_FILES = (os.path.join(RESOURCES_SUBDIR, "libchromescreenai.so"),)
APPROX_SIZE_MB = 120  # shown before the real Content-Length is known


class ScreenAIDownloader:
    """Downloads and manages the on-disk Chrome Screen AI package.

    `models_dir` is the shared parent models/ directory; this extracts into
    `models_dir/screen_ai/`. All the download state (progress/error/cancel)
    is plain attributes behind a lock, polled by main.py's status endpoint
    rather than pushed - matches how translation settings are read (no
    event bus in this plugin, just periodic polling from the frontend).
    """

    def __init__(self, models_dir: str):
        self._models_dir = models_dir
        self._target_dir = os.path.join(models_dir, MODEL_DIR_NAME)

        self._lock = threading.Lock()
        self._downloading = False
        self._progress = 0.0  # 0.0-1.0
        self._error: Optional[str] = None
        self._cancel_requested = False
        self._thread: Optional[threading.Thread] = None

        os.makedirs(models_dir, exist_ok=True)
        self._cleanup_partial()

    def _cleanup_partial(self):
        """Remove leftovers from a download that was killed mid-flight (e.g.
        plugin reload during a fetch)."""
        try:
            for name in os.listdir(self._models_dir):
                if name.endswith(".downloading") or name.endswith(".tmpzip"):
                    path = os.path.join(self._models_dir, name)
                    if os.path.isdir(path):
                        shutil.rmtree(path, ignore_errors=True)
                    else:
                        try:
                            os.remove(path)
                        except OSError:
                            pass
        except OSError as exc:
            logger.warning("screenai_downloader: cleanup_partial failed: %s", exc)

    def get_resources_dir(self) -> str:
        """The .so resolves its own TFLite model paths relative to itself,
        so ocr_screenai.py must be pointed at this directory, not
        get_target_dir()."""
        return os.path.join(self._target_dir, RESOURCES_SUBDIR)

    def is_installed(self) -> bool:
        return all(os.path.exists(os.path.join(self._target_dir, f)) for f in REQUIRED_FILES)

    def get_install_size(self) -> int:
        if not os.path.isdir(self._target_dir):
            return 0
        total = 0
        for root, _dirs, files in os.walk(self._target_dir):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
        return total

    def get_status(self) -> Dict:
        with self._lock:
            return {
                "installed": self.is_installed(),
                "size_bytes": self.get_install_size(),
                "approx_size_mb": APPROX_SIZE_MB,
                "downloading": self._downloading,
                "progress": self._progress,
                "error": self._error,
            }

    def start_download(self) -> bool:
        with self._lock:
            if self._downloading:
                return False
            self._downloading = True
            self._progress = 0.0
            self._error = None
            self._cancel_requested = False
        self._thread = threading.Thread(target=self._download, daemon=True)
        self._thread.start()
        return True

    def cancel_download(self):
        with self._lock:
            self._cancel_requested = True

    def clear_error(self):
        with self._lock:
            self._error = None

    def delete(self) -> bool:
        if not os.path.isdir(self._target_dir):
            return True
        try:
            shutil.rmtree(self._target_dir)
            return True
        except OSError as exc:
            logger.error("screenai_downloader: delete failed: %s", exc)
            return False

    def _prpc_post(self, url: str, payload: dict) -> dict:
        req = urlrequest.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urlrequest.urlopen(req, timeout=30) as resp:
                body = resp.read()
        except urlerror.HTTPError as exc:
            raise RuntimeError(f"CIPD pRPC HTTP {exc.code}: {exc.read()[:200]}") from exc
        except urlerror.URLError as exc:
            raise RuntimeError(f"Could not reach chrome-infra-packages.appspot.com: {exc.reason}") from exc
        if body.startswith(_PRPC_PREFIX):
            body = body[len(_PRPC_PREFIX):]
        return json.loads(body.decode("utf-8"))

    def _resolve_signed_url(self) -> str:
        if re.fullmatch(r"[0-9a-f]{64}", CIPD_VERSION):
            hash_algo, hex_digest = "SHA256", CIPD_VERSION
        else:
            resolved = self._prpc_post(
                PRPC_RESOLVE_URL, {"package": CIPD_PACKAGE, "version": CIPD_VERSION}
            )
            instance = resolved.get("instance") or {}
            hash_algo, hex_digest = instance.get("hashAlgo"), instance.get("hexDigest")
            if not hash_algo or not hex_digest:
                raise RuntimeError(f"CIPD ResolveVersion returned no instance: {resolved}")

        url_resp = self._prpc_post(
            PRPC_GET_URL,
            {"package": CIPD_PACKAGE, "instance": {"hashAlgo": hash_algo, "hexDigest": hex_digest}},
        )
        signed_url = url_resp.get("signedUrl")
        if not signed_url:
            raise RuntimeError(f"CIPD GetInstanceURL returned no URL: {url_resp}")
        return signed_url

    def _download(self):
        zip_path = os.path.join(self._models_dir, f"{MODEL_DIR_NAME}.tmpzip")
        staging_dir = os.path.join(self._models_dir, f"{MODEL_DIR_NAME}.downloading")

        try:
            signed_url = self._resolve_signed_url()
            if self._cancel_requested:
                raise RuntimeError("Download cancelled")

            try:
                resp = urlrequest.urlopen(signed_url, timeout=60)
            except urlerror.HTTPError as exc:
                raise RuntimeError(f"HTTP {exc.code} fetching Chrome Screen AI package") from exc
            except urlerror.URLError as exc:
                raise RuntimeError(f"Could not reach the CIPD download URL: {exc.reason}") from exc

            with resp:
                total = int(resp.headers.get("Content-Length") or 0)
                downloaded = 0
                with open(zip_path, "wb") as f:
                    while True:
                        if self._cancel_requested:
                            raise RuntimeError("Download cancelled")
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total > 0:
                            with self._lock:
                                # Cap at 95% - unzip below still needs to run.
                                self._progress = min(0.95, downloaded / total * 0.95)

            if self._cancel_requested:
                raise RuntimeError("Download cancelled")

            if os.path.exists(staging_dir):
                shutil.rmtree(staging_dir, ignore_errors=True)
            os.makedirs(staging_dir, exist_ok=True)

            try:
                with zipfile.ZipFile(zip_path, "r") as zf:
                    zf.extractall(staging_dir)
            except zipfile.BadZipFile as exc:
                raise RuntimeError("Downloaded Chrome Screen AI package is not a valid zip") from exc

            # CIPD packages carry a .cipdpkg manifest dir; not needed at runtime.
            cipd_meta = os.path.join(staging_dir, ".cipdpkg")
            if os.path.isdir(cipd_meta):
                shutil.rmtree(cipd_meta, ignore_errors=True)

            for req in REQUIRED_FILES:
                if not os.path.exists(os.path.join(staging_dir, req)):
                    raise RuntimeError(f"Missing required file after extract: {req}")

            so_path = os.path.join(staging_dir, RESOURCES_SUBDIR, "libchromescreenai.so")
            try:
                os.chmod(so_path, 0o755)
            except OSError:
                pass

            if os.path.exists(self._target_dir):
                shutil.rmtree(self._target_dir)
            os.rename(staging_dir, self._target_dir)

            try:
                os.remove(zip_path)
            except OSError:
                pass

            with self._lock:
                self._progress = 1.0
                self._downloading = False
            logger.info("Chrome Screen AI package downloaded to %s", self._target_dir)

        except Exception as exc:
            for path in (zip_path, staging_dir):
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                elif os.path.isfile(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
            with self._lock:
                self._error = str(exc)
                self._downloading = False
            logger.error("Chrome Screen AI download failed: %s", exc)
