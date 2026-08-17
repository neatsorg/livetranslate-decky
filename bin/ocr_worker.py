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
import statistics
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


def _bbox_area(bbox):
    x0, y0, x1, y1 = bbox
    return max(0, x1 - x0) * max(0, y1 - y0)


def _overlap_area(a, b):
    ox = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    oy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    return ox * oy


def _drop_contained_groups(groups, containment_ratio=0.6):
    """Drop groups that are mostly nested inside a larger group's bbox.

    Confirmed live against a real NORCO frame: Tesseract's line detector
    can emit small duplicate fragments ("vast", "saw an") sitting entirely
    inside a large paragraph's bbox that already contains that same text -
    verified by re-running OCR on the identical static image and getting
    byte-identical output both times, ruling out engine non-determinism.
    These nested duplicates were the main source of the block set
    thrashing on that scene: as isolated word-fragments they carry no
    context, translate to noise, and constantly re-trigger the flaky-block
    guard. Kept the larger paragraph group (which read correctly and
    consistently) and drop whatever's mostly inside it, rather than trying
    to fix Tesseract's segmentation upstream.
    """
    ordered = sorted(groups, key=lambda g: -_bbox_area(g["bbox"]))
    kept = []
    for g in ordered:
        area = _bbox_area(g["bbox"])
        if area <= 0:
            continue
        if any(_overlap_area(g["bbox"], k["bbox"]) / area >= containment_ratio for k in kept):
            continue
        kept.append(g)
    return kept


def _merge_same_row_fragments(lines, horizontal_gap_ratio=1.5):
    """Merge OCR "lines" that actually sit on the same visual row into one.

    Confirmed live against a real game frame: Tesseract's SPARSE_TEXT line
    segmentation sometimes splits a single visual line into multiple
    same-row RIL.TEXTLINE fragments wherever the gap between words is
    unusually wide (e.g. "You spoke to Blake," and "learning" - one
    sentence, one row - came back as two separate "lines" 18px apart
    horizontally). Left unmerged, group_lines_into_blocks()'s vertical/
    horizontal-overlap paragraph merge below has no way to recombine them
    (they don't overlap in x, which is exactly the signal it uses to
    distinguish a wrapped paragraph line from unrelated text) - so a
    fragment like "learning" ends up either as its own disconnected
    single-word block, or silently deleted by _drop_contained_groups() for
    incidentally overlapping a neighboring (also wrongly split) paragraph
    block. Merging same-row fragments first, before that pass runs, fixes
    both: the words end up back in the right sentence, and there's nothing
    left over for _drop_contained_groups() to eat.
    """
    ordered = sorted(lines, key=lambda line: (line["bbox"][1], line["bbox"][0]))
    merged = []
    for line in ordered:
        x0, y0, x1, y1 = line["bbox"]
        for existing in merged:
            ex0, ey0, ex1, ey1 = existing["bbox"]
            vertical_overlap = min(y1, ey1) - max(y0, ey0)
            same_row = vertical_overlap > 0 and vertical_overlap >= 0.5 * min(y1 - y0, ey1 - ey0)
            gap_x = max(x0 - ex1, ex0 - x1, 0)
            # Sort order guarantees x0 ascending within a row, so a match
            # is always further right than what's already merged - safe to
            # always append rather than figure out left/right insertion.
            close_enough = gap_x <= horizontal_gap_ratio * max(y1 - y0, ey1 - ey0)
            if same_row and close_enough:
                existing["bbox"] = (min(x0, ex0), min(y0, ey0), max(x1, ex1), max(y1, ey1))
                existing["text"] = existing["text"] + " " + line["text"]
                existing["conf"] = min(existing["conf"], line["conf"])
                break
        else:
            merged.append({"bbox": (x0, y0, x1, y1), "text": line["text"], "conf": line["conf"]})
    return merged


def group_lines_into_blocks(lines, vertical_gap_ratio=0.8, horizontal_overlap_ratio=0.3):
    """Merge individual OCR text lines into paragraph-like blocks.

    Tesseract's PSM.SPARSE_TEXT doesn't build real paragraph/block structure -
    verified empirically against real game frames: iterating at RIL.PARA or
    RIL.BLOCK returns exactly the same one-line-per-item output as
    RIL.TEXTLINE, since sparse mode assumes scattered, layout-less text. A
    dialogue message that wraps across N lines therefore comes back as N
    separate same-priority "blocks" unless grouped here - which then get
    discovered/tracked/translated as unrelated fragments instead of one
    coherent message.

    Two lines merge when they're vertically close *relative to their own
    line height* (not a fixed pixel constant, so this scales across font
    sizes/DPI/game resolutions) and their horizontal spans substantially
    overlap - the overlap check is what distinguishes a wrapped paragraph
    from an unrelated line that happens to sit at a similar height (e.g. a
    HUD icon off to the side of a dialogue box).

    vertical_gap_ratio defaults to 0.8, not a tighter-looking 0.6 - measured
    live against a real game frame's line pitch (row-to-row gaps of 6-10px
    against line heights of 12-17px put the *real* ratio right around
    0.6-0.7 with essentially no margin, so normal OCR bbox jitter of a
    couple px was enough to miss the tolerance check and split a paragraph
    mid-sentence). Inter-paragraph gaps in the same frame were 30px+, so
    0.8 still leaves a wide margin before that - checked up to 1.0 without
    any false merges across real paragraph boundaries.

    Input: [{"text", "conf", "bbox": (x0,y0,x1,y1)}, ...]
    Output: [{"id", "text", "conf", "bbox": {"x0","y0","x1","y1"}}, ...]
    """
    ordered = _merge_same_row_fragments(lines)
    groups = []
    for line in ordered:
        x0, y0, x1, y1 = line["bbox"]
        for group in groups:
            gx0, gy0, gx1, gy1 = group["bbox"]
            line_h = y1 - y0
            # Median of the *individual* line heights merged so far, not the
            # group bbox's total span (gy1 - gy0) - the latter grows with
            # every absorbed line, so the gap tolerance below would keep
            # widening and make a group progressively more eager to pull in
            # unrelated lines underneath it. Median (vs. e.g. the previous
            # line alone) also survives one stray mis-sized line without
            # throwing off tolerance for the rest of the group.
            ref_h = statistics.median(group["heights"])
            gap = max(y0 - gy1, 0)
            vertical_ok = gap <= vertical_gap_ratio * max(line_h, ref_h, 1)
            overlap = min(x1, gx1) - max(x0, gx0)
            shorter_width = min(x1 - x0, gx1 - gx0)
            horizontal_ok = shorter_width > 0 and overlap / shorter_width >= horizontal_overlap_ratio
            if vertical_ok and horizontal_ok:
                group["bbox"] = (min(x0, gx0), min(y0, gy0), max(x1, gx1), max(y1, gy1))
                group["texts"].append(line["text"])
                group["confs"].append(line["conf"])
                group["heights"].append(line_h)
                break
        else:
            groups.append(
                {"bbox": (x0, y0, x1, y1), "texts": [line["text"]], "confs": [line["conf"]], "heights": [y1 - y0]}
            )

    groups = _drop_contained_groups(groups)

    blocks = []
    for group_id, group in enumerate(groups):
        gx0, gy0, gx1, gy1 = group["bbox"]
        blocks.append(
            {
                "id": group_id,
                "text": " ".join(group["texts"]),
                "conf": round(min(group["confs"]), 1),
                "bbox": {"x0": gx0, "y0": gy0, "x1": gx1, "y1": gy1},
            }
        )
    return blocks


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

    def discover(self, image_path, lang, conf_threshold):
        with self._lock:
            api = self._api_for(lang)
            api.SetImageFile(str(image_path))
            api.Recognize()
            lines = []
            ri = api.GetIterator()
            if ri:
                level = tesserocr.RIL.TEXTLINE
                while True:
                    text = ri.GetUTF8Text(level)
                    conf = ri.Confidence(level)
                    bbox = ri.BoundingBox(level)
                    if text and text.strip() and bbox and conf >= conf_threshold:
                        lines.append({"text": text.strip(), "conf": conf, "bbox": bbox})
                    if not ri.Next(level):
                        break
            return group_lines_into_blocks(lines)


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
            self.discovery_engine = DiscoveryEngine(tessdata_path)
        else:
            if tesserocr is not None:
                print(f"tesserocr installed but no tessdata dir found; using CLI fallback", file=sys.stderr, flush=True)
            self.engine = CliEngine(self.ocr, self.tesseract_path)
            self.discovery_engine = None

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

    def discover_blocks(self, image_path, lang="eng", conf_threshold=70.0):
        if self.discovery_engine is None:
            raise RuntimeError("discovery requires tesserocr + tessdata (CLI fallback not supported)")
        image_path = Path(image_path)
        if not image_path.exists():
            raise FileNotFoundError(str(image_path))
        t0 = time.monotonic()
        blocks = self.discovery_engine.discover(image_path, lang, conf_threshold)
        return {"blocks": blocks, "elapsed_s": round(time.monotonic() - t0, 3)}

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
        text = self.translate.strip_leading_speaker(self.translate.collect_text(ocr_json), speaker)
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
            if not image or not regions_json or not http_url:
                self.send_json(400, {"error": "image, regions_json, http_url are required"})
                return
            result = self.server.worker.translate_image(image, regions_json, http_url, target_lang)
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
            if not image:
                self.send_json(400, {"error": "image is required"})
                return
            result = self.server.worker.discover_blocks(image, lang, conf_threshold)
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
