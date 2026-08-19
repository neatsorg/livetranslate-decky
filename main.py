import asyncio
import base64
import importlib.util
import json
import os
import re
import select
import signal
import struct
import subprocess
import time
from urllib import error as urlerror
from urllib import parse, request
from pathlib import Path

import decky

_GAME_ID_RE = re.compile(r"^[A-Za-z0-9_]+$")
_DEFAULT_GAME_ID = "enigma_of_fear"

# Matches index.tsx's STATUS_TOAST_SUPPRESS_MS - see refresh_dynamic_capture().
_STATUS_TOAST_SUPPRESS_S = 2.5

_DEFAULT_TRANSLATION_SETTINGS = {
    "engine": "ollama",
    "source_lang": "English",
    "target_lang": "Japanese",
    "ollama": {},
    "gemini": {"api_key": "", "model": "gemini-3.6-flash"},
    "google": {},
    "google_cloud": {"api_key": ""},
    "deepl": {"api_key": ""},
}
_SENSITIVE_SETTING_KEYS = {"api_key"}

_DEFAULT_OCR_SETTINGS = {
    # "chromescreenai" (default - on-device neural OCR, downloaded on demand,
    # see screenai_downloader.py) or "tesseract" (kept around as a debug
    # fallback; Tesseract also needs setup work on a fresh Steam Deck, so it
    # has no real zero-install advantage over chromescreenai as a default -
    # see project_playtranslate_multi_engine_design). Only affects
    # /discover_blocks (Dynamic Capture's full-frame text discovery); the
    # fixed-region calibration path stays on Tesseract either way.
    "engine": "chromescreenai",
    "chromescreenai": {"min_confidence": 0.5},
}

# The 18 bindable keys for the keybinding feature (no d-pad - see
# keybinding UI design discussion). Dynamic Capture Start/Stop is
# deliberately never bindable, UI-button only.
_VALID_KEYS = {
    "A", "B", "X", "Y",
    "L1", "L2", "L3", "L4", "L5",
    "R1", "R2", "R3", "R4", "R5",
    "select", "start",
    "trackpad_left_tap", "trackpad_right_tap",
}
_VALID_COMMANDS = {"refresh", "pause_resume", "touch_translate"}

# Reproduces today's hardcoded index.tsx behavior exactly: L4 alone (no
# long-press) refreshes, L4 alone held past 900ms pauses/resumes, L4+L2
# held (while paused) enters tap-translate mode.
_DEFAULT_KEYBINDING_SETTINGS = {
    "bindings": [
        {"id": "default_refresh", "command": "refresh", "keys": ["L4"], "long_press": False, "threshold_ms": 900},
        {"id": "default_pause_resume", "command": "pause_resume", "keys": ["L4"], "long_press": True, "threshold_ms": 900},
        {"id": "default_touch_translate", "command": "touch_translate", "keys": ["L4", "L2"], "long_press": False, "threshold_ms": 900},
    ]
}


class Plugin:
    def __init__(self):
        self.process = None
        self.log_file = None
        self.translation_worker_task = None
        self.translation_worker_running = False
        self.translation_in_progress = False
        self.last_translated_image_mtime_ns = None
        self.ocr_worker_process = None
        self.ocr_worker_log_file = None
        self.dynamic_process = None
        self.dynamic_log_file = None
        self.translate_server_process = None
        self.translate_server_log_file = None
        self.dynamic_refresh_lock = asyncio.Lock()
        self.plugin_dir = Path(__file__).resolve().parent
        self.data_dir = Path(getattr(decky, "DECKY_PLUGIN_DIR", self.plugin_dir)).parent.parent / "data" / "PlayTranslate"
        self.log_path = self.data_dir / "playtranslate-capture.log"
        self.translation_path = self.data_dir / "last_translation.txt"
        self.translation_json_path = self.data_dir / "last_translation.json"
        self.translation_error_path = self.data_dir / "last_translation_error.txt"
        self.translate_url_path = self.data_dir / "translate_url.txt"
        self.translation_settings_path = self.data_dir / "translation_settings.json"
        self.ocr_settings_path = self.data_dir / "ocr_settings.json"
        self.screenai_downloader = None
        self.active_blocks_path = self.data_dir / "active_blocks.json"
        self.dynamic_config_path = self.data_dir / "dynamic_config.json"
        self.dynamic_log_path = self.data_dir / "playtranslate-dynamic-capture.log"
        self.dynamic_pause_flag_path = self.data_dir / "dynamic_paused.flag"
        self.dynamic_qam_open_flag_path = self.data_dir / "dynamic_qam_open.flag"
        self.dynamic_status_toast_flag_path = self.data_dir / "dynamic_status_toast.flag"
        self._status_toast_clear_task = None
        self.tap_request_path = self.data_dir / "tap_request.json"
        self.tap_result_path = self.data_dir / "tap_result.json"
        self.dynamic_translation_config_path = self.data_dir / "dynamic_translation_config.json"
        self.keybinding_settings_path = self.data_dir / "keybindings.json"
        self.hidraw_path = None

    async def _main(self):
        decky.logger.info("PlayTranslate loaded")

    async def _unload(self):
        await asyncio.gather(self.stop_capture(), self.stop_dynamic_capture(), self._stop_translate_server())
        decky.logger.info("PlayTranslate unloaded")

    async def _uninstall(self):
        await asyncio.gather(self.stop_capture(), self.stop_dynamic_capture(), self._stop_translate_server())

    def _candidate_engine_dirs(self):
        env_dir = os.environ.get("PLAYTRANSLATE_ENGINE_DIR")
        if env_dir:
            yield Path(env_dir)

        yield self.plugin_dir / "bin"
        yield self.plugin_dir.parent / "playtranslate-deck"
        yield Path("/home/deck/project/playtranslate-deck")
        yield Path("/home/user/project/playtranslate-deck")

    def _find_engine(self):
        for engine_dir in self._candidate_engine_dirs():
            capture_py = engine_dir / "capture.py"
            config_json = engine_dir / "config.json"
            if capture_py.exists() and config_json.exists():
                return engine_dir, capture_py, config_json
        return None, None, None

    def _find_dynamic_capture_script(self):
        for engine_dir in self._candidate_engine_dirs():
            script = engine_dir / "capture_dynamic.py"
            if script.exists():
                return engine_dir, script
        return None, None

    def _is_running(self):
        return self.process is not None and self.process.poll() is None

    def _is_dynamic_running(self):
        return self.dynamic_process is not None and self.dynamic_process.poll() is None

    def _find_processes_by_script(self, script_path):
        script_path = str(script_path)
        pids = []
        current_pid = os.getpid()
        for proc_dir in Path("/proc").iterdir():
            if not proc_dir.name.isdigit():
                continue
            pid = int(proc_dir.name)
            if pid == current_pid:
                continue
            try:
                cmdline = (proc_dir / "cmdline").read_bytes().decode("utf-8", errors="replace")
            except OSError:
                continue
            parts = [part for part in cmdline.split("\0") if part]
            if script_path in parts:
                pids.append(pid)
        return pids

    def _find_capture_processes(self, capture_py):
        return self._find_processes_by_script(capture_py)

    async def _stop_processes_by_script(self, script_path, label):
        pids = self._find_processes_by_script(script_path)
        if not pids:
            return

        decky.logger.info(f"Stopping existing PlayTranslate {label} processes: {pids}")
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

        for _ in range(20):
            await asyncio.sleep(0.1)
            if not self._find_processes_by_script(script_path):
                return

        for pid in self._find_processes_by_script(script_path):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    async def _stop_existing_capture_processes(self, capture_py):
        await self._stop_processes_by_script(capture_py, "capture")

    def _tail_log(self, lines=40):
        if not self.log_path.exists():
            return ""
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"Could not read log: {exc}"
        return "\n".join(text.splitlines()[-lines:])

    def _capture_env(self):
        env = os.environ.copy()
        env.setdefault("XDG_RUNTIME_DIR", "/run/user/1000")
        env.setdefault("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
        env["PATH"] = "/usr/local/bin:/usr/bin:/bin:" + env.get("PATH", "")
        return env

    def _subprocess_env(self):
        env = self._capture_env()
        for key in (
            "LD_LIBRARY_PATH",
            "LD_PRELOAD",
            "PYTHONHOME",
            "PYTHONPATH",
        ):
            env.pop(key, None)
        return env

    def _read_text_file(self, path):
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError as exc:
            return f"Could not read {path.name}: {exc}"

    def _translate_url(self):
        # Cloud engines (google/gemini/deepl) are served by a translate_server.py
        # this plugin spawns locally on the Deck (see _ensure_translate_server),
        # so users without their own Ollama LAN server can still translate.
        # Selecting "ollama" leaves this entirely untouched - same env var /
        # translate_url.txt / hardcoded-LAN-default resolution as always.
        if self._load_translation_settings().get("engine") != "ollama":
            return f"http://127.0.0.1:{self._translate_server_port()}/translate"

        env_url = os.environ.get("PLAYTRANSLATE_TRANSLATE_URL")
        if env_url:
            return self._normalize_translate_url(env_url)
        if self.translate_url_path.exists():
            configured = self._read_text_file(self.translate_url_path)
            if configured:
                return self._normalize_translate_url(configured)
        return "http://192.168.1.32:8787/translate"

    def _load_translation_settings(self):
        if not self.translation_settings_path.exists():
            return json.loads(json.dumps(_DEFAULT_TRANSLATION_SETTINGS))
        try:
            data = json.loads(self.translation_settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return json.loads(json.dumps(_DEFAULT_TRANSLATION_SETTINGS))
        settings = json.loads(json.dumps(_DEFAULT_TRANSLATION_SETTINGS))
        for key, value in (data or {}).items():
            if key in settings:
                settings[key] = value
        return settings

    def _save_translation_settings(self, settings):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.translation_settings_path.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def _mask_settings_for_log(self, settings):
        masked = json.loads(json.dumps(settings))
        for value in masked.values():
            if isinstance(value, dict):
                for key in value:
                    if key in _SENSITIVE_SETTING_KEYS and value[key]:
                        value[key] = "***"
        return masked

    async def get_translation_settings(self):
        return self._load_translation_settings()

    async def set_translation_settings(self, settings):
        old = self._load_translation_settings()
        new = dict(old)
        for key, value in (settings or {}).items():
            if key in new:
                new[key] = value
        self._save_translation_settings(new)
        decky.logger.info(f"PlayTranslate translation settings updated: {self._mask_settings_for_log(new)}")

        engine_changed = old.get("engine") != new.get("engine")
        engine_config_changed = old.get(new.get("engine")) != new.get(new.get("engine"))
        if engine_changed or engine_config_changed:
            await self._sync_translate_server(new)
        # capture_dynamic.py is a separate long-running process with its
        # translate_url/target_lang/source_lang frozen in argv at spawn time
        # (unlike the fixed-ROI path, which re-reads settings on every
        # translation attempt) - refresh the file it polls so a running
        # dynamic session picks up engine/language changes without a
        # restart. Written *after* _sync_translate_server above so
        # _translate_url() below reflects the just-started/just-stopped
        # local server, not a stale one.
        if self._is_dynamic_running():
            self._write_dynamic_translation_config(new)
        return new

    def _write_dynamic_translation_config(self, settings=None):
        settings = settings or self._load_translation_settings()
        config = {
            "translate_url": self._translate_url(),
            "target_lang": settings.get("target_lang", "Japanese"),
            "source_lang": settings.get("source_lang", "English"),
        }
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.dynamic_translation_config_path.write_text(
            json.dumps(config, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    def _load_ocr_settings(self):
        if not self.ocr_settings_path.exists():
            return json.loads(json.dumps(_DEFAULT_OCR_SETTINGS))
        try:
            data = json.loads(self.ocr_settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return json.loads(json.dumps(_DEFAULT_OCR_SETTINGS))
        settings = json.loads(json.dumps(_DEFAULT_OCR_SETTINGS))
        for key, value in (data or {}).items():
            if key in settings:
                settings[key] = value
        return settings

    def _save_ocr_settings(self, settings):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.ocr_settings_path.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    async def get_ocr_settings(self):
        return self._load_ocr_settings()

    async def set_ocr_settings(self, settings):
        old = self._load_ocr_settings()
        new = dict(old)
        for key, value in (settings or {}).items():
            if key in new:
                new[key] = value
        self._save_ocr_settings(new)
        decky.logger.info(f"PlayTranslate OCR settings updated: {new}")

        # ocr_worker.py reads its engine choice from argv at spawn time
        # (unlike translation settings, which capture_dynamic.py re-polls
        # from a file) - a running worker won't pick up the change on its
        # own, so restart it when the engine or its config actually moved.
        if old.get("engine") != new.get("engine") or old.get("chromescreenai") != new.get("chromescreenai"):
            await self._stop_ocr_worker()
            engine_dir, _capture_py, _config_json = self._find_engine()
            if engine_dir is not None:
                await self._ensure_ocr_worker(engine_dir)
        return new

    def _is_valid_binding(self, binding):
        if not isinstance(binding, dict):
            return False
        if binding.get("command") not in _VALID_COMMANDS:
            return False
        keys = binding.get("keys")
        if not isinstance(keys, list) or not (1 <= len(keys) <= 3):
            return False
        if len(set(keys)) != len(keys) or any(key not in _VALID_KEYS for key in keys):
            return False
        if not isinstance(binding.get("long_press"), bool):
            return False
        if not isinstance(binding.get("threshold_ms"), (int, float)):
            return False
        return True

    def _binding_signature(self, binding):
        return (frozenset(binding["keys"]), bool(binding["long_press"]))

    def _has_duplicate_binding_signature(self, bindings):
        seen = set()
        for binding in bindings:
            signature = self._binding_signature(binding)
            if signature in seen:
                return True
            seen.add(signature)
        return False

    def _load_keybinding_settings(self):
        if not self.keybinding_settings_path.exists():
            return json.loads(json.dumps(_DEFAULT_KEYBINDING_SETTINGS))
        try:
            data = json.loads(self.keybinding_settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return json.loads(json.dumps(_DEFAULT_KEYBINDING_SETTINGS))
        bindings = data.get("bindings") if isinstance(data, dict) else None
        if not isinstance(bindings, list) or not all(self._is_valid_binding(b) for b in bindings):
            return json.loads(json.dumps(_DEFAULT_KEYBINDING_SETTINGS))
        if self._has_duplicate_binding_signature(bindings):
            return json.loads(json.dumps(_DEFAULT_KEYBINDING_SETTINGS))
        return {"bindings": bindings}

    def _save_keybinding_settings(self, settings):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.keybinding_settings_path.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    async def get_keybinding_settings(self):
        return self._load_keybinding_settings()

    async def set_keybinding_settings(self, settings):
        bindings = (settings or {}).get("bindings", [])
        if not isinstance(bindings, list) or not all(self._is_valid_binding(b) for b in bindings):
            return {"ok": False, "error": "invalid binding shape", **self._load_keybinding_settings()}
        if self._has_duplicate_binding_signature(bindings):
            return {"ok": False, "error": "duplicate binding (same keys + same long-press setting) across commands"}
        new = {"bindings": bindings}
        self._save_keybinding_settings(new)
        decky.logger.info(f"PlayTranslate keybindings updated: {new}")
        return {"ok": True, **new}

    def _get_screenai_downloader(self):
        """Lazily loads screenai_downloader.py from the engine dir (same
        dir ocr_worker.py/capture.py live in) and caches one instance so
        in-flight download progress isn't lost on repeated calls. Not
        imported at module load time because the engine dir isn't known
        until _find_engine() resolves it, same reasoning as ocr_worker.py's
        own load_module() for ocr_tesseract.py/translate_stub.py."""
        if self.screenai_downloader is not None:
            return self.screenai_downloader
        engine_dir, _capture_py, _config_json = self._find_engine()
        if engine_dir is None:
            return None
        module_path = engine_dir / "screenai_downloader.py"
        if not module_path.exists():
            return None
        spec = importlib.util.spec_from_file_location("playtranslate_screenai_downloader", module_path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.screenai_downloader = module.ScreenAIDownloader(str(self.data_dir / "models"))
        return self.screenai_downloader

    async def get_screenai_status(self):
        downloader = self._get_screenai_downloader()
        if downloader is None:
            return {
                "installed": False, "size_bytes": 0, "approx_size_mb": 0,
                "downloading": False, "progress": 0.0,
                "error": "PlayTranslate engine directory not found",
            }
        return await asyncio.to_thread(downloader.get_status)

    async def download_screenai(self):
        downloader = self._get_screenai_downloader()
        if downloader is None:
            return {"ok": False, "error": "PlayTranslate engine directory not found"}
        return {"ok": downloader.start_download()}

    async def cancel_screenai_download(self):
        downloader = self._get_screenai_downloader()
        if downloader is not None:
            downloader.cancel_download()
        return {"ok": True}

    async def delete_screenai(self):
        downloader = self._get_screenai_downloader()
        if downloader is None:
            return {"ok": False, "error": "PlayTranslate engine directory not found"}
        return {"ok": await asyncio.to_thread(downloader.delete)}

    def _normalize_translate_url(self, url):
        url = url.strip()
        parsed = parse.urlparse(url)
        if parsed.scheme and parsed.netloc and parsed.path in ("", "/"):
            return parse.urlunparse(parsed._replace(path="/translate"))
        return url

    def _health_url(self):
        parsed = parse.urlparse(self._translate_url())
        return parse.urlunparse(parsed._replace(path="/health", params="", query="", fragment=""))

    def _cache_clear_url(self):
        parsed = parse.urlparse(self._translate_url())
        return parse.urlunparse(parsed._replace(path="/cache/clear", params="", query="", fragment=""))

    def _clear_translation_cache(self):
        """Blocking - always called via asyncio.to_thread (see
        refresh_dynamic_capture()), fire-and-forget, so a slow or
        unreachable translate_server.py never adds latency to the
        user-perceived Refresh action itself. Targets whichever server
        _translate_url() currently resolves to (local Deck-spawned server
        for cloud engines, or the configured Ollama host) - same server a
        translation would actually go to right now.
        """
        url = self._cache_clear_url()
        req = request.Request(url, data=b"", method="POST")
        try:
            with request.urlopen(req, timeout=5) as response:
                body = json.loads(response.read().decode("utf-8", errors="replace"))
                decky.logger.info(f"PlayTranslate: cleared translation cache ({body.get('cleared', '?')} entries) at {url}")
        except (urlerror.URLError, OSError, ValueError) as exc:
            decky.logger.warning(f"PlayTranslate: failed to clear translation cache at {url}: {exc}")

    def _clear_translation_outputs(self):
        for path in (self.translation_path, self.translation_json_path, self.translation_error_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                decky.logger.warning(f"Could not remove {path}: {exc}")

    async def status(self):
        engine_dir, capture_py, _config_json = self._find_engine()
        running = self._is_running()
        image_path = self.data_dir / "captures" / "last_settled.png"
        try:
            image_mtime_ns = image_path.stat().st_mtime_ns
        except OSError:
            image_mtime_ns = None
        worker_alive = self.translation_worker_task is not None and not self.translation_worker_task.done()
        return {
            "running": running,
            "pid": self.process.pid if running else None,
            "returncode": None if running or self.process is None else self.process.returncode,
            "engine_dir": str(engine_dir) if engine_dir else None,
            "capture_py": str(capture_py) if capture_py else None,
            "log_path": str(self.log_path),
            "log_tail": self._tail_log(),
            "translation_path": str(self.translation_path),
            "translation": self._read_text_file(self.translation_path),
            "translation_error": self._read_text_file(self.translation_error_path),
            "translate_url": self._translate_url(),
            "translation_worker": self.translation_worker_running and worker_alive,
            "translation_in_progress": self.translation_in_progress,
            "ocr_worker_running": self._is_ocr_worker_running(),
            "ocr_worker_pid": self.ocr_worker_process.pid if self._is_ocr_worker_running() else None,
            "translation_engine": self._load_translation_settings().get("engine"),
            "ocr_engine": self._load_ocr_settings().get("engine"),
            "translate_server_running": self._is_translate_server_running(),
            "translate_server_pid": self.translate_server_process.pid if self._is_translate_server_running() else None,
            "last_settled_mtime_ns": image_mtime_ns,
            "last_translated_image_mtime_ns": self.last_translated_image_mtime_ns,
            "translation_stale": (
                image_mtime_ns is not None
                and self.last_translated_image_mtime_ns is not None
                and image_mtime_ns != self.last_translated_image_mtime_ns
            ),
        }

    async def check_ai_server(self):
        health_url = self._health_url()

        def check():
            try:
                with request.urlopen(health_url, timeout=3) as response:
                    body = response.read(400).decode("utf-8", errors="replace")
                    return {
                        "ok": 200 <= response.status < 300,
                        "url": health_url,
                        "status": response.status,
                        "body": body,
                    }
            except urlerror.URLError as exc:
                return {"ok": False, "url": health_url, "error": str(exc)}
            except OSError as exc:
                return {"ok": False, "url": health_url, "error": str(exc)}

        return await asyncio.to_thread(check)

    def _distrobox_python_command(self, script, *args):
        return [
            "distrobox",
            "enter",
            os.environ.get("PLAYTRANSLATE_OCR_BOX", "playtranslate-ocr"),
            "--",
            "python3",
            str(script),
            *[str(arg) for arg in args],
        ]

    def _ocr_worker_port(self):
        return int(os.environ.get("PLAYTRANSLATE_OCR_WORKER_PORT", "8788"))

    def _ocr_worker_health_url(self):
        return f"http://127.0.0.1:{self._ocr_worker_port()}/health"

    def _ocr_worker_translate_url(self):
        return f"http://127.0.0.1:{self._ocr_worker_port()}/translate"

    def _ocr_worker_test_region_url(self):
        return f"http://127.0.0.1:{self._ocr_worker_port()}/test_region"

    def _valid_game_id(self, game_id):
        return bool(game_id) and bool(_GAME_ID_RE.match(game_id))

    def _active_game_path(self):
        return self.data_dir / "active_game.txt"

    def _get_active_game_id(self):
        game_id = self._read_text_file(self._active_game_path())
        return game_id if self._valid_game_id(game_id) else _DEFAULT_GAME_ID

    def _region_config_path(self, engine_dir, game_id):
        return engine_dir / f"ocr_regions.{game_id}.json"

    def _regions_json_path(self, engine_dir):
        return self._region_config_path(engine_dir, self._get_active_game_id())

    async def get_active_game(self):
        return {"active": self._get_active_game_id()}

    async def set_active_game(self, game_id):
        game_id = str(game_id or "").strip()
        if not self._valid_game_id(game_id):
            return {"ok": False, "error": "game id must be non-empty and use only letters, numbers, underscore"}
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._active_game_path().write_text(game_id, encoding="utf-8")
        return {"ok": True, "active": game_id}

    async def list_region_configs(self):
        engine_dir, _capture_py, _config_json = self._find_engine()
        games = []
        if engine_dir:
            for path in sorted(engine_dir.glob("ocr_regions.*.json")):
                game_id = path.name[len("ocr_regions.") : -len(".json")]
                if self._valid_game_id(game_id):
                    games.append(game_id)
        return {"games": games, "active": self._get_active_game_id()}

    async def get_region_config(self, game_id):
        game_id = str(game_id or "").strip()
        if not self._valid_game_id(game_id):
            return {"ok": False, "error": "invalid game id", "regions": []}
        engine_dir, _capture_py, _config_json = self._find_engine()
        if not engine_dir:
            return {"ok": False, "error": "engine directory was not found", "regions": []}
        path = self._region_config_path(engine_dir, game_id)
        if not path.exists():
            return {"ok": True, "regions": []}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": str(exc), "regions": []}
        regions = data.get("ocr_regions") if isinstance(data, dict) else data
        return {"ok": True, "regions": regions or []}

    def _reference_image_path(self, engine_dir, game_id):
        return engine_dir / f"ocr_regions.{game_id}.reference.png"

    async def save_region_config(self, game_id, regions, reference_image_base64=None):
        game_id = str(game_id or "").strip()
        if not self._valid_game_id(game_id):
            return {"ok": False, "error": "game id must be non-empty and use only letters, numbers, underscore"}
        if not isinstance(regions, list):
            return {"ok": False, "error": "regions must be a list"}
        engine_dir, _capture_py, _config_json = self._find_engine()
        if not engine_dir:
            return {"ok": False, "error": "engine directory was not found"}
        path = self._region_config_path(engine_dir, game_id)
        try:
            path.write_text(
                json.dumps({"ocr_regions": regions}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            return {"ok": False, "error": str(exc)}

        # Best-effort: a saved region set is much more useful later (re-editing
        # a game whose regions drifted, or without the game even running) if
        # it's paired with the screenshot it was drawn against. A failure here
        # shouldn't fail the actual save, which is why regions are already
        # written and returned as ok above regardless of what happens next.
        if reference_image_base64:
            try:
                self._reference_image_path(engine_dir, game_id).write_bytes(
                    base64.b64decode(reference_image_base64)
                )
            except (OSError, ValueError):
                pass

        return {"ok": True, "path": str(path)}

    async def get_reference_image(self, game_id):
        game_id = str(game_id or "").strip()
        if not self._valid_game_id(game_id):
            return {"ok": False, "error": "invalid game id"}
        engine_dir, _capture_py, _config_json = self._find_engine()
        if not engine_dir:
            return {"ok": False, "error": "engine directory was not found"}
        return self._read_image_base64(self._reference_image_path(engine_dir, game_id))

    def _read_image_base64(self, image_path):
        if not image_path.exists():
            return {"ok": False, "error": f"{image_path} was not found"}
        try:
            data = image_path.read_bytes()
            mtime_ns = image_path.stat().st_mtime_ns
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "base64": base64.b64encode(data).decode("ascii"),
            "mtime_ns": mtime_ns,
            "path": str(image_path),
        }

    async def get_last_settled_image(self):
        return self._read_image_base64(self.data_dir / "captures" / "last_settled.png")

    async def get_active_blocks(self):
        """Priority-ordered dynamic text blocks + translations, written by
        capture_dynamic.py (see its docstring). Returns an empty list when
        that engine isn't running yet - callers should fall back to the
        single-region `status().translation` in that case.
        """
        empty = {"blocks": [], "updated_at": None, "capture_width": None, "capture_height": None}
        try:
            raw = self.active_blocks_path.read_text(encoding="utf-8")
        except (OSError, FileNotFoundError):
            return empty
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return empty
        if not isinstance(data, dict) or not isinstance(data.get("blocks"), list):
            return empty
        return {
            "blocks": data["blocks"],
            "updated_at": data.get("updated_at"),
            "capture_width": data.get("capture_width"),
            "capture_height": data.get("capture_height"),
        }

    def _steam_screenshot_paths(self):
        userdata = Path.home() / ".local" / "share" / "Steam" / "userdata"
        patterns = (
            "*/760/remote/*/screenshots/*.jpg",
            "*/760/remote/*/screenshots/*.jpeg",
            "*/760/remote/*/screenshots/*.png",
        )
        paths = [path for pattern in patterns for path in userdata.glob(pattern)]
        paths.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)
        return paths

    def _load_capture_roi(self, engine_dir):
        # capture.py crops to this same roi when it saves last_settled.png
        # (crop_stage: "save" in config.json), so OCR region x/y/width/height
        # percentages are only meaningful relative to an image cropped the
        # same way. A screenshot cropped some other way (or not at all)
        # silently targets the wrong pixels - this is exactly what happened
        # when regions were calibrated against the raw, uncropped Steam
        # screenshot: same percentages, different image, wrong crop.
        try:
            config = json.loads((engine_dir / "config.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if config.get("crop_stage") != "save":
            return None
        return config.get("roi") or None

    def _crop_to_roi_via_worker(self, image_path, roi, output_path):
        payload = json.dumps({"image": str(image_path), "roi": roi, "output": str(output_path)}).encode("utf-8")
        req = request.Request(
            f"http://127.0.0.1:{self._ocr_worker_port()}/crop_to_roi",
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=15) as response:
                return 200 <= response.status < 300
        except (urlerror.URLError, OSError):
            return False

    async def get_latest_steam_screenshot(self):
        # SteamClient.Screenshots.GetLastScreenshotTaken() and
        # GameSessions.RegisterForScreenshotNotification() were both tried
        # from the plugin frontend first (see Calibration.tsx history) but
        # neither ever resolved/fired in live testing, despite screenshots
        # visibly accumulating in Steam's own userdata folder - that API
        # surface doesn't seem to work reliably from a Decky plugin's
        # sandboxed context. Polling the folder directly (same mtime-watch
        # pattern this file already uses for translation results) sidesteps
        # that entirely.
        try:
            candidates = await asyncio.to_thread(self._steam_screenshot_paths)
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        if not candidates:
            return {"ok": False, "error": "no Steam screenshots found"}

        image_path = candidates[0]
        engine_dir, _capture_py, _config_json = self._find_engine()
        roi = self._load_capture_roi(engine_dir) if engine_dir else None
        if roi and engine_dir:
            await self._ensure_ocr_worker(engine_dir)
            output_path = self.data_dir / "captures" / "calibration_source.png"
            cropped_ok = await asyncio.to_thread(self._crop_to_roi_via_worker, image_path, roi, output_path)
            if cropped_ok:
                return self._read_image_base64(output_path)
            # Worker down, bad roi, etc. - fall back to the uncropped shot
            # rather than blocking calibration entirely; the region
            # percentages just won't match runtime until this succeeds.
        return self._read_image_base64(image_path)

    async def test_ocr_region(self, region, image_path=None):
        if not isinstance(region, dict):
            return {"ok": False, "error": "region must be an object"}
        # image_path is whatever's currently loaded in the calibration UI
        # (see Calibration.tsx's imagePath state) - region x/y/width/height
        # are percentages of THAT image, so testing against a different one
        # (the old hardcoded default here) crops the wrong pixels entirely.
        image_path = Path(image_path) if image_path else self.data_dir / "captures" / "last_settled.png"
        if not image_path.exists():
            return {"ok": False, "error": f"{image_path} was not found"}

        engine_dir, _capture_py, _config_json = self._find_engine()
        if engine_dir:
            await self._ensure_ocr_worker(engine_dir)

        payload = json.dumps({"image": str(image_path), "region": region}).encode("utf-8")
        req = request.Request(
            self._ocr_worker_test_region_url(),
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )

        def call():
            try:
                with request.urlopen(req, timeout=15) as response:
                    return {"ok": True, **json.loads(response.read().decode("utf-8"))}
            except (urlerror.URLError, OSError, ValueError) as exc:
                return {"ok": False, "error": str(exc)}

        return await asyncio.to_thread(call)

    def _is_ocr_worker_running(self):
        return self.ocr_worker_process is not None and self.ocr_worker_process.poll() is None

    async def _ocr_worker_health(self):
        url = self._ocr_worker_health_url()

        def check():
            try:
                with request.urlopen(url, timeout=2) as response:
                    return 200 <= response.status < 300
            except (urlerror.URLError, OSError):
                return False

        return await asyncio.to_thread(check)

    async def check_ocr_worker(self):
        url = self._ocr_worker_health_url()

        def check():
            try:
                with request.urlopen(url, timeout=2) as response:
                    body = response.read(400).decode("utf-8", errors="replace")
                    return {"ok": 200 <= response.status < 300, "url": url, "status": response.status, "body": body}
            except (urlerror.URLError, OSError) as exc:
                return {"ok": False, "url": url, "error": str(exc)}

        return await asyncio.to_thread(check)

    async def _ensure_ocr_worker(self, engine_dir):
        if self._is_ocr_worker_running():
            return True
        if await self._ocr_worker_health():
            # A worker from a previous plugin load is already up and healthy; adopt it.
            return True

        worker_py = engine_dir / "ocr_worker.py"
        if not worker_py.exists():
            decky.logger.warning(f"{worker_py} was not found; staying on the per-call distrobox path")
            return False

        await self._stop_processes_by_script(worker_py, "ocr_worker")

        self.data_dir.mkdir(parents=True, exist_ok=True)
        worker_log_path = self.data_dir / "ocr-worker.log"
        worker_log = worker_log_path.open("a", encoding="utf-8")
        worker_log.write(f"\n--- PlayTranslate ocr_worker start {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        worker_log.flush()

        ocr_settings = self._load_ocr_settings()
        ocr_engine = ocr_settings.get("engine", "tesseract")
        worker_args = ["--port", str(self._ocr_worker_port()), "--ocr-engine", ocr_engine]
        if ocr_engine == "chromescreenai":
            downloader = self._get_screenai_downloader()
            if downloader is not None and downloader.is_installed():
                # distrobox mounts the host home directory at the same
                # absolute path, so this host-side path (under data_dir)
                # resolves the same way inside the container - same
                # assumption main.py already relies on for every image
                # path it hands to ocr_worker.py's HTTP API.
                worker_args += ["--screenai-model-dir", downloader.get_resources_dir()]
            else:
                decky.logger.warning(
                    "OCR engine set to chromescreenai but the model files aren't downloaded yet; "
                    "ocr_worker.py will fall back to tesseract for discovery until they are."
                )
            min_confidence = ocr_settings.get("chromescreenai", {}).get("min_confidence", 0.5)
            worker_args += ["--screenai-min-confidence", str(min_confidence)]
        command = self._distrobox_python_command(worker_py, *worker_args)
        env = self._subprocess_env()
        try:
            self.ocr_worker_process = subprocess.Popen(
                command,
                cwd=str(engine_dir),
                stdout=worker_log,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
                start_new_session=True,
            )
        except OSError as exc:
            worker_log.write(f"Failed to start ocr_worker: {exc}\n")
            worker_log.flush()
            worker_log.close()
            return False

        self.ocr_worker_log_file = worker_log

        # distrobox enter + tesseract/tesserocr init can take a few seconds on first start.
        for _ in range(50):
            await asyncio.sleep(0.1)
            if await self._ocr_worker_health():
                decky.logger.info(f"PlayTranslate OCR worker healthy pid={self.ocr_worker_process.pid}")
                return True
            if self.ocr_worker_process.poll() is not None:
                break

        decky.logger.warning("PlayTranslate OCR worker did not become healthy; falling back to the per-call distrobox path")
        return False

    async def _stop_ocr_worker(self):
        engine_dir, _capture_py, _config_json = self._find_engine()
        worker_py = (engine_dir / "ocr_worker.py") if engine_dir else None

        if self.ocr_worker_process is not None:
            pid = self.ocr_worker_process.pid
            try:
                os.killpg(pid, signal.SIGTERM)
                for _ in range(20):
                    if self.ocr_worker_process.poll() is not None:
                        break
                    await asyncio.sleep(0.1)
                if self.ocr_worker_process.poll() is None:
                    os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.ocr_worker_process = None

        if self.ocr_worker_log_file:
            self.ocr_worker_log_file.write(f"--- PlayTranslate ocr_worker stop {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
            self.ocr_worker_log_file.close()
            self.ocr_worker_log_file = None

        if worker_py:
            await self._stop_processes_by_script(worker_py, "ocr_worker")

    def _translate_server_port(self):
        return int(os.environ.get("PLAYTRANSLATE_TRANSLATE_SERVER_PORT", "8790"))

    def _translate_server_health_url(self):
        return f"http://127.0.0.1:{self._translate_server_port()}/health"

    def _is_translate_server_running(self):
        return self.translate_server_process is not None and self.translate_server_process.poll() is None

    async def _translate_server_health(self):
        url = self._translate_server_health_url()

        def check():
            try:
                with request.urlopen(url, timeout=2) as response:
                    return 200 <= response.status < 300
            except (urlerror.URLError, OSError):
                return False

        return await asyncio.to_thread(check)

    def _translate_server_command(self, script, settings):
        engine = settings.get("engine", "ollama")
        command = [
            "python3",
            str(script),
            "--host",
            "127.0.0.1",
            "--port",
            str(self._translate_server_port()),
            "--backend",
            engine,
        ]
        if engine == "gemini":
            cfg = settings.get("gemini") or {}
            command += ["--model", cfg.get("model") or "gemini-3.6-flash"]
        # No --api-key here on purpose: CLI args are visible to any other
        # process on the box via `ps`/proc; the key is passed through the
        # subprocess's own env instead (see _ensure_translate_server).
        return command

    def _translate_server_api_key(self, settings):
        engine = settings.get("engine", "ollama")
        if engine in ("gemini", "deepl", "google_cloud"):
            return (settings.get(engine) or {}).get("api_key", "")
        return ""

    async def _ensure_translate_server(self, settings):
        # translate_server.py is pure stdlib (no OCR/PIL deps), so unlike
        # ocr_worker.py it runs directly - no distrobox hop needed.
        if self._is_translate_server_running() and await self._translate_server_health():
            return True

        engine_dir, _capture_py, _config_json = self._find_engine()
        if not engine_dir:
            decky.logger.warning("PlayTranslate: engine dir not found; cannot start local translate_server.py")
            return False
        script = engine_dir / "translate_server.py"
        if not script.exists():
            decky.logger.warning(f"{script} was not found; cannot start local translate_server.py")
            return False

        await self._stop_processes_by_script(script, "translate_server")

        self.data_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.data_dir / "translate-server.log"
        log_file = log_path.open("a", encoding="utf-8")
        log_file.write(
            f"\n--- PlayTranslate translate_server start {time.strftime('%Y-%m-%d %H:%M:%S')} "
            f"engine={settings.get('engine')} ---\n"
        )
        log_file.flush()

        command = self._translate_server_command(script, settings)
        env = self._subprocess_env()
        api_key = self._translate_server_api_key(settings)
        if api_key:
            env["PLAYTRANSLATE_ENGINE_API_KEY"] = api_key
        # Without this, translate_server.py's own print()s (its startup
        # banner, and every request's access-log line from log_message())
        # sit in a fully-buffered stdout and are lost outright whenever this
        # process gets SIGTERM'd/SIGKILL'd (the normal case - see
        # _stop_translate_server) instead of exiting cleanly. Confirmed live:
        # translate-server.log only ever contained the lines *this* class
        # writes directly with an explicit flush() below, never anything
        # translate_server.py itself printed.
        env["PYTHONUNBUFFERED"] = "1"
        try:
            self.translate_server_process = subprocess.Popen(
                command,
                cwd=str(engine_dir),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
                start_new_session=True,
            )
        except OSError as exc:
            log_file.write(f"Failed to start translate_server: {exc}\n")
            log_file.flush()
            log_file.close()
            return False

        self.translate_server_log_file = log_file

        for _ in range(30):
            await asyncio.sleep(0.1)
            if await self._translate_server_health():
                decky.logger.info(f"PlayTranslate local translate_server healthy pid={self.translate_server_process.pid}")
                return True
            if self.translate_server_process.poll() is not None:
                break

        decky.logger.warning("PlayTranslate local translate_server did not become healthy")
        return False

    async def _ensure_translate_server_if_needed(self):
        settings = self._load_translation_settings()
        if settings.get("engine") == "ollama":
            return
        await self._ensure_translate_server(settings)

    async def _sync_translate_server(self, settings):
        await self._stop_translate_server()
        if settings.get("engine") != "ollama":
            await self._ensure_translate_server(settings)

    async def _stop_translate_server(self):
        engine_dir, _capture_py, _config_json = self._find_engine()
        script = (engine_dir / "translate_server.py") if engine_dir else None

        if self.translate_server_process is not None:
            pid = self.translate_server_process.pid
            try:
                os.killpg(pid, signal.SIGTERM)
                for _ in range(20):
                    if self.translate_server_process.poll() is not None:
                        break
                    await asyncio.sleep(0.1)
                if self.translate_server_process.poll() is None:
                    os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.translate_server_process = None

        if self.translate_server_log_file:
            self.translate_server_log_file.write(
                f"--- PlayTranslate translate_server stop {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n"
            )
            self.translate_server_log_file.close()
            self.translate_server_log_file = None

        if script:
            await self._stop_processes_by_script(script, "translate_server")

    def _write_translation_outputs(self, translation, full_result):
        self.translation_path.write_text(translation.strip() + "\n", encoding="utf-8")
        self.translation_json_path.write_text(
            json.dumps(full_result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def _run_translation_via_worker(self, image_path, engine_dir):
        settings = self._load_translation_settings()
        payload = json.dumps(
            {
                "image": str(image_path),
                "regions_json": str(self._regions_json_path(engine_dir)),
                "http_url": self._translate_url(),
                "target_lang": settings.get("target_lang", "Japanese"),
                "source_lang": settings.get("source_lang", "English"),
            }
        ).encode("utf-8")
        req = request.Request(
            self._ocr_worker_translate_url(),
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        # 140s: must exceed translate_stub.py's own 130s timeout on the
        # ocr_worker -> translate_server hop, which itself must exceed
        # OllamaProvider's 120s timeout - otherwise this outer timeout fires
        # first and kills a slow-but-healthy Ollama call before it can
        # return a clean error (or a result).
        try:
            with request.urlopen(req, timeout=140) as response:
                return json.loads(response.read().decode("utf-8"))
        except urlerror.HTTPError as exc:
            # ocr_worker.py's _handle_translate forwards translate_server.py's
            # error/error_type as a JSON body even on a non-2xx status -
            # surface that instead of losing it to a generic HTTPError.
            try:
                return json.loads(exc.read().decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise exc

    def _run_translation(self, engine_dir, image_path):
        # Always attempt the worker rather than gating on self.ocr_worker_process:
        # that handle is only set for workers this process itself spawned, so a
        # worker adopted from a previous plugin load (see _ensure_ocr_worker)
        # would otherwise be skipped forever even though it's healthy. A failed
        # call (worker absent, stale, or unhealthy) falls back below regardless.
        try:
            result = self._run_translation_via_worker(image_path, engine_dir)
            if result.get("error"):
                # A clean, structured failure (bad API key, rate limit, ...) -
                # falling back to the distrobox path wouldn't fix it and
                # would just waste ~180s retrying the same broken config, so
                # report it directly instead of falling through below.
                error_type = result.get("error_type")
                message = result["error"]
                return False, "", f"[{error_type}] {message}" if error_type else message
            translation = str(result.get("translation") or "").strip()
            if not translation:
                return False, "", "ocr worker returned an empty translation"
            self._write_translation_outputs(translation, result)
            timing = result.get("timing")
            if timing:
                self._append_translation_worker_log(f"worker timing: {json.dumps(timing, ensure_ascii=False)}")
            return True, translation, ""
        except (urlerror.URLError, OSError, ValueError, KeyError) as exc:
            self._append_translation_worker_log(f"ocr worker call failed, falling back to distrobox path: {exc}")

        return self._run_translation_runner(engine_dir)

    def _find_steamdeck_hidraw(self):
        if self.hidraw_path and self.hidraw_path.exists():
            return self.hidraw_path

        candidates = []
        for i in range(10):
            path = Path(f"/dev/hidraw{i}")
            uevent_path = Path(f"/sys/class/hidraw/hidraw{i}/device/uevent")
            if not path.exists():
                continue
            try:
                content = uevent_path.read_text(encoding="utf-8", errors="replace").upper()
            except OSError:
                continue
            if "28DE" in content and "1205" in content:
                candidates.append((i, path))

        for i, path in candidates:
            try:
                link_target = os.readlink(f"/sys/class/hidraw/hidraw{i}")
            except OSError:
                continue
            if ":1.2/" in link_target:
                self.hidraw_path = path
                return path
        self.hidraw_path = candidates[-1][1] if candidates else None
        return self.hidraw_path

    # Every entry: button name, byte offset into the 64-byte report, and the
    # mask to test against that single byte. None of these came from a
    # public spec - all measured live on hardware by capturing raw hidraw
    # reports on /dev/hidraw2 (of the 3 candidate interfaces at VID:PID
    # 28DE:1205, the other two - :1.0 and :1.1 - never produced a single
    # report in any measurement session, idle or active) while holding each
    # button and diffing against an idle baseline. L2/L4/L5/R4/R5 were
    # measured first (see the original comment this table replaced); the
    # remaining 13 keys (A/B/X/Y/L1/L3/R1/R2/R3/select/start + both
    # trackpad taps) were measured 2026-08-19 the same way, one key at a
    # time to avoid overlapping presses contaminating the diff (batched
    # multi-key passes produced ambiguous/colliding results for the
    # closely-timed L3/R3/select/start group - isolate each key if
    # re-measuring). R2's bit correlates with its analog trigger axis
    # moving (a separate field elsewhere in the report), same as L2.
    # Trackpad taps are level signals (high the entire time a finger is on
    # the pad) living in this same buttons_l/buttons_h bitfield, not a
    # separate coordinate-only field - no distinct interface or report
    # needed for them beyond what L2/L4/L5/R4/R5 already used.
    _BUTTON_FIELDS = [
        {"name": "A", "offset": 8, "mask": 0x80},
        {"name": "B", "offset": 8, "mask": 0x20},
        {"name": "X", "offset": 8, "mask": 0x40},
        {"name": "Y", "offset": 8, "mask": 0x10},
        {"name": "L1", "offset": 8, "mask": 0x08},
        {"name": "R1", "offset": 8, "mask": 0x04},
        # R2's digital soft-pull click, not its analog trigger axis.
        {"name": "R2", "offset": 8, "mask": 0x01},
        # L2's digital soft-pull click, not its analog trigger axis (that's
        # a separate 16-bit value elsewhere in the report).
        {"name": "L2", "offset": 8, "mask": 0x02},
        {"name": "L5", "offset": 9, "mask": 0x80},
        {"name": "select", "offset": 9, "mask": 0x10},
        {"name": "start", "offset": 9, "mask": 0x40},
        {"name": "R5", "offset": 10, "mask": 0x01},
        {"name": "trackpad_left_tap", "offset": 10, "mask": 0x08},
        {"name": "trackpad_right_tap", "offset": 10, "mask": 0x10},
        {"name": "L3", "offset": 10, "mask": 0x40},
        {"name": "R3", "offset": 11, "mask": 0x04},
        {"name": "L4", "offset": 13, "mask": 0x02},
        {"name": "R4", "offset": 13, "mask": 0x04},
    ]

    def _read_hidraw_buttons_once(self, timeout=0.2):
        path = self._find_steamdeck_hidraw()
        if not path:
            return {"success": False, "buttons": [], "error": "Steam Deck hidraw device was not found"}

        fd = None
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            deadline = time.monotonic() + timeout
            last_buttons = []
            while time.monotonic() < deadline:
                readable, _, _ = select.select([fd], [], [], 0.01)
                if not readable:
                    continue
                data = os.read(fd, 64)
                if len(data) < 16:
                    continue
                buttons = [
                    field["name"]
                    for field in self._BUTTON_FIELDS
                    if data[field["offset"]] & field["mask"]
                ]
                last_buttons = buttons
                if buttons:
                    break
            return {"success": True, "device": str(path), "buttons": last_buttons}
        except OSError as exc:
            self.hidraw_path = None
            return {"success": False, "buttons": [], "device": str(path), "error": str(exc)}
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    async def test_hidraw_button_state(self):
        return await asyncio.to_thread(self._read_hidraw_buttons_once)

    async def translate_latest(self):
        if self.translation_in_progress:
            return await self.status()

        engine_dir, _capture_py, _config_json = self._find_engine()
        if not engine_dir:
            return {"error": "engine directory was not found", **(await self.status())}

        image_path = self.data_dir / "captures" / "last_settled.png"
        if not image_path.exists():
            return {"error": f"{image_path} was not found", **(await self.status())}

        self.data_dir.mkdir(parents=True, exist_ok=True)

        # Belt-and-suspenders: start_capture()/start_dynamic_capture() and
        # set_translation_settings() already try to keep the local
        # translate_server up, but only as a fire-and-forget task - if it
        # died since (crash, OOM, ...) nothing else would notice before
        # actually attempting a translation. Awaited (not fire-and-forget)
        # here so a dead server is really back up before _run_translation
        # tries to use it; near-instant when it's already healthy.
        await self._ensure_translate_server_if_needed()

        self.translation_in_progress = True
        try:
            ok, stdout, stderr = await asyncio.to_thread(self._run_translation, engine_dir, image_path)
        finally:
            self.translation_in_progress = False

        if not ok:
            error = (stderr or stdout or "translation failed").strip()
            return {"error": error, **(await self.status())}

        try:
            self.last_translated_image_mtime_ns = image_path.stat().st_mtime_ns
        except OSError:
            self.last_translated_image_mtime_ns = None
        try:
            self.translation_error_path.unlink()
        except FileNotFoundError:
            pass
        return await self.status()

    def _append_translation_worker_log(self, message):
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            with (self.data_dir / "translation-worker.log").open("a", encoding="utf-8") as log:
                log.write(f"{message}\n")
        except OSError as exc:
            decky.logger.warning(f"Could not write translation worker log: {exc}")

    def _run_translation_runner(self, engine_dir):
        runner = self.data_dir / "run_translate.sh"
        if not runner.exists():
            return False, "", f"{runner} was not found"

        settings = self._load_translation_settings()
        env = self._subprocess_env()
        env.setdefault("PLAYTRANSLATE_DATA_DIR", str(self.data_dir))
        env.setdefault("PLAYTRANSLATE_ENGINE_DIR", str(engine_dir))
        env.setdefault("PLAYTRANSLATE_TRANSLATE_URL", self._translate_url())
        env.setdefault("PLAYTRANSLATE_REGIONS_JSON", str(self._regions_json_path(engine_dir)))
        env.setdefault("PLAYTRANSLATE_TARGET_LANG", settings.get("target_lang", "Japanese"))
        env.setdefault("PLAYTRANSLATE_SOURCE_LANG", settings.get("source_lang", "English"))

        try:
            result = subprocess.run(
                [str(runner)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=180,
                env=env,
            )
            return True, result.stdout.strip(), result.stderr.strip()
        except subprocess.CalledProcessError as exc:
            return False, (exc.stdout or "").strip(), (exc.stderr or str(exc)).strip()
        except subprocess.TimeoutExpired:
            return False, "", "translation timed out"
        except OSError as exc:
            return False, "", str(exc)

    async def _translation_worker_loop(self, engine_dir):
        image_path = self.data_dir / "captures" / "last_settled.png"
        self._append_translation_worker_log(f"--- worker start {time.strftime('%Y-%m-%d %H:%M:%S')} ---")
        while self.translation_worker_running:
            try:
                if image_path.exists():
                    mtime_ns = image_path.stat().st_mtime_ns
                    if mtime_ns != self.last_translated_image_mtime_ns and not self.translation_in_progress:
                        self.translation_in_progress = True
                        self._append_translation_worker_log(
                            f"translate image mtime_ns={mtime_ns} {time.strftime('%Y-%m-%d %H:%M:%S')}"
                        )
                        # See translate_latest() for why this is awaited here
                        # rather than relying only on the fire-and-forget
                        # ensure calls at capture start / settings change.
                        await self._ensure_translate_server_if_needed()
                        ok, stdout, stderr = await asyncio.to_thread(self._run_translation, engine_dir, image_path)
                        try:
                            current_mtime_ns = image_path.stat().st_mtime_ns
                        except OSError:
                            current_mtime_ns = None
                        stale_result = current_mtime_ns is not None and current_mtime_ns != mtime_ns
                        if ok:
                            if stale_result:
                                self._clear_translation_outputs()
                                self._append_translation_worker_log(
                                    f"stale result discarded translated_mtime_ns={mtime_ns} current_mtime_ns={current_mtime_ns}"
                                )
                            else:
                                self.last_translated_image_mtime_ns = mtime_ns
                                try:
                                    self.translation_error_path.unlink()
                                except FileNotFoundError:
                                    pass
                                self._append_translation_worker_log(f"ok: {stdout[:500]}")
                        else:
                            if stale_result:
                                self._append_translation_worker_log(
                                    f"stale error discarded translated_mtime_ns={mtime_ns} current_mtime_ns={current_mtime_ns}"
                                )
                            else:
                                message = (stderr or stdout or "translation failed").strip()
                                self.translation_error_path.write_text(message + "\n", encoding="utf-8")
                                self._append_translation_worker_log(f"error: {message[:1000]}")
                        self.translation_in_progress = False
                await asyncio.sleep(0.2)
            except Exception as exc:
                self.translation_in_progress = False
                self._append_translation_worker_log(f"unexpected: {type(exc).__name__}: {exc}")
                await asyncio.sleep(1.0)
        self._append_translation_worker_log(f"--- worker stop {time.strftime('%Y-%m-%d %H:%M:%S')} ---")

    def _start_translation_worker(self, engine_dir):
        if self.translation_worker_task and not self.translation_worker_task.done():
            self.translation_worker_running = True
            return
        self.translation_worker_running = True
        self.translation_worker_task = asyncio.create_task(self._translation_worker_loop(engine_dir))

    async def _stop_translation_worker(self):
        self.translation_worker_running = False
        if self.translation_worker_task:
            try:
                await asyncio.wait_for(self.translation_worker_task, timeout=2.0)
            except asyncio.TimeoutError:
                self.translation_worker_task.cancel()
            self.translation_worker_task = None

    async def start_capture(self):
        engine_dir, capture_py, config_json = self._find_engine()
        if self._is_running():
            if engine_dir:
                self._start_translation_worker(engine_dir)
                asyncio.create_task(self._ensure_ocr_worker(engine_dir))
                asyncio.create_task(self._ensure_translate_server_if_needed())
            return await self.status()

        if not capture_py or not config_json:
            return {
                "running": False,
                "error": "capture.py/config.json was not found. Set PLAYTRANSLATE_ENGINE_DIR or place files in bin/.",
                "log_path": str(self.log_path),
            }

        # The dynamic (wide-area) engine and this one open independent
        # PipeWire consumers on the same gamescope source, which is a
        # confirmed trigger for the capture freeze cee3705/e2f3e2e recover
        # from automatically but don't prevent - keep them mutually
        # exclusive rather than let that happen on every normal start.
        await self._kill_tracked_dynamic_process()

        await self._stop_existing_capture_processes(capture_py)
        self.last_translated_image_mtime_ns = None
        self._clear_translation_outputs()

        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_file = self.log_path.open("a", encoding="utf-8")
        self.log_file.write(f"\n--- PlayTranslate start {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        env = self._capture_env()
        self.log_file.write(f"PATH={env.get('PATH', '')}\n")
        self.log_file.write(f"XDG_RUNTIME_DIR={env.get('XDG_RUNTIME_DIR', '')}\n")
        self.log_file.write(f"DBUS_SESSION_BUS_ADDRESS={env.get('DBUS_SESSION_BUS_ADDRESS', '')}\n")
        self.log_file.flush()
        save_dir = self.data_dir / "captures"
        save_dir.mkdir(parents=True, exist_ok=True)

        command = [
            "python3",
            str(capture_py),
            "--config",
            str(config_json),
            "--diff",
            "--save-settled",
            "--save-dir",
            str(save_dir),
            "--save-prefix",
            "playtranslate",
            "--save-latest-name",
            "last_settled.png",
        ]

        try:
            self.process = subprocess.Popen(
                command,
                cwd=str(engine_dir),
                stdout=self.log_file,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
                start_new_session=True,
            )
        except OSError as exc:
            self.process = None
            self.log_file.write(f"Failed to start: {exc}\n")
            self.log_file.flush()
            return {"running": False, "error": str(exc), "log_path": str(self.log_path)}

        decky.logger.info(f"Started PlayTranslate capture pid={self.process.pid}")
        self._start_translation_worker(engine_dir)
        asyncio.create_task(self._ensure_ocr_worker(engine_dir))
        asyncio.create_task(self._ensure_translate_server_if_needed())
        return await self.status()

    async def _kill_tracked_capture_process(self):
        if not self._is_running():
            return
        pid = self.process.pid
        try:
            os.killpg(pid, signal.SIGTERM)
            for _ in range(20):
                if self.process.poll() is not None:
                    break
                await asyncio.sleep(0.1)
            if self.process.poll() is None:
                os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    async def stop_capture(self):
        # These three are independent (different subsystems) and each has its
        # own ~2s worst-case SIGTERM-wait-then-SIGKILL timeout, so running
        # them sequentially could take ~6s+ - long enough that Decky's
        # plugin_loader (which force-SIGKILLs the whole plugin backend if
        # _unload/stop_capture hasn't returned within 5s) could kill this
        # process mid-cleanup, orphaning whichever subprocess hadn't been
        # reached yet. Running them concurrently keeps the worst case to
        # roughly the slowest single one (~2s) instead of their sum.
        await asyncio.gather(
            self._stop_translation_worker(),
            self._stop_ocr_worker(),
            self._kill_tracked_capture_process(),
        )

        if self.log_file:
            self.log_file.write(f"--- PlayTranslate stop {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
            self.log_file.flush()
            self.log_file.close()
            self.log_file = None

        decky.logger.info("Stopped PlayTranslate capture")
        _engine_dir, capture_py, _config_json = self._find_engine()
        if capture_py:
            await self._stop_existing_capture_processes(capture_py)
        return await self.status()

    # ── Dynamic (wide-area multi-block) engine — see PHASE_A_HANDOFF.md ────
    #
    # capture_dynamic.py, not yet the default: still validated by manually
    # starting/stopping it here while iterating on discovery/grouping/
    # tracking quality across different games. Mutually exclusive with the
    # legacy capture.py (see the note in start_capture()) rather than
    # something a user would run alongside it.

    def _write_dynamic_config(self):
        """capture_dynamic.py needs a wide/full-frame capture region, unlike
        config.json's calibrated subtitle ROI - write a fixed no-crop config
        for it rather than exposing this as something to hand-tune, since
        it isn't a per-game setting the way config.json's roi is.
        """
        self.dynamic_config_path.parent.mkdir(parents=True, exist_ok=True)
        self.dynamic_config_path.write_text(
            json.dumps({"auto_detect": True, "target_object": None, "crop": {"left": 0, "right": 0, "top": 0, "bottom": 0}}),
            encoding="utf-8",
        )

    async def _kill_tracked_dynamic_process(self):
        if not self._is_dynamic_running():
            return
        pid = self.dynamic_process.pid
        try:
            os.killpg(pid, signal.SIGTERM)
            for _ in range(20):
                if self.dynamic_process.poll() is not None:
                    break
                await asyncio.sleep(0.1)
            if self.dynamic_process.poll() is None:
                os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    async def start_dynamic_capture(self):
        engine_dir, script = self._find_dynamic_capture_script()
        if self._is_dynamic_running():
            if engine_dir:
                asyncio.create_task(self._ensure_ocr_worker(engine_dir))
                asyncio.create_task(self._ensure_translate_server_if_needed())
            return await self.dynamic_status()

        if not script:
            return {
                "running": False,
                "error": "capture_dynamic.py was not found in the engine dir.",
                "log_path": str(self.dynamic_log_path),
            }

        # See the note in start_capture(): the two engines opening
        # independent PipeWire consumers on the same source is a confirmed
        # freeze trigger, so keep them mutually exclusive.
        await self._kill_tracked_capture_process()
        await self._stop_existing_capture_processes(engine_dir / "capture.py")

        self._write_dynamic_config()
        try:
            self.active_blocks_path.unlink()
        except FileNotFoundError:
            pass
        try:
            self.dynamic_pause_flag_path.unlink()
        except FileNotFoundError:
            pass
        try:
            self.dynamic_qam_open_flag_path.unlink()
        except FileNotFoundError:
            pass
        try:
            self.dynamic_status_toast_flag_path.unlink()
        except FileNotFoundError:
            pass
        try:
            self.tap_request_path.unlink()
        except FileNotFoundError:
            pass
        try:
            self.tap_result_path.unlink()
        except FileNotFoundError:
            pass

        self.dynamic_log_path.parent.mkdir(parents=True, exist_ok=True)
        self.dynamic_log_file = self.dynamic_log_path.open("a", encoding="utf-8")
        self.dynamic_log_file.write(f"\n--- PlayTranslate dynamic start {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        self.dynamic_log_file.flush()
        env = self._capture_env()
        # Set PT_DEBUG_DIFF=1 here temporarily when diagnosing tracker
        # stability live - region_tracker.py logs each block's raw diff
        # ratio every tick when it's set. This is how the positioned-
        # overlay capture feedback loop (see PHASE_A_HANDOFF.md) was found:
        # diff values stayed 0.0000 in an isolated test with nothing
        # rendering an overlay, then spiked in lockstep across unrelated
        # blocks the moment the real overlay was actually on screen.

        translation_settings = self._load_translation_settings()
        self._write_dynamic_translation_config(translation_settings)
        command = [
            "python3",
            str(script),
            "--config",
            str(self.dynamic_config_path),
            "--ocr-worker-url",
            f"http://127.0.0.1:{self._ocr_worker_port()}",
            "--translate-url",
            self._translate_url(),
            "--target-lang",
            translation_settings.get("target_lang", "Japanese"),
            "--source-lang",
            translation_settings.get("source_lang", "English"),
            "--translation-config",
            str(self.dynamic_translation_config_path),
            "--output",
            str(self.active_blocks_path),
            "--pause-flag",
            str(self.dynamic_pause_flag_path),
            "--qam-open-flag",
            str(self.dynamic_qam_open_flag_path),
            "--status-toast-flag",
            str(self.dynamic_status_toast_flag_path),
            "--tap-request",
            str(self.tap_request_path),
            "--tap-result",
            str(self.tap_result_path),
            "--discovery-min-interval",
            "3",
            "--report-interval",
            "8",
        ]

        try:
            self.dynamic_process = subprocess.Popen(
                command,
                cwd=str(engine_dir),
                stdout=self.dynamic_log_file,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
                start_new_session=True,
            )
        except OSError as exc:
            self.dynamic_process = None
            self.dynamic_log_file.write(f"Failed to start: {exc}\n")
            self.dynamic_log_file.flush()
            return {"running": False, "error": str(exc), "log_path": str(self.dynamic_log_path)}

        decky.logger.info(f"Started PlayTranslate dynamic capture pid={self.dynamic_process.pid}")
        asyncio.create_task(self._ensure_ocr_worker(engine_dir))
        asyncio.create_task(self._ensure_translate_server_if_needed())
        return await self.dynamic_status()

    async def stop_dynamic_capture(self):
        await self._kill_tracked_dynamic_process()
        try:
            self.active_blocks_path.unlink()
        except FileNotFoundError:
            pass
        try:
            self.tap_request_path.unlink()
        except FileNotFoundError:
            pass
        try:
            self.tap_result_path.unlink()
        except FileNotFoundError:
            pass
        if self.dynamic_log_file:
            self.dynamic_log_file.write(f"--- PlayTranslate dynamic stop {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
            self.dynamic_log_file.flush()
            self.dynamic_log_file.close()
            self.dynamic_log_file = None

        decky.logger.info("Stopped PlayTranslate dynamic capture")
        _engine_dir, script = self._find_dynamic_capture_script()
        if script:
            await self._stop_processes_by_script(script, "dynamic capture")
        return await self.dynamic_status()

    async def refresh_dynamic_capture(self):
        """User-facing escape hatch: force a clean restart of the dynamic
        engine, clearing whatever's currently displayed and re-running
        full-frame discovery from scratch. For display states the tracker
        won't self-correct - e.g. a multi-line dialogue box discovered as
        several independently (mis)translated line fragments, which a
        fresh discovery pass will very likely reproduce identically since
        it's a layout issue, not a stale-state issue - the user's own
        judgement that "this looks wrong" is a better trigger than any
        heuristic here.

        Also clears translate_server.py's translation cache (fire-and-
        forget, see _clear_translation_cache()) - the QAM's own tooltip for
        this button already promises a from-scratch restart, but until
        2026-08-19 that never actually included the cache, so a single bad
        OCR-noise translation (e.g. PlayTranslate's own UI text getting
        misread once) could keep resurfacing verbatim with no way for the
        user to clear it short of restarting translate_server.py by hand.
        See PHASE_A_HANDOFF.md.

        Serialized by dynamic_refresh_lock so repeated presses queue behind
        each other's full stop/start cycle instead of interleaving - two
        overlapping stop+start sequences could otherwise race on
        self.dynamic_process (e.g. a second start() overwriting the tracked
        PID while the first stop() is still polling the old one).

        Also pre-arms dynamic_status_toast_flag_path itself, synchronously,
        right after the new process spawns - found live 2026-08-19 that
        relying solely on index.tsx's showToast()->set_dynamic_status_
        toast_visible() round trip (see that method) was too slow: with
        --startup-delay now 0, the new process's first discovery can finish
        before that RPC round trip (itself following the *entire* refresh
        RPC this method just ran) ever lands, so "PlayTranslate: refreshed"
        was still getting OCR'd and translated most times. Setting it here
        instead closes the gap to a few lines of synchronous Python.
        Self-clearing (not left for the frontend's showToast() to clear
        alone) because this method is also called from the QAM's "Refresh
        Dynamic Capture" button, which never calls showToast() at all - if
        only the frontend cleared it, a QAM-triggered refresh would leave
        capture permanently suppressed.
        """
        async with self.dynamic_refresh_lock:
            try:
                self.active_blocks_path.unlink()
            except FileNotFoundError:
                pass
            await self.stop_dynamic_capture()
            result = await self.start_dynamic_capture()
            asyncio.create_task(asyncio.to_thread(self._clear_translation_cache))
            self.dynamic_status_toast_flag_path.touch()
            # Cancel any still-pending clear from an earlier refresh before
            # scheduling this one's - without this, rapid repeated Refresh
            # presses (mashing the QAM button, or L4) could let an older
            # call's timer clear the flag while a newer refresh's process
            # is still supposed to be protected by it.
            if self._status_toast_clear_task is not None:
                self._status_toast_clear_task.cancel()
            self._status_toast_clear_task = asyncio.create_task(
                self._clear_status_toast_flag_after(_STATUS_TOAST_SUPPRESS_S)
            )
            return result

    async def _clear_status_toast_flag_after(self, delay_s):
        await asyncio.sleep(delay_s)
        try:
            self.dynamic_status_toast_flag_path.unlink()
        except FileNotFoundError:
            pass

    def _tail_dynamic_log(self, lines=40):
        if not self.dynamic_log_path.exists():
            return ""
        try:
            text = self.dynamic_log_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"Could not read log: {exc}"
        return "\n".join(text.splitlines()[-lines:])

    async def dynamic_status(self):
        running = self._is_dynamic_running()
        active_blocks = await self.get_active_blocks()
        return {
            "running": running,
            "paused": self.dynamic_pause_flag_path.exists(),
            "qam_open": self.dynamic_qam_open_flag_path.exists(),
            "pid": self.dynamic_process.pid if running else None,
            "returncode": None if running or self.dynamic_process is None else self.dynamic_process.returncode,
            "log_path": str(self.dynamic_log_path),
            "log_tail": self._tail_dynamic_log(),
            "blocks": active_blocks["blocks"],
            "updated_at": active_blocks["updated_at"],
        }

    async def toggle_dynamic_pause(self):
        """User-facing pause/resume for the dynamic engine's translation
        work, without stopping/restarting the process itself. Bound to L4
        long-press while the dynamic engine is running (see index.tsx's
        startHotkeyPolling), and to the QAM toggle button, for when a
        wide-area scene is producing too much noisy/garbled output and the
        user wants it to stop *right now* without waiting on a threshold
        tune or risking the GStreamer/PipeWire reconnect fragility a full
        stop+start (refresh_dynamic_capture) would repeat on every toggle
        (see CAPTURE_STABILITY_HANDOFF.md).

        capture_dynamic.py polls dynamic_pause_flag_path itself (see its
        on_sample()) and skips all work while it exists, leaving tracked
        state exactly as it was so resuming continues seamlessly with no
        fresh discovery needed. This method also blanks active_blocks.json
        immediately on pause, rather than waiting for the subprocess to
        notice, so the display clears the instant the user asks for it -
        except capture_width/capture_height, which are deliberately
        preserved rather than wiped: tap-to-translate (request_tap_
        translate() below) only ever runs while paused, and needs those
        dimensions to convert a screen tap into capture-pixel coordinates,
        but the file that normally carries them (active_blocks.json) would
        otherwise be blank for the entire time tap-to-translate is usable.
        """
        if not self._is_dynamic_running():
            return {"paused": False, "error": "dynamic engine is not running"}
        if self.dynamic_pause_flag_path.exists():
            self.dynamic_pause_flag_path.unlink()
            return {"paused": False}
        self.dynamic_pause_flag_path.touch()
        try:
            existing = json.loads(self.active_blocks_path.read_text(encoding="utf-8"))
        except (OSError, FileNotFoundError, json.JSONDecodeError):
            existing = {}
        blanked = {
            "updated_at": None,
            "blocks": [],
            "capture_width": existing.get("capture_width"),
            "capture_height": existing.get("capture_height"),
        }
        self.active_blocks_path.write_text(json.dumps(blanked), encoding="utf-8")
        return {"paused": True}

    async def set_dynamic_qam_open(self, is_open: bool):
        """Driven by index.tsx polling Steam's own openSideMenu state
        (window.SteamUIStore...m_eOpenSideMenu) live, not by any button
        click - see PHASE_A_HANDOFF.md's 2026-08-19 QAM-cascade writeup.
        Deliberately a separate flag from dynamic_pause_flag_path/
        toggle_dynamic_pause() above rather than reusing it: this is an
        automatic, high-frequency signal tied purely to QAM visibility, and
        must not be able to clobber (or be clobbered by) a user's own
        manual pause/resume state - capture_dynamic.py's on_sample() checks
        both flags independently. Unlike toggle_dynamic_pause(), this never
        touches active_blocks.json - the point is to be invisible to
        whatever's already correctly displayed, not to blank it, since the
        QAM opening/closing isn't a user request about the translation
        display at all.
        """
        if not self._is_dynamic_running():
            return {"qam_open": False}
        if is_open:
            self.dynamic_qam_open_flag_path.touch()
        else:
            try:
                self.dynamic_qam_open_flag_path.unlink()
            except FileNotFoundError:
                pass
        return {"qam_open": is_open}

    async def set_dynamic_status_toast_visible(self, is_visible: bool):
        """Called directly by index.tsx's showToast() around dispatching the
        StatusToast corner notification (L4 refresh/pause/resume feedback),
        not polled like set_dynamic_qam_open() above - the toast's on-screen
        window is entirely self-triggered by that same call, so there's
        nothing to poll for. Found live 2026-08-19: once --startup-delay
        was cut, the L4-refresh toast itself ("PlayTranslate: refreshed")
        started getting OCR'd and "translated" as if it were game dialogue,
        since _own_overlay_regions() in capture_dynamic.py only masks
        PositionedOverlay's tracked translation blocks, not this separate
        toast. Same independence reasoning as set_dynamic_qam_open(): a
        separate flag file, not a reuse of dynamic_pause_flag_path or
        dynamic_qam_open_flag_path, and never touches active_blocks.json.
        """
        if not self._is_dynamic_running():
            return {"status_toast_visible": False}
        if is_visible:
            self.dynamic_status_toast_flag_path.touch()
        else:
            try:
                self.dynamic_status_toast_flag_path.unlink()
            except FileNotFoundError:
                pass
        return {"status_toast_visible": is_visible}

    async def request_tap_translate(self, x, y):
        """Tap-to-translate: the frontend's L4+L2-hold + touch-long-press
        gesture (index.tsx's TapTranslateOverlay) lands here with a tapped
        point already converted to capture-pixel coordinates. Only valid
        while the dynamic engine is running *and* paused - enforced both
        here (so a stray/late call fails fast with a clear reason) and
        again on the capture_dynamic.py side (handle_tap_request() is only
        ever invoked from on_sample()'s paused branch), so this can't
        accidentally compete with live discovery for the same on-screen
        area.

        Request/response go through a pair of flag files
        (tap_request_path/tap_result_path) rather than a direct RPC into
        capture_dynamic.py, matching every other piece of cross-process
        state in this file (active_blocks.json, dynamic_paused.flag) -
        that process only reads its own args, no IPC server of its own.
        """
        if not self._is_dynamic_running():
            return {"ok": False, "error": "dynamic engine is not running"}
        if not self.dynamic_pause_flag_path.exists():
            return {"ok": False, "error": "dynamic engine is not paused"}
        try:
            self.tap_result_path.unlink()
        except FileNotFoundError:
            pass
        try:
            self.tap_request_path.write_text(json.dumps({"x": int(x), "y": int(y)}), encoding="utf-8")
        except OSError as exc:
            return {"ok": False, "error": str(exc)}

        # capture_dynamic.py only services this once per paused frame, so
        # polling needs to comfortably outlast one frame interval plus
        # however long a re-OCR+translate HTTP round trip takes (same order
        # of magnitude as reocr_block()'s existing per-block work).
        for _ in range(25):
            await asyncio.sleep(0.2)
            if self.tap_result_path.exists():
                break
        else:
            return {"ok": False, "error": "timed out waiting for tap result"}

        try:
            result = json.loads(self.tap_result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {"ok": False, "error": str(exc)}
        finally:
            try:
                self.tap_result_path.unlink()
            except FileNotFoundError:
                pass
        return result
