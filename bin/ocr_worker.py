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
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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


class Worker:
    def __init__(self, ocr_script, translate_script):
        self.ocr = load_module(ocr_script, "playtranslate_worker_ocr")
        self.translate = load_module(translate_script, "playtranslate_worker_translate")

        self.tesseract_path = None
        try:
            self.tesseract_path = self.ocr.require_command("tesseract")
        except RuntimeError:
            if tesserocr is None:
                raise

        tessdata_path = find_tessdata_prefix() if tesserocr is not None else None
        if tesserocr is not None and tessdata_path:
            self.engine = TesserocrEngine(tessdata_path)
        else:
            if tesserocr is not None:
                print(f"tesserocr installed but no tessdata dir found; using CLI fallback", file=sys.stderr, flush=True)
            self.engine = CliEngine(self.ocr, self.tesseract_path)

        self.started_at = time.monotonic()
        self._regions_cache = {}  # path str -> (mtime_ns, regions)

    @property
    def engine_name(self):
        return self.engine.name

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

    def translate_image(self, image_path, regions_json, http_url, target_lang):
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
        text = self.translate.collect_text(ocr_json)
        if not text:
            raise ValueError("No useful text region found")

        t_http_start = time.monotonic()
        result = self.translate.post_http(http_url, speaker, text, target_lang)
        t_http_end = time.monotonic()
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
                },
            )
            return
        self.send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/translate":
            self.send_json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            image = payload.get("image")
            regions_json = payload.get("regions_json")
            http_url = payload.get("http_url")
            target_lang = payload.get("target_lang", "Japanese")
            if not image or not regions_json or not http_url:
                self.send_json(400, {"error": "image, regions_json, http_url are required"})
                return
            result = self.server.worker.translate_image(image, regions_json, http_url, target_lang)
            self.send_json(200, result)
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
    args = parser.parse_args()

    worker = Worker(args.ocr_script, args.translate_script)
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
