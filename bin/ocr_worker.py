#!/usr/bin/env python3
"""Resident OCR + translate worker.

Runs once inside distrobox and stays up, so tesseract/python startup cost is
paid once instead of on every translation event. The Decky plugin talks to
this over localhost HTTP instead of shelling out to `distrobox enter` per
button press. Falls back to spawning the tesseract CLI per region if
tesserocr (persistent TessBaseAPI) isn't installed, so this works today and
gets faster once tesserocr is added to the box.
"""
import argparse
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image, ImageOps

from ocr_grouping import group_lines_into_blocks

try:
    import tesserocr
except ImportError:
    tesserocr = None


def find_tessdata_prefix():
    """tesserocr wheels vendor their own tesseract and don't reliably find the
    system tessdata dir on their own (seen defaulting to './' on Arch, while
    the tesseract CLI finds /usr/share/tessdata fine via its own compiled-in
    default). Resolve it explicitly instead of trusting tesserocr's default.
    """
    env = os.environ.get("TESSDATA_PREFIX")
    if env and Path(env).is_dir():
        return env
    for candidate in (
        "/usr/share/tessdata",
        "/usr/share/tesseract-ocr/5/tessdata",
        "/usr/share/tesseract-ocr/4.00/tessdata",
        "/usr/local/share/tessdata",
    ):
        if Path(candidate).is_dir():
            return candidate
    return None


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TesserocrEngine:
    """Keeps one TessBaseAPI per (lang, oem) loaded in memory across requests."""

    name = "tesserocr"

    def __init__(self, tessdata_path):
        self._tessdata_path = tessdata_path
        self._lock = threading.Lock()
        self._apis = {}

    def _api_for(self, lang, oem):
        key = (lang, oem)
        api = self._apis.get(key)
        if api is None:
            api = tesserocr.PyTessBaseAPI(path=self._tessdata_path, lang=lang, oem=oem)
            self._apis[key] = api
        return api

    def run(self, image_path, lang, psm, oem):
        with self._lock:
            api = self._api_for(lang, oem)
            api.SetPageSegMode(psm)
            api.SetImageFile(str(image_path))
            return api.GetUTF8Text()


class CliEngine:
    """Spawns the tesseract CLI per call, same as today's behavior."""

    name = "cli"

    def __init__(self, ocr_module, tesseract_path):
        self._ocr = ocr_module
        self._tesseract = tesseract_path

    def run(self, image_path, lang, psm, oem):
        return self._ocr.run_tesseract(self._tesseract, image_path, lang, psm, oem)


def _looks_like_text(text, min_letters=2, min_letter_ratio=0.5):
    """Reject OCR lines that are mostly punctuation/symbol noise rather than
    real words.

    Confirmed live against real Deck discovery logs: full-frame SPARSE_TEXT
    scanning over busy game art frequently misreads small visual details as
    short, symbol-heavy "text" ('|', 'A4', 'J)', '\\/', '¥') at
    deceptively high confidence (60-90) - conf_threshold alone doesn't catch
    this class of noise, and is_useful_text()'s "2+ alnum chars" bar is too
    weak to either ("A4", "J)" both pass it). This is a character-composition
    check instead: real dialogue/UI text is mostly letters, so require a
    minimum letter count and a minimum letters-to-non-space-chars ratio.
    Independent of and complementary to conf_threshold - a garbage line can
    still be individually high-confidence and get rejected here, and a real
    low-confidence line isn't rescued by looking word-like.
    """
    letters = sum(1 for ch in text if ch.isalpha())
    non_space = sum(1 for ch in text if not ch.isspace())
    if non_space == 0 or letters < min_letters:
        return False
    return (letters / non_space) >= min_letter_ratio


def _prepare_discovery_image(image, upscale_pct, autocontrast):
    """Generic, game-agnostic sharpening for full-frame discovery OCR.

    The per-ROI path (ocr_tesseract.prepare_image) can binarize with a fixed
    white_text_threshold because a region is calibrated by hand for one
    known text color/background. Full-frame discovery has no such
    calibration - text polarity and background busyness vary across the
    same frame - so this deliberately stays global/parameter-free instead
    of guessing a per-game threshold: autocontrast stretches whatever
    contrast the frame already has without assuming polarity, and a modest
    upscale gives Tesseract more pixels to resolve small text against, the
    same lever prepare_image() uses (resize) but lighter since this always
    covers the whole screen instead of one small crop.
    """
    image = image.convert("L")
    if autocontrast:
        image = ImageOps.autocontrast(image, cutoff=1)
    if upscale_pct and upscale_pct != 100:
        width = max(int(image.width * upscale_pct / 100), 1)
        height = max(int(image.height * upscale_pct / 100), 1)
        image = image.resize((width, height), Image.Resampling.LANCZOS)
    return image


class DiscoveryEngine:
    """Full-frame sparse-text OCR for dynamic text-block discovery.

    Separate from TesserocrEngine because it needs PSM.SPARSE_TEXT plus
    per-line bounding boxes and confidence, not a single block of plain
    text for one pre-cropped region. tesserocr only, no CLI fallback -
    getting TSV/box data out of the `tesseract` CLI needs parsing a
    different output format, and every deployment target for this so far
    has tesserocr available.
    """

    name = "tesserocr-discovery"

    def __init__(self, tessdata_path):
        self._tessdata_path = tessdata_path
        self._lock = threading.Lock()
        self._apis = {}  # lang -> PyTessBaseAPI

    def _api_for(self, lang):
        api = self._apis.get(lang)
        if api is None:
            api = tesserocr.PyTessBaseAPI(
                path=self._tessdata_path, lang=lang, psm=tesserocr.PSM.SPARSE_TEXT
            )
            self._apis[lang] = api
        return api

    def discover(self, image_path, lang, conf_threshold, upscale_pct=130, autocontrast=True):
        with self._lock:
            api = self._api_for(lang)
            with Image.open(image_path) as raw_image:
                prepared = _prepare_discovery_image(raw_image, upscale_pct, autocontrast)
            api.SetImage(prepared)
            api.Recognize()
            lines = []
            ri = api.GetIterator()
            if ri:
                level = tesserocr.RIL.TEXTLINE
                while True:
                    text = ri.GetUTF8Text(level)
                    conf = ri.Confidence(level)
                    bbox = ri.BoundingBox(level)
                    stripped = text.strip() if text else ""
                    if stripped and bbox and conf >= conf_threshold and _looks_like_text(stripped):
                        lines.append({"text": stripped, "conf": conf, "bbox": bbox})
                    if not ri.Next(level):
                        break
            blocks = group_lines_into_blocks(lines)
            # Line/block bboxes above are in `prepared`'s (possibly upscaled)
            # pixel space - callers need them in the original capture frame's
            # space (they crop/mask/track against the raw, non-upscaled
            # buffer), so scale back down before returning.
            scale = (upscale_pct or 100) / 100.0
            if scale != 1.0:
                for block in blocks:
                    bbox = block["bbox"]
                    # Round-trip through int: downstream consumers (pixel
                    # cropping/masking in capture_dynamic.py) use these in
                    # range()/buffer-slice math that requires ints, same as
                    # the un-scaled bboxes they replace.
                    block["bbox"] = {
                        "x0": int(bbox["x0"] / scale),
                        "y0": int(bbox["y0"] / scale),
                        "x1": int(round(bbox["x1"] / scale)),
                        "y1": int(round(bbox["y1"] / scale)),
                    }
            return blocks


class ChromeScreenAIEngine:
    """Full-frame discovery backed by libchromescreenai.so (see
    ocr_screenai.py), run as a persistent subprocess talked to over a
    JSON-line stdin/stdout protocol - same idea as TesserocrEngine staying
    resident to avoid paying model-load cost per call, but kept in a
    separate process so a native-library crash or an incompatible .so
    build can't take the resident ocr_worker.py HTTP server down with it.

    Chrome Screen AI groups detected lines into blocks/paragraphs itself
    (see proto/chrome_screen_ai.proto's LineBox.block_id), so results come
    back already block-shaped - group_lines_into_blocks() above exists
    only to compensate for Tesseract's SPARSE_TEXT mode returning flat,
    ungrouped lines, and isn't used on this path.
    """

    name = "chromescreenai"

    def __init__(self, script_path, python_path, model_dir, min_confidence=0.5):
        self._script = str(script_path)
        self._python = python_path
        self._model_dir = model_dir
        self._min_confidence = min_confidence
        self._lock = threading.Lock()
        self._proc = None
        self.init_error = None

    def is_available(self):
        return bool(self._model_dir) and os.path.exists(
            os.path.join(self._model_dir, "libchromescreenai.so")
        )

    def _alive(self):
        return self._proc is not None and self._proc.poll() is None

    def _drain_stderr(self, proc):
        def drain():
            try:
                for raw in iter(proc.stderr.readline, b""):
                    if not raw:
                        break
            except Exception:
                pass

        threading.Thread(target=drain, daemon=True).start()

    def _start_locked(self):
        if self._alive():
            return True
        if not self.is_available():
            self.init_error = "Chrome Screen AI model files not downloaded"
            return False
        try:
            self._proc = subprocess.Popen(
                [self._python, self._script, "--worker"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        except OSError as exc:
            self.init_error = f"failed to spawn ocr_screenai.py: {exc}"
            self._proc = None
            return False

        self._drain_stderr(self._proc)
        init_msg = {"type": "init", "model_dir": self._model_dir, "min_confidence": self._min_confidence}
        try:
            self._proc.stdin.write((json.dumps(init_msg) + "\n").encode())
            self._proc.stdin.flush()
            line = self._proc.stdout.readline()
            if not line:
                self.init_error = "worker died before ready response"
                self._kill_locked()
                return False
            ready = json.loads(line.decode().strip())
            if ready.get("error"):
                self.init_error = ready["error"]
                self._kill_locked()
                return False
        except Exception as exc:
            self.init_error = str(exc)
            self._kill_locked()
            return False

        self.init_error = None
        return True

    def _kill_locked(self):
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                try:
                    proc.stdin.write(b'{"type":"shutdown"}\n')
                    proc.stdin.flush()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except Exception:
            pass

    def stop(self):
        with self._lock:
            self._kill_locked()

    def discover(self, image_path, conf_threshold=70.0):
        # conf_threshold arrives on ocr_worker.py's existing 0-100 Tesseract
        # scale (capture_dynamic.py's --conf-threshold / --conf-threshold-
        # retry); ocr_screenai.py's confidence is 0-1, so rescale per call
        # rather than baking a threshold into the worker at init time - the
        # dynamic capture loop retries discovery at a lower threshold when
        # the first pass finds nothing, and that retry has to actually
        # reach the engine each time, not just be silently ignored.
        min_confidence = max(0.0, min(1.0, conf_threshold / 100.0))
        request = {
            "type": "recognize",
            "image_path": str(image_path),
            "min_confidence": min_confidence,
        }
        with self._lock:
            if not self._alive() and not self._start_locked():
                raise RuntimeError(self.init_error or "Chrome Screen AI worker unavailable")
            try:
                self._proc.stdin.write((json.dumps(request) + "\n").encode())
                self._proc.stdin.flush()
                line = self._proc.stdout.readline()
            except Exception as exc:
                self._kill_locked()
                raise RuntimeError(f"Chrome Screen AI worker I/O error: {exc}") from exc
            if not line:
                self._kill_locked()
                raise RuntimeError("Chrome Screen AI worker died mid-request")
            response = json.loads(line.decode().strip())

        if response.get("error"):
            raise RuntimeError(response["error"])
        return response.get("blocks", [])


class Worker:
    def __init__(
        self,
        ocr_script,
        translate_script,
        ocr_engine="tesseract",
        screenai_script=None,
        screenai_model_dir=None,
        screenai_min_confidence=0.5,
    ):
        self.ocr = load_module(ocr_script, "playtranslate_worker_ocr")
        self.translate = load_module(translate_script, "playtranslate_worker_translate")

        self.tesseract_path = None
        self.tesseract_init_error = None
        try:
            self.tesseract_path = self.ocr.require_command("tesseract")
        except RuntimeError as exc:
            self.tesseract_init_error = str(exc)

        tessdata_path = find_tessdata_prefix() if tesserocr is not None else None
        if tesserocr is not None and tessdata_path:
            self.engine = TesserocrEngine(tessdata_path)
            self.discovery_engine = DiscoveryEngine(tessdata_path)
        elif self.tesseract_path is not None:
            if tesserocr is not None:
                print(f"tesserocr installed but no tessdata dir found; using CLI fallback", file=sys.stderr, flush=True)
            self.engine = CliEngine(self.ocr, self.tesseract_path)
            self.discovery_engine = None
        else:
            # Neither tesserocr+tessdata nor the tesseract CLI is available.
            # Not fatal by itself: Dynamic Capture's chromescreenai engine
            # (the default) never touches self.engine/self.discovery_engine
            # (see discover_blocks() below) - only the legacy fixed-region
            # path (_ocr_region/test_region/translate_image) needs one.
            # Confirmed live: this used to raise here unconditionally,
            # which killed the whole worker - including chromescreenai
            # discovery - on any box without Tesseract installed, even
            # though chromescreenai doesn't need it at all.
            print(
                f"tesseract unavailable ({self.tesseract_init_error}) - legacy fixed-region OCR "
                "will error until it's installed; chromescreenai discovery is unaffected",
                file=sys.stderr, flush=True,
            )
            self.engine = None
            self.discovery_engine = None

        self.ocr_engine_name = ocr_engine
        self.screenai_engine = None
        if screenai_script is not None:
            self.screenai_engine = ChromeScreenAIEngine(
                screenai_script, sys.executable, screenai_model_dir, screenai_min_confidence
            )

        self.started_at = time.monotonic()
        self._regions_cache = {}  # path str -> (mtime_ns, regions)

    @property
    def engine_name(self):
        return self.engine.name if self.engine is not None else None

    def _load_regions(self, regions_json):
        path = Path(regions_json)
        mtime_ns = path.stat().st_mtime_ns
        cached = self._regions_cache.get(str(path))
        if cached and cached[0] == mtime_ns:
            return cached[1]
        regions = self.ocr.load_regions(path)
        self._regions_cache[str(path)] = (mtime_ns, regions)
        return regions

    def _ocr_region(self, image_path, region, index):
        name = region.get("name") or f"region_{index}"
        role = region.get("role", "text")
        try:
            if self.engine is None:
                raise RuntimeError(f"tesseract is not available: {self.tesseract_init_error}")
            prepared = self.ocr.prepare_image(image_path, region, f"{index}_{name}")
            raw_text = self.ocr.normalize_text(
                self.engine.run(
                    prepared,
                    region.get("lang", "eng"),
                    int(region.get("psm", 6)),
                    int(region.get("oem", 1)),
                )
            )
            cleaned_text = self.ocr.cleanup_text(raw_text, region)
            error = None
        except Exception as exc:
            raw_text, cleaned_text, error = "", "", str(exc)
            print(f"ocr region '{name}' failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return {
            "name": name,
            "role": role,
            "text": cleaned_text,
            "raw_text": raw_text,
            "useful": self.ocr.is_useful_text(cleaned_text),
            **({"error": error} if error else {}),
        }

    def test_region(self, image_path, region):
        image_path = Path(image_path)
        if not image_path.exists():
            raise FileNotFoundError(str(image_path))
        return self._ocr_region(image_path, region, 0)

    def crop_to_roi(self, image_path, roi, output_path):
        # Used to make a fresh (QAM-free) full-frame screenshot match the
        # coordinate space of last_settled.png before it's shown for OCR
        # region calibration - capture.py crops to this same roi at save
        # time, so region x/y/width/height percentages are only meaningful
        # relative to an image cropped the same way. See Calibration.tsx /
        # main.py's get_latest_steam_screenshot.
        image_path = Path(image_path)
        if not image_path.exists():
            raise FileNotFoundError(str(image_path))
        output_path = Path(output_path)
        with self.ocr.Image.open(image_path) as image:
            image = image.convert("RGB")
            cropped = self.ocr.crop_image(image, roi)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            cropped.save(output_path)

    def discover_blocks(self, image_path, lang="eng", conf_threshold=70.0, upscale_pct=130, autocontrast=True):
        image_path = Path(image_path)
        if not image_path.exists():
            raise FileNotFoundError(str(image_path))
        t0 = time.monotonic()
        engine_used = self.ocr_engine_name

        if self.ocr_engine_name == "chromescreenai" and self.screenai_engine is not None:
            try:
                blocks = self.screenai_engine.discover(image_path, conf_threshold=conf_threshold)
                return {"blocks": blocks, "elapsed_s": round(time.monotonic() - t0, 3), "engine": "chromescreenai"}
            except Exception as exc:
                # Self-heal rather than error out the whole discovery pass:
                # fall back to Tesseract for this call (e.g. model files
                # deleted mid-session, worker crashed and won't restart).
                print(f"chromescreenai discovery failed, falling back to tesseract: {exc}", file=sys.stderr, flush=True)
                engine_used = "tesseract"

        if self.discovery_engine is None:
            raise RuntimeError("discovery requires tesserocr + tessdata (CLI fallback not supported)")
        blocks = self.discovery_engine.discover(image_path, lang, conf_threshold, upscale_pct, autocontrast)
        return {"blocks": blocks, "elapsed_s": round(time.monotonic() - t0, 3), "engine": engine_used}

    def translate_image(self, image_path, regions_json, http_url, target_lang, source_lang="English"):
        t_start = time.monotonic()
        image_path = Path(image_path)
        if not image_path.exists():
            raise FileNotFoundError(str(image_path))
        regions = self._load_regions(regions_json)
        if not regions:
            raise ValueError("No OCR regions configured")

        t_ocr_start = time.monotonic()
        region_timings = []
        ocr_results = []
        for index, region in enumerate(regions):
            t_region_start = time.monotonic()
            ocr_results.append(self._ocr_region(image_path, region, index))
            region_timings.append(
                {
                    "name": region.get("name") or f"region_{index}",
                    "seconds": round(time.monotonic() - t_region_start, 3),
                }
            )
        t_ocr_end = time.monotonic()
        ocr_json = {"regions": ocr_results}

        speaker = self.translate.pick_speaker(ocr_json)
        text = self.translate.strip_leading_speaker(self.translate.collect_text(ocr_json), speaker)
        if not text:
            raise ValueError("No useful text region found")

        t_http_start = time.monotonic()
        result = self.translate.post_http(http_url, speaker, text, target_lang, source_lang=source_lang)
        t_http_end = time.monotonic()
        if result.get("error"):
            # Surfaced verbatim (error/error_type) so the Handler can pass it
            # straight through to main.py instead of collapsing it into a
            # generic "empty translation" - see _handle_translate below.
            return {
                "error": result.get("error"),
                "error_type": result.get("error_type"),
                "ocr": ocr_json,
                "http": result,
                "url": http_url,
            }
        translation = str(result.get("translation") or "").strip()

        timing = {
            "engine": self.engine_name,
            "ocr_total_s": round(t_ocr_end - t_ocr_start, 3),
            "ocr_regions": region_timings,
            "http_translate_s": round(t_http_end - t_http_start, 3),
            "request_total_s": round(time.monotonic() - t_start, 3),
        }
        return {
            "translation": translation,
            "ocr": ocr_json,
            "http": result,
            "url": http_url,
            "timing": timing,
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "PlayTranslateOCRWorker/0.1"

    def send_json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            worker = self.server.worker
            self.send_json(
                200,
                {
                    "ok": True,
                    "engine": worker.engine_name,
                    "tesseract": worker.tesseract_path,
                    "uptime_s": round(time.monotonic() - worker.started_at, 1),
                    "discovery_engine": worker.ocr_engine_name,
                    "chromescreenai_available": (
                        worker.screenai_engine.is_available() if worker.screenai_engine else False
                    ),
                    "chromescreenai_error": (
                        worker.screenai_engine.init_error if worker.screenai_engine else None
                    ),
                },
            )
            return
        self.send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/translate":
            self._handle_translate()
        elif self.path == "/test_region":
            self._handle_test_region()
        elif self.path == "/crop_to_roi":
            self._handle_crop_to_roi()
        elif self.path == "/discover_blocks":
            self._handle_discover_blocks()
        else:
            self.send_json(404, {"error": "not found"})

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _handle_translate(self):
        try:
            payload = self._read_json_body()
            image = payload.get("image")
            regions_json = payload.get("regions_json")
            http_url = payload.get("http_url")
            target_lang = payload.get("target_lang", "Japanese")
            source_lang = payload.get("source_lang", "English")
            if not image or not regions_json or not http_url:
                self.send_json(400, {"error": "image, regions_json, http_url are required"})
                return
            result = self.server.worker.translate_image(
                image, regions_json, http_url, target_lang, source_lang=source_lang
            )
            if result.get("error"):
                self.send_json(502, {"error": result["error"], "error_type": result.get("error_type")})
                return
            self.send_json(200, result)
        except (FileNotFoundError, ValueError) as exc:
            self.send_json(400, {"error": str(exc)})
        except Exception as exc:
            self.send_json(500, {"error": str(exc)})

    def _handle_test_region(self):
        try:
            payload = self._read_json_body()
            image = payload.get("image")
            region = payload.get("region")
            if not image or not isinstance(region, dict):
                self.send_json(400, {"error": "image and region are required"})
                return
            result = self.server.worker.test_region(image, region)
            self.send_json(200, result)
        except (FileNotFoundError, ValueError) as exc:
            self.send_json(400, {"error": str(exc)})
        except Exception as exc:
            self.send_json(500, {"error": str(exc)})

    def _handle_discover_blocks(self):
        try:
            payload = self._read_json_body()
            image = payload.get("image")
            lang = payload.get("lang", "eng")
            conf_threshold = float(payload.get("conf_threshold", 70.0))
            upscale_pct = float(payload.get("upscale_pct", 130))
            autocontrast = bool(payload.get("autocontrast", True))
            if not image:
                self.send_json(400, {"error": "image is required"})
                return
            result = self.server.worker.discover_blocks(image, lang, conf_threshold, upscale_pct, autocontrast)
            self.send_json(200, result)
        except (FileNotFoundError, ValueError) as exc:
            self.send_json(400, {"error": str(exc)})
        except Exception as exc:
            self.send_json(500, {"error": str(exc)})

    def _handle_crop_to_roi(self):
        try:
            payload = self._read_json_body()
            image = payload.get("image")
            roi = payload.get("roi")
            output = payload.get("output")
            if not image or not isinstance(roi, dict) or not output:
                self.send_json(400, {"error": "image, roi, output are required"})
                return
            self.server.worker.crop_to_roi(image, roi, output)
            self.send_json(200, {"ok": True})
        except (FileNotFoundError, ValueError) as exc:
            self.send_json(400, {"error": str(exc)})
        except Exception as exc:
            self.send_json(500, {"error": str(exc)})

    def log_message(self, fmt, *args):
        print(f"{self.client_address[0]} - {fmt % args}", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Resident PlayTranslate OCR+translate worker.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host.")
    parser.add_argument("--port", type=int, default=8788, help="Bind port.")
    parser.add_argument("--ocr-script", type=Path, default=Path(__file__).with_name("ocr_tesseract.py"))
    parser.add_argument("--translate-script", type=Path, default=Path(__file__).with_name("translate_stub.py"))
    parser.add_argument(
        "--ocr-engine", choices=["tesseract", "chromescreenai"], default="chromescreenai",
        help="Full-frame discovery engine (/discover_blocks). Falls back to tesseract if chromescreenai is unavailable "
             "(also selectable directly - kept around as a debug fallback).",
    )
    parser.add_argument("--screenai-script", type=Path, default=Path(__file__).with_name("ocr_screenai.py"))
    parser.add_argument("--screenai-model-dir", default=None, help="Dir containing libchromescreenai.so + models.")
    parser.add_argument("--screenai-min-confidence", type=float, default=0.5)
    args = parser.parse_args()

    worker = Worker(
        args.ocr_script,
        args.translate_script,
        ocr_engine=args.ocr_engine,
        screenai_script=args.screenai_script,
        screenai_model_dir=args.screenai_model_dir,
        screenai_min_confidence=args.screenai_min_confidence,
    )
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.worker = worker
    print(
        f"Listening on http://{args.host}:{args.port} "
        f"engine={worker.engine_name} tesseract={worker.tesseract_path}",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    raise SystemExit(main())
