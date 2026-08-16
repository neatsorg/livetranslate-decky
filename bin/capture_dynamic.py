#!/usr/bin/env python3
"""Wide-area capture + dynamic text-block discovery/tracking (Phase A).

Validation driver for the new detection architecture described in the
PlayTranslate design discussion: instead of one hand-calibrated fixed ROI
with a single whole-region pixel diff (capture.py's --diff mode), this:

  1. Captures a wide/full-frame region via the same PipeWire/GStreamer path
     as capture.py.
  2. On a "need discovery" trigger, runs full-frame sparse-text OCR via
     ocr_worker's /discover_blocks (confidence-filtered) to find all
     currently-visible text blocks and their positions.
  3. Tracks each discovered block's own pixels independently (region_tracker
     .MultiRegionTracker) so a block's content changing doesn't require
     re-running full-frame OCR, and background motion elsewhere on screen
     doesn't spuriously invalidate blocks that didn't change.
  4. On a block settling into a changed state, re-OCRs just that block's
     crop via ocr_worker's existing /test_region endpoint, then translates
     it (serially - one in-flight translation call at a time, same load on
     the translation backend as today's single-region pipeline).
  5. On a sustained background-wide change (scene cut), drops all tracked
     blocks and re-triggers discovery.

Each block's translation result is kept in a priority-ordered list (most
recently changed first, longer text as a tiebreak) and written to
--output as JSON after every update. main.py's get_active_blocks() reads
that file to drive the HUD: top of the list is shown by default, and a
button (L5) advances through the rest - see the block-display design
discussion for why ordering only needs to be "good enough to start from"
rather than exactly right (the user can always page past a bad guess, and
tap-to-select will later just jump a block to the front directly).

Process supervision (starting/stopping this alongside capture.py, keeping
--output pointed at the right file) is still not wired into main.py -
this is deliberately still run by hand for validation before that cutover.
"""
import argparse
import importlib.util
import json
import sys
import tempfile
import time
from pathlib import Path
from urllib import request as urlrequest

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from region_tracker import MultiRegionTracker


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


capture = load_module(Path(__file__).with_name("capture.py"), "playtranslate_capture")
translate_stub = load_module(Path(__file__).with_name("translate_stub.py"), "playtranslate_translate_stub")


def normalize_text(text):
    """Same as ocr_tesseract.normalize_text - duplicated instead of imported
    because ocr_tesseract.py pulls in PIL at module load, which is only
    installed inside the playtranslate-ocr distrobox container (where
    ocr_worker.py runs), not in this script's host-side Python (which needs
    GStreamer/PipeWire access instead - see capture.py/requirements.txt).
    """
    return "\n".join(line.strip() for line in text.splitlines() if line.strip()).strip()


def is_useful_text(text):
    compact = "".join(ch for ch in text if ch.isalnum())
    return len(compact) >= 2


def http_post_json(url, payload, timeout=10.0):
    data = json.dumps(payload).encode("utf-8")
    req = urlrequest.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urlrequest.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def save_bgrx_crop_png(raw, width, height, bbox, out_path, pad=4):
    """Crop (x0,y0,x1,y1) out of a raw BGRx buffer and save as PNG via GdkPixbuf,
    matching capture.py's own save path (keeps this script's only image
    dependency the same one capture.py already requires).
    """
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GLib, GdkPixbuf

    x0, y0, x1, y1 = bbox
    x0 = max(x0 - pad, 0)
    y0 = max(y0 - pad, 0)
    x1 = min(x1 + pad, width)
    y1 = min(y1 + pad, height)
    crop_w, crop_h = max(x1 - x0, 1), max(y1 - y0, 1)

    rgb = bytearray(crop_w * crop_h * 3)
    dst = 0
    for y in range(y0, y1):
        row_start = (y * width + x0) * 4
        row = raw[row_start : row_start + crop_w * 4]
        for x in range(0, len(row), 4):
            if x + 2 >= len(row):
                break
            rgb[dst] = row[x + 2]
            rgb[dst + 1] = row[x + 1]
            rgb[dst + 2] = row[x]
            dst += 3

    pixbuf = GdkPixbuf.Pixbuf.new_from_bytes(
        GLib.Bytes.new(bytes(rgb)), GdkPixbuf.Colorspace.RGB, False, 8, crop_w, crop_h, crop_w * 3
    )
    pixbuf.savev(str(out_path), "png", [], [])


class DynamicCaptureRunner:
    def __init__(self, args, config):
        self.args = args
        self.config = config
        self.tracker = None
        self.state = "need_discovery"
        self.last_discovery_attempt = 0.0
        self.discovery_min_interval = args.discovery_min_interval
        self.discovering = False
        self.frame_count = 0
        self.temp_dir = Path(tempfile.mkdtemp(prefix="pt_dynamic_"))
        self.last_report = time.monotonic()
        # Per-block metadata not owned by MultiRegionTracker (translation
        # result + timestamps for HUD priority ordering). Keyed by block id,
        # dropped whenever the tracker drops the corresponding block.
        self.block_meta = {}

    def log(self, msg):
        print(msg, flush=True)

    def translate_block(self, block_id, text):
        """Translate one block's OCR text and update block_meta. Runs
        serially (one HTTP call at a time, same as the existing single-region
        pipeline) so this never asks more of the translation backend than
        today's production path does - no per-block parallelism.
        """
        cleaned = normalize_text(text)
        if not is_useful_text(cleaned):
            return None
        try:
            result = translate_stub.post_http(self.args.translate_url, "", cleaned, self.args.target_lang)
            return str(result.get("translation") or "").strip()
        except Exception as exc:
            self.log(f"[block {block_id}] translate failed: {type(exc).__name__}: {exc}")
            return None

    def write_active_blocks(self):
        if not self.args.output:
            return
        now = time.time()
        entries = []
        for block_id, meta in self.block_meta.items():
            block = self.tracker.blocks.get(block_id) if self.tracker else None
            if block is None:
                continue
            # Blocks that failed the is_useful_text check (garbage OCR off
            # game art, not real dialogue) never got translated - keep
            # tracking them internally so the pixel tracker still watches
            # their region, but don't expose them for the HUD/L5 to cycle
            # through. See the block-display design discussion + the L5
            # "cycles through noise" bug report.
            translation = meta.get("translation")
            if not translation:
                continue
            entries.append(
                {
                    "id": block_id,
                    "text": block.text,
                    "translation": translation,
                    "bbox": {"x0": block.bbox[0], "y0": block.bbox[1], "x1": block.bbox[2], "y1": block.bbox[3]},
                    "last_changed": meta.get("last_changed", 0.0),
                }
            )
        # Priority: most-recently-changed first, longer text first as a tiebreak
        # (favors substantial dialogue over short incidental UI labels that were
        # picked up in the same discovery/change batch).
        entries.sort(key=lambda e: (-e["last_changed"], -len(e["text"])))
        payload = {"updated_at": now, "blocks": entries}
        try:
            tmp_path = Path(str(self.args.output) + ".tmp")
            tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp_path.replace(self.args.output)
        except OSError as exc:
            self.log(f"[output] failed to write {self.args.output}: {exc}")

    def maybe_discover(self, raw, width, height):
        now = time.monotonic()
        if self.discovering or now - self.last_discovery_attempt < self.discovery_min_interval:
            return
        self.last_discovery_attempt = now
        self.discovering = True
        try:
            snapshot_path = self.temp_dir / "discovery_frame.png"
            save_bgrx_crop_png(raw, width, height, (0, 0, width, height), snapshot_path, pad=0)
            result = http_post_json(
                self.args.ocr_worker_url + "/discover_blocks",
                {"image": str(snapshot_path), "lang": self.args.lang, "conf_threshold": self.args.conf_threshold},
            )
            blocks = result["blocks"]
            self.log(
                f"[discover] found {len(blocks)} block(s) in {result['elapsed_s']}s: "
                + ", ".join(f"#{b['id']}({b['conf']:.0f}) {b['text'][:30]!r}" for b in blocks)
            )
            if self.tracker is None:
                self.tracker = MultiRegionTracker(width, height)
            self.tracker.set_regions(blocks, raw, width, height)
            self.block_meta = {}
            now = time.time()
            for b in blocks:
                translation = self.translate_block(b["id"], b["text"])
                self.block_meta[b["id"]] = {"translation": translation, "last_changed": now}
            self.state = "tracking"
            self.write_active_blocks()
        except Exception as exc:
            self.log(f"[discover] failed: {type(exc).__name__}: {exc}")
        finally:
            self.discovering = False

    def reocr_block(self, block, raw, width, height):
        crop_path = self.temp_dir / f"block_{block.block_id}.png"
        save_bgrx_crop_png(raw, width, height, block.bbox, crop_path)
        try:
            result = http_post_json(
                self.args.ocr_worker_url + "/test_region",
                {"image": str(crop_path), "region": {"no_crop": True, "psm": 7, "lang": self.args.lang}},
            )
            new_text = result.get("text", "")
            self.log(f"[block {block.block_id}] changed: {block.text[:40]!r} -> {new_text[:40]!r}")
            block.text = new_text
            translation = self.translate_block(block.block_id, new_text)
            self.block_meta[block.block_id] = {"translation": translation, "last_changed": time.time()}
        except Exception as exc:
            self.log(f"[block {block.block_id}] re-ocr failed: {type(exc).__name__}: {exc}")

    def on_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.ERROR
        self.frame_count += 1

        caps = sample.get_caps()
        structure = caps.get_structure(0)
        _, width = structure.get_int("width")
        _, height = structure.get_int("height")
        buffer = sample.get_buffer()
        success, map_info = buffer.map(Gst.MapFlags.READ)
        if not success:
            return Gst.FlowReturn.OK
        try:
            raw = bytes(map_info.data)
        finally:
            buffer.unmap(map_info)

        if self.state == "need_discovery":
            self.maybe_discover(raw, width, height)
        elif self.state == "tracking" and self.tracker is not None:
            changed_ids, stale_ids, scene_changed = self.tracker.update(raw, width, height)
            for block_id in changed_ids:
                block = self.tracker.blocks.get(block_id)
                if block is not None:
                    self.reocr_block(block, raw, width, height)
            for block_id in stale_ids:
                self.block_meta.pop(block_id, None)
            if stale_ids:
                self.log(f"[track] blocks went stale and were dropped: {stale_ids}")
            if scene_changed:
                self.log("[track] sustained background change -> dropping all blocks, re-discovering")
                self.tracker.clear()
                self.block_meta = {}
                self.state = "need_discovery"
            if changed_ids or stale_ids:
                self.write_active_blocks()

        now = time.monotonic()
        if now - self.last_report >= self.args.report_interval:
            n_blocks = len(self.tracker.blocks) if self.tracker else 0
            self.log(f"[status] frames={self.frame_count} state={self.state} tracked_blocks={n_blocks}")
            self.last_report = now
            if self.state == "tracking":
                # Heartbeat: block content is validated stable when unchanged
                # (see the block-display design discussion), but the
                # frontend's freshness check only knows "is this engine
                # still alive" from updated_at - refresh it periodically
                # even with nothing new to report, so a long-static dialogue
                # box doesn't make main.py fall back to the old pipeline.
                self.write_active_blocks()

        return Gst.FlowReturn.OK


def build_pipeline_text(args, config):
    source = capture.source_element(args, config)
    crop = capture.crop_from_config(config)
    return (
        f"{source} ! "
        f"videocrop left={crop['left']} right={crop['right']} top={crop['top']} bottom={crop['bottom']} ! "
        "videoconvert ! "
        "video/x-raw,format=BGRx ! "
        "appsink name=sink emit-signals=true sync=false max-buffers=2 drop=true"
    )


def main():
    parser = argparse.ArgumentParser(description="Dynamic wide-area text-block discovery + tracking (Phase A).")
    parser.add_argument("--config", default="config.json", help="Path to config JSON (roi = capture bounds).")
    parser.add_argument("--target-object", help="PipeWire object.serial or node.name override.")
    parser.add_argument("--path", type=int, help="PipeWire node id fallback for pipewiresrc path=.")
    parser.add_argument("--ocr-worker-url", default="http://127.0.0.1:8788", help="ocr_worker.py base URL.")
    parser.add_argument("--lang", default="eng")
    parser.add_argument("--conf-threshold", type=float, default=70.0, help="Discovery confidence cutoff.")
    parser.add_argument("--discovery-min-interval", type=float, default=2.0, help="Seconds between discovery attempts.")
    parser.add_argument("--translate-url", default="http://192.168.1.32:8787/translate", help="Translation HTTP endpoint.")
    parser.add_argument("--target-lang", default="Japanese")
    parser.add_argument("--output", type=Path, help="Write priority-sorted block+translation JSON here after each update.")
    parser.add_argument("--duration", type=float, help="Stop after N seconds.")
    parser.add_argument("--report-interval", type=float, default=5.0)
    parser.add_argument("--print-pipeline", action="store_true")
    args = parser.parse_args()

    config = capture.load_config(args.config)
    Gst.init(None)

    runner = DynamicCaptureRunner(args, config)
    pipeline_text = build_pipeline_text(args, config)
    if args.print_pipeline:
        print(pipeline_text, flush=True)
    pipeline = Gst.parse_launch(pipeline_text)
    sink = pipeline.get_by_name("sink")
    sink.connect("new-sample", runner.on_sample)
    pipeline.set_state(Gst.State.PLAYING)

    bus = pipeline.get_bus()
    started = time.monotonic()
    try:
        while True:
            msg = bus.timed_pop_filtered(100 * Gst.MSECOND, Gst.MessageType.ERROR | Gst.MessageType.EOS)
            if msg:
                if msg.type == Gst.MessageType.ERROR:
                    err, debug = msg.parse_error()
                    print(f"GStreamer error: {err}", file=sys.stderr)
                    if debug:
                        print(debug, file=sys.stderr)
                    return 1
                if msg.type == Gst.MessageType.EOS:
                    return 0
            if args.duration and time.monotonic() - started >= args.duration:
                return 0
    finally:
        pipeline.set_state(Gst.State.NULL)


if __name__ == "__main__":
    raise SystemExit(main())
