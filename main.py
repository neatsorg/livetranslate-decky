import asyncio
import base64
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
        self.plugin_dir = Path(__file__).resolve().parent
        self.data_dir = Path(getattr(decky, "DECKY_PLUGIN_DIR", self.plugin_dir)).parent.parent / "data" / "PlayTranslate"
        self.log_path = self.data_dir / "playtranslate-capture.log"
        self.translation_path = self.data_dir / "last_translation.txt"
        self.translation_json_path = self.data_dir / "last_translation.json"
        self.translation_error_path = self.data_dir / "last_translation_error.txt"
        self.translate_url_path = self.data_dir / "translate_url.txt"
        self.active_blocks_path = self.data_dir / "active_blocks.json"
        self.hidraw_path = None

    async def _main(self):
        decky.logger.info("PlayTranslate loaded")

    async def _unload(self):
        await self.stop_capture()
        decky.logger.info("PlayTranslate unloaded")

    async def _uninstall(self):
        await self.stop_capture()

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

    def _is_running(self):
        return self.process is not None and self.process.poll() is None

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
        env_url = os.environ.get("PLAYTRANSLATE_TRANSLATE_URL")
        if env_url:
            return self._normalize_translate_url(env_url)
        if self.translate_url_path.exists():
            configured = self._read_text_file(self.translate_url_path)
            if configured:
                return self._normalize_translate_url(configured)
        return "http://192.168.1.32:8787/translate"

    def _normalize_translate_url(self, url):
        url = url.strip()
        parsed = parse.urlparse(url)
        if parsed.scheme and parsed.netloc and parsed.path in ("", "/"):
            return parse.urlunparse(parsed._replace(path="/translate"))
        return url

    def _health_url(self):
        parsed = parse.urlparse(self._translate_url())
        return parse.urlunparse(parsed._replace(path="/health", params="", query="", fragment=""))

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
        try:
            raw = self.active_blocks_path.read_text(encoding="utf-8")
        except (OSError, FileNotFoundError):
            return {"blocks": [], "updated_at": None}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {"blocks": [], "updated_at": None}
        if not isinstance(data, dict) or not isinstance(data.get("blocks"), list):
            return {"blocks": [], "updated_at": None}
        return {"blocks": data["blocks"], "updated_at": data.get("updated_at")}

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

        command = self._distrobox_python_command(worker_py, "--port", str(self._ocr_worker_port()))
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

    def _write_translation_outputs(self, translation, full_result):
        self.translation_path.write_text(translation.strip() + "\n", encoding="utf-8")
        self.translation_json_path.write_text(
            json.dumps(full_result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def _run_translation_via_worker(self, image_path, engine_dir):
        payload = json.dumps(
            {
                "image": str(image_path),
                "regions_json": str(self._regions_json_path(engine_dir)),
                "http_url": self._translate_url(),
                "target_lang": "Japanese",
            }
        ).encode("utf-8")
        req = request.Request(
            self._ocr_worker_translate_url(),
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))

    def _run_translation(self, engine_dir, image_path):
        # Always attempt the worker rather than gating on self.ocr_worker_process:
        # that handle is only set for workers this process itself spawned, so a
        # worker adopted from a previous plugin load (see _ensure_ocr_worker)
        # would otherwise be skipped forever even though it's healthy. A failed
        # call (worker absent, stale, or unhealthy) falls back below regardless.
        try:
            result = self._run_translation_via_worker(image_path, engine_dir)
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

    def _read_hidraw_buttons_once(self, timeout=0.2):
        path = self._find_steamdeck_hidraw()
        if not path:
            return {"success": False, "buttons": [], "error": "Steam Deck hidraw device was not found"}

        buttons_l_masks = {
            "L5": 0x00008000,
            "R5": 0x00010000,
        }
        buttons_h_masks = {
            "L4": 0x00000200,
            "R4": 0x00000400,
        }

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
                buttons_l = struct.unpack("<I", data[8:12])[0]
                buttons_h = struct.unpack("<I", data[12:16])[0]
                buttons = []
                for name, mask in buttons_l_masks.items():
                    if buttons_l & mask:
                        buttons.append(name)
                for name, mask in buttons_h_masks.items():
                    if buttons_h & mask:
                        buttons.append(name)
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

        env = self._subprocess_env()
        env.setdefault("PLAYTRANSLATE_DATA_DIR", str(self.data_dir))
        env.setdefault("PLAYTRANSLATE_ENGINE_DIR", str(engine_dir))
        env.setdefault("PLAYTRANSLATE_TRANSLATE_URL", self._translate_url())
        env.setdefault("PLAYTRANSLATE_REGIONS_JSON", str(self._regions_json_path(engine_dir)))

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
            return await self.status()

        if not capture_py or not config_json:
            return {
                "running": False,
                "error": "capture.py/config.json was not found. Set PLAYTRANSLATE_ENGINE_DIR or place files in bin/.",
                "log_path": str(self.log_path),
            }

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
