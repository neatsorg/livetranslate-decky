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


def looks_like_text(text, min_letters=2, min_letter_ratio=0.5):
    """Same check as ocr_worker.DiscoveryEngine's _looks_like_text (see its
    docstring) - duplicated here for the same PIL-import-boundary reason as
    normalize_text() above, not because the logic differs.

    Applying this only in discover_blocks() (ocr_worker.py) isn't enough on
    its own: reocr_block() below re-OCRs an already-tracked block whenever
    its pixels change (region_tracker's diff), which on a visually flickery
    game fires far more often than discovery ever runs - confirmed live
    that most of the garbage a user actually sees on screen comes from here
    ('wy' -> 'vr |', '4]' -> 'wy', etc.), not from discovery adding new
    noise blocks. translate_block() is the one chokepoint both discovery
    and reocr_block() funnel through before calling the translation LLM, so
    that's where this gets applied instead of in each caller separately.
    """
    letters = sum(1 for ch in text if ch.isalpha())
    non_space = sum(1 for ch in text if not ch.isspace())
    if non_space == 0 or letters < min_letters:
        return False
    return (letters / non_space) >= min_letter_ratio


def is_probably_name_label(text):
    """Heuristic: a short, single-word, all-caps token with no sentence
    punctuation looks like a speaker-name tag ("MIA", "AGATHA"), not a line
    of dialogue. Confirmed live: translating a bare name through the same
    dialogue-oriented prompt as real text misfires - translate_server's
    game-profile context (which documents each character's preferred
    first-person pronoun *for use inside their dialogue*) got applied to
    the name itself, so "MIA" came back as "私" instead of staying "MIA".
    Skipping translation for these is blunter than properly detecting
    speaker-vs-dialogue blocks and passing the name via the `speaker` field
    instead, but it's cheap and avoids showing an actively wrong
    translation for a narrow, easy-to-detect case.
    """
    if len(text) > 15 or " " in text:
        return False
    if any(ch.islower() for ch in text):
        return False
    return not any(ch in ".!?…" for ch in text)


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


def _mask_regions_bgrx(raw, width, height, regions):
    """Return a copy of a raw BGRx frame with each (x0,y0,x1,y1) in
    `regions` painted solid black.

    Used to blank out PlayTranslate's own on-screen overlay before it gets
    OCR'd back - confirmed live (screenshot: a discovery_frame.png snapshot
    showing the positioned overlay's own translated-text boxes rendered on
    top of the original game text) that gamescope's captured compositor
    output includes anything drawn on top, including this plugin's own
    Decky/CEF overlay. The existing suppress() guard in region_tracker.py
    only protects an already-tracked block's own pixel-diff baseline from
    this; it does nothing for a fresh full-frame discovery scan or for a
    re-OCR crop that happens to overlap a *different* block's overlay box.
    Never mutates `raw` itself - callers still need the true current frame
    for baseline/diff sampling elsewhere.
    """
    masked = bytearray(raw)
    row_bytes = width * 4
    for (rx0, ry0, rx1, ry1) in regions:
        rx0 = max(0, min(rx0, width))
        rx1 = max(0, min(rx1, width))
        ry0 = max(0, min(ry0, height))
        ry1 = max(0, min(ry1, height))
        if rx1 <= rx0 or ry1 <= ry0:
            continue
        blank_row = b"\x00" * ((rx1 - rx0) * 4)
        for y in range(ry0, ry1):
            row_start = y * row_bytes + rx0 * 4
            masked[row_start : row_start + len(blank_row)] = blank_row
    return masked


# Matches PositionedOverlay's own CSS constants (index.tsx: fontSize 12px,
# lineHeight 15px, padding "3px 7px") - used to estimate how tall its
# rendered box for a given translation will actually be on screen, since
# capture_dynamic.py has no visibility into the frontend's real DOM layout.
_OVERLAY_LINE_HEIGHT_PX = 15
_OVERLAY_VPADDING_PX = 6  # 3px top + 3px bottom
_OVERLAY_HPADDING_PX = 14  # 7px left + 7px right
_OVERLAY_AVG_CHAR_PX = 15  # ~= full-width CJK glyph cell at 12px font/weight 600 -
# raised from an initial 12px estimate after live measurement (a 57-char
# Japanese translation in a ~300px-wide box rendered ~3 visual lines, not
# the ~2 the earlier constant predicted)
_OVERLAY_SAFETY_MULT = 1.3  # extra margin on top of the char-count estimate


def _estimate_overlay_height_px(translation, box_width_px, min_height_px=0):
    """Deliberately generous, not pixel-exact (real wrapping depends on
    font metrics/kerning this process can't see) - a masking rectangle
    that's a bit too tall just hides a bit more of the frame from this
    one discovery/re-OCR pass, while one that's too short leaves the
    overlay's own text exposed to OCR again, defeating the point. Confirmed
    live that even a calibrated-looking char-width estimate can still
    undershoot real rendered height, so this also takes a floor
    (`min_height_px`, callers pass the block's own original bbox height)
    and a blanket safety multiplier on top of the character-count math,
    rather than trusting the math alone.
    """
    if not translation:
        estimate = _OVERLAY_LINE_HEIGHT_PX + _OVERLAY_VPADDING_PX
    else:
        usable_width = max(box_width_px - _OVERLAY_HPADDING_PX, _OVERLAY_AVG_CHAR_PX)
        chars_per_line = max(1, int(usable_width // _OVERLAY_AVG_CHAR_PX))
        line_count = max(1, -(-len(translation) // chars_per_line))  # ceil div
        estimate = line_count * _OVERLAY_LINE_HEIGHT_PX + _OVERLAY_VPADDING_PX
    return max(int(estimate * _OVERLAY_SAFETY_MULT), min_height_px)


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
        self.last_discovery_success = time.monotonic()
        self.started_at = time.monotonic()
        # Per-block metadata not owned by MultiRegionTracker (translation
        # result + timestamps for HUD priority ordering). Keyed by block id,
        # dropped whenever the tracker drops the corresponding block.
        self.block_meta = {}
        # Last translation actually written to --output per block id, so
        # write_active_blocks() can tell when a block's on-screen display is
        # about to change (see the suppress() call there) - reset alongside
        # block_meta whenever a fresh discovery invalidates all block ids.
        self._last_written_translation = {}

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
        if not looks_like_text(cleaned):
            return None
        if is_probably_name_label(cleaned):
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
        current_translations = {}
        for block_id, meta in self.block_meta.items():
            block = self.tracker.blocks.get(block_id) if self.tracker else None
            if block is None:
                continue
            translation = meta.get("translation")
            current_translations[block_id] = translation
            # Blocks that failed the is_useful_text check (garbage OCR off
            # game art, not real dialogue) never got translated - keep
            # tracking them internally so the pixel tracker still watches
            # their region, but don't expose them for the HUD/L5 to cycle
            # through. See the block-display design discussion + the L5
            # "cycles through noise" bug report.
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
        # PositionedOverlay renders each block's translation at the block's
        # own bbox - the same region MultiRegionTracker samples from the
        # PipeWire feed. Any block whose displayed translation just changed
        # (appeared, disappeared, or was replaced by different text) is
        # about to cause a self-inflicted pixel change there once the
        # frontend's next poll picks this up, which would otherwise
        # re-trigger tracking on itself forever (see PHASE_A_HANDOFF.md's
        # feedback-loop writeup). Suppress diffing on exactly those blocks
        # for a window that comfortably outlasts the frontend's poll
        # interval + repaint.
        if self.tracker:
            for block_id, translation in current_translations.items():
                if translation != self._last_written_translation.get(block_id):
                    self.tracker.suppress(block_id, self.args.overlay_transition_guard_s)
        self._last_written_translation = current_translations

        # Priority: most-recently-changed first, longer text first as a tiebreak
        # (favors substantial dialogue over short incidental UI labels that were
        # picked up in the same discovery/change batch).
        entries.sort(key=lambda e: (-e["last_changed"], -len(e["text"])))
        payload = {
            "updated_at": now,
            "blocks": entries,
            # Bboxes are in these pixel dimensions - the frontend needs them
            # to convert to percentage-based CSS positioning for the
            # position-anchored overlay, since the capture resolution isn't
            # guaranteed to match the panel's actual render size.
            "capture_width": self.tracker.width if self.tracker else None,
            "capture_height": self.tracker.height if self.tracker else None,
        }
        try:
            tmp_path = Path(str(self.args.output) + ".tmp")
            tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp_path.replace(self.args.output)
        except OSError as exc:
            self.log(f"[output] failed to write {self.args.output}: {exc}")

    def _own_overlay_regions(self, exclude_block_id=None):
        """Screen-space rectangles PositionedOverlay is currently rendering
        translated text into (see _mask_regions_bgrx()'s docstring for why
        this needs to exist at all). `exclude_block_id` skips a block's own
        region - reocr_block() wants to mask *other* blocks' overlays that
        might bleed into its crop, not blank out the very thing it's
        trying to re-OCR.
        """
        regions = []
        if self.tracker is None:
            return regions
        for block_id, meta in self.block_meta.items():
            if block_id == exclude_block_id:
                continue
            translation = meta.get("translation")
            if not translation:
                continue
            block = self.tracker.blocks.get(block_id)
            if block is None:
                continue
            x0, y0, x1, y1 = block.bbox
            box_width = max(x1 - x0, 80)  # matches PositionedOverlay's CSS minWidth
            box_height = _estimate_overlay_height_px(translation, box_width, min_height_px=y1 - y0)
            regions.append((x0, y0, x1, y0 + box_height))
        return regions

    def maybe_discover(self, raw, width, height, reason="periodic"):
        now = time.monotonic()
        if now - self.started_at < self.args.startup_delay:
            # This process is always started by pressing a QAM button, so
            # the QAM sidebar is guaranteed to be open at that exact moment
            # - discovering immediately reliably bakes the sidebar's own
            # text into the block set (confirmed live), which nothing then
            # cleans up quickly (see the periodic-rediscover comment above).
            # Give the user a moment to close it first.
            return
        if self.discovering or now - self.last_discovery_attempt < self.discovery_min_interval:
            return
        self.last_discovery_attempt = now
        self.discovering = True
        try:
            snapshot_path = self.temp_dir / "discovery_frame.png"
            mask_regions = self._own_overlay_regions()
            snapshot_raw = _mask_regions_bgrx(raw, width, height, mask_regions) if mask_regions else raw
            save_bgrx_crop_png(snapshot_raw, width, height, (0, 0, width, height), snapshot_path, pad=0)

            def _discover_at(conf_threshold):
                result = http_post_json(
                    self.args.ocr_worker_url + "/discover_blocks",
                    {
                        "image": str(snapshot_path),
                        "lang": self.args.lang,
                        "conf_threshold": conf_threshold,
                        "upscale_pct": self.args.discovery_upscale_pct,
                        "autocontrast": self.args.discovery_autocontrast,
                    },
                )
                self.log(
                    f"[discover:{reason}] conf>={conf_threshold:.0f} found {len(result['blocks'])} "
                    f"block(s) in {result['elapsed_s']}s: "
                    + ", ".join(f"#{b['id']}({b['conf']:.0f}) {b['text'][:30]!r}" for b in result["blocks"])
                )
                return result

            result = _discover_at(self.args.conf_threshold)
            if not result["blocks"] and self.args.conf_threshold_retry < self.args.conf_threshold:
                # Same discovery attempt (no extra discovery_min_interval
                # wait), one immediate retry at a looser threshold - this is
                # what recovers the "70 sometimes misses real text on a
                # given frame" case the threshold was previously lowered
                # globally to fix, without paying for that permanently on
                # every other attempt too.
                result = _discover_at(self.args.conf_threshold_retry)
            blocks = result["blocks"]
            self.last_discovery_success = time.monotonic()
            if not blocks:
                # Don't touch whatever's currently tracked/displayed - a
                # single OCR miss doesn't prove the screen is actually
                # empty (confirmed live: discovery flips between finding
                # real text and finding nothing on consecutive attempts
                # against the same static screen). Leaving the existing
                # set alone means the display only ever gets *replaced* by
                # something better, never blanked by a failed retry -
                # on_sample() keeps calling this at discovery_min_interval
                # cadence while nothing is tracked, or at
                # periodic_rediscover_interval cadence otherwise.
                return
            if self.tracker is None:
                self.tracker = MultiRegionTracker(width, height)
            # merge, not set_regions(): a successful discovery pass can
            # still miss an already-tracked block (confirmed live - see
            # merge_regions()'s docstring for the exact incident), so only
            # genuinely new blocks get added; existing ones (and their
            # displayed translations) are left alone - except a block whose
            # bbox merge_regions() can tell was too small (partial-text
            # capture that later discovery proves has grown), which it
            # expands/refines in place instead (also see its docstring).
            added, updated = self.tracker.merge_regions(blocks, raw, width, height)
            if added or updated:
                now = time.time()
                for block_id, text, _conf in added + updated:
                    translation = self.translate_block(block_id, text)
                    self.block_meta[block_id] = {"translation": translation, "last_changed": now}
                if updated:
                    self.log(
                        "[discover] expanded previously-partial block(s): "
                        + ", ".join(f"#{block_id} {text[:30]!r}" for block_id, text, _conf in updated)
                    )
                self.write_active_blocks()
            self.state = "tracking"
        except Exception as exc:
            self.log(f"[discover] failed: {type(exc).__name__}: {exc}")
        finally:
            self.discovering = False

    def _find_block_at_point(self, x, y, pad=6):
        """Which tracked block (if any) a tap-to-translate coordinate landed
        in - padded slightly since a tap can land a few px outside the exact
        OCR bbox even when the user is clearly aiming at that block's text.
        Per the user's own design decision: a tap that matches nothing
        returns None and the caller does nothing (no ad hoc OCR fallback) -
        avoids misreading decorative art/UI chrome as dialogue.
        """
        if self.tracker is None:
            return None
        for block in self.tracker.blocks.values():
            x0, y0, x1, y1 = block.bbox
            if x0 - pad <= x <= x1 + pad and y0 - pad <= y <= y1 + pad:
                return block
        return None

    def handle_tap_request(self, raw, width, height):
        """Tap-to-translate: only ever called from on_sample()'s paused
        branch (see there), so this is the enforcement point for "this
        feature only works while translation is paused" - not just a
        frontend-side gate. main.py's request_tap_translate() writes
        --tap-request with a tapped point (capture-pixel coords, from the
        frontend's L4+L2-hold + touch-long-press gesture) and polls
        --tap-result for the response.
        """
        if not self.args.tap_request or not self.args.tap_request.exists():
            return
        try:
            payload = json.loads(self.args.tap_request.read_text(encoding="utf-8"))
            x, y = int(payload["x"]), int(payload["y"])
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            self.log(f"[tap] bad request: {exc}")
            try:
                self.args.tap_request.unlink()
            except FileNotFoundError:
                pass
            return
        try:
            # Unlink immediately, before doing any OCR/translate work below -
            # a single in-flight request at a time, and this stops the same
            # request being reprocessed on a later paused frame if this one
            # takes longer than a frame interval.
            self.args.tap_request.unlink()
        except FileNotFoundError:
            pass

        try:
            block = self._find_block_at_point(x, y)
            if block is None:
                result = {"ok": True, "matched": False}
            else:
                self.log(f"[tap] ({x},{y}) matched block {block.block_id}")
                self.reocr_block(block, raw, width, height)
                meta = self.block_meta.get(block.block_id, {})
                result = {
                    "ok": True,
                    "matched": True,
                    "text": block.text,
                    "translation": meta.get("translation") or "",
                    "bbox": {"x0": block.bbox[0], "y0": block.bbox[1], "x1": block.bbox[2], "y1": block.bbox[3]},
                }
        except Exception as exc:
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        try:
            tmp_path = Path(str(self.args.tap_result) + ".tmp")
            tmp_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            tmp_path.replace(self.args.tap_result)
        except OSError as exc:
            self.log(f"[tap] failed to write result: {exc}")

    def reocr_block(self, block, raw, width, height):
        crop_path = self.temp_dir / f"block_{block.block_id}.png"
        # Mask *other* blocks' overlay boxes only - not this block's own
        # (nothing would be left to OCR). Two tracked blocks sitting close
        # enough for their positioned-overlay boxes to visually collide is
        # exactly the case PHASE_A_HANDOFF.md's CSS-spacing note flagged;
        # this stops that collision from also corrupting re-OCR, not just
        # looking bad on screen.
        mask_regions = self._own_overlay_regions(exclude_block_id=block.block_id)
        crop_raw = _mask_regions_bgrx(raw, width, height, mask_regions) if mask_regions else raw
        save_bgrx_crop_png(crop_raw, width, height, block.bbox, crop_path)
        try:
            result = http_post_json(
                self.args.ocr_worker_url + "/test_region",
                # psm 6 ("uniform block of text"), not 7 ("single text
                # line"): a tracked block's crop can span multiple lines of
                # the same dialogue (discovery's group_lines_into_blocks()
                # grouped them together in the first place). psm 7 assumed
                # exactly one line, so re-OCR after a content change
                # garbled/truncated any block that started out multi-line -
                # confirmed via PHASE_A_HANDOFF.md code review. Omitting
                # "psm" here defers to _ocr_region()'s own default (6).
                {"image": str(crop_path), "region": {"no_crop": True, "lang": self.args.lang}},
            )
            # test_region already strips blank lines but keeps "\n" between
            # real ones (ocr_tesseract.normalize_text) - join with spaces
            # instead, matching how discover_blocks()/group_lines_into_
            # blocks() format a multi-line block's text (" ".join(texts)),
            # so a re-OCR'd block's text stays in the same shape whether it
            # came from initial discovery or a later content change.
            new_text = result.get("text", "").replace("\n", " ")
            self.log(f"[block {block.block_id}] changed: {block.text[:40]!r} -> {new_text[:40]!r}")
            block.text = new_text
            translation = self.translate_block(block.block_id, new_text)
            prev_meta = self.block_meta.get(block.block_id, {})
            if translation != prev_meta.get("translation"):
                # Real content change - this is what HUD priority (most
                # recently changed first) should actually track.
                self.block_meta[block.block_id] = {"translation": translation, "last_changed": time.time()}
            else:
                # The pixel tracker fired (e.g. a small block like a speaker
                # name flickering from background noise near its edges,
                # confirmed live), but re-OCR/re-translate landed back on
                # the same content. Keep the old last_changed - otherwise a
                # noisy-but-unchanged block keeps outranking a genuinely
                # stable, larger dialogue block in the priority queue just
                # by being noisy, which is exactly backwards.
                self.block_meta[block.block_id] = {
                    "translation": translation,
                    "last_changed": prev_meta.get("last_changed", time.time()),
                }
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

        if self.args.pause_flag and self.args.pause_flag.exists():
            # User-triggered pause (L4 long-press / QAM toggle - see
            # main.py's toggle_dynamic_pause()). Still pull/map/unmap every
            # frame above to keep the GStreamer pipeline flowing without a
            # backlog, but do none of the discovery/tracking/translation
            # work and touch no tracked state, so resuming continues
            # exactly where it left off. main.py clears active_blocks.json
            # itself when it sets this flag, for instant display feedback -
            # this process doesn't need to do that part.
            #
            # Tap-to-translate (L4+L2 hold + touch long-press, see index.tsx)
            # is serviced here and only here - it's a deliberately paused-
            # only feature (see the design discussion), and this branch
            # already still pulls/maps a live raw frame every tick even
            # while paused, so a tapped block's re-OCR always reads current
            # pixels, not a stale frame from whenever pause was toggled.
            self.handle_tap_request(raw, width, height)
            return Gst.FlowReturn.OK

        if self.state == "need_discovery":
            self.maybe_discover(raw, width, height, reason="startup")
        elif self.state == "tracking" and self.tracker is not None:
            changed_ids, pending_ids, stale_ids, scene_changed = self.tracker.update(raw, width, height)
            for block_id in pending_ids:
                # Content just started changing (e.g. a scrolling/growing
                # NORCO-style text box mid-scroll) - the translation we last
                # showed for this position no longer matches what's on
                # screen there. Clear it immediately rather than leaving it
                # up until the new content happens to settle: write_active_
                # blocks() already excludes entries with no translation, so
                # this makes the block disappear from the HUD right away.
                # reocr_block() (below, once it settles) will repopulate it.
                if block_id in self.block_meta:
                    if self.block_meta[block_id]["translation"]:
                        self.log(f"[block {block_id}] pending -> hiding until it resettles")
                    self.block_meta[block_id]["translation"] = None
            for block_id in changed_ids:
                block = self.tracker.blocks.get(block_id)
                if block is not None:
                    self.reocr_block(block, raw, width, height)
            for block_id in stale_ids:
                self.block_meta.pop(block_id, None)
            if stale_ids:
                self.log(f"[track] blocks went stale and were dropped: {stale_ids}")
            if scene_changed:
                # A directly-observed real transition - the old block set is
                # confirmed wrong now, so this is the one case that still
                # blanks the display immediately rather than waiting for a
                # replacement (see maybe_discover()'s "don't touch on a
                # miss" comment for why the other two triggers below don't).
                self.log("[track] sustained background change -> dropping all blocks, re-discovering")
                self.tracker.clear()
                self.block_meta = {}
                self._last_written_translation = {}
                self.state = "need_discovery"
            elif not self.tracker.blocks:
                # Nothing currently tracked/displayed (e.g. every block went
                # stale independently without a scene_changed event) - retry
                # discovery as soon as discovery_min_interval allows rather
                # than waiting out the full periodic_rediscover_interval
                # below. Safe to call every frame: maybe_discover() only
                # ever adds to the (already-empty) tracked set on success,
                # never blanks it further, and self-throttles internally.
                self.maybe_discover(raw, width, height, reason="empty")
            elif time.monotonic() - self.last_discovery_success >= self.args.periodic_rediscover_interval:
                # Self-healing catch-all, not a primary trigger: scene-change
                # detection only fires on a *transition* it directly
                # observes. A block set discovered while something
                # transient covered the screen (confirmed live: the Decky
                # QAM overlay) can settle into stable-but-wrong content
                # before this process's baseline ever captures the change,
                # so there's nothing left to detect - and a block that
                # settles once into static garbage and then stops changing
                # never trips the flaky-block guard either (that needs
                # *repeated* settling). Forcing a fresh discovery on a
                # cadence bounds how long any such staleness can persist,
                # without needing to diagnose which specific trigger missed
                # it each time. Non-destructive like the branch above - only
                # merges newly-found blocks into the tracked set, never
                # replaces or blanks what's already there. Logging happens
                # inside maybe_discover() itself (gated by its own
                # discovery_min_interval throttle) rather than here - this
                # elif re-evaluates true on every captured frame until an
                # attempt actually goes through and resets
                # last_discovery_success, so a log call unconditionally
                # placed here would print on every single frame in between
                # instead of once per actual attempt (confirmed live:
                # thousands of near-duplicate "self-healing check" lines
                # with a flush=True write each, likely adding real
                # per-frame I/O overhead).
                self.maybe_discover(raw, width, height, reason="self-healing")
            if changed_ids or pending_ids or stale_ids or scene_changed:
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
    parser.add_argument(
        "--conf-threshold",
        type=float,
        default=70.0,
        help=(
            "Discovery confidence cutoff, tried first on every discovery attempt. Was "
            "lowered to 60 in an earlier session because 70 sometimes missed real "
            "dialogue/title text on a given frame - but that also pulled in a lot more "
            "decorative-glyph noise permanently (confirmed live: single-symbol garbage "
            "blocks like '|', 'A4', 'J)' at conf 60-90). Restored to 70 now that a miss "
            "at this threshold is handled by an immediate same-attempt retry at "
            "--conf-threshold-retry instead of permanently lowering the bar - "
            "see maybe_discover()."
        ),
    )
    parser.add_argument(
        "--conf-threshold-retry",
        type=float,
        default=55.0,
        help=(
            "Fallback confidence cutoff, tried once immediately (same discovery attempt, "
            "no extra wait) only when --conf-threshold finds zero blocks. Recovers the "
            "case --conf-threshold=70 was originally lowered for, without leaving the "
            "looser threshold in effect on every attempt."
        ),
    )
    parser.add_argument(
        "--discovery-upscale-pct",
        type=float,
        default=130.0,
        help=(
            "Upscale full-frame discovery snapshots to this %% before OCR (100 = off). "
            "Game-agnostic (no fixed per-game threshold, unlike the per-region "
            "white_text_threshold path) - gives Tesseract more pixels to resolve small "
            "text against on a full-screen scan."
        ),
    )
    parser.add_argument(
        "--discovery-autocontrast",
        dest="discovery_autocontrast",
        action="store_true",
        default=True,
        help="Autocontrast-stretch discovery snapshots before OCR (default on).",
    )
    parser.add_argument(
        "--no-discovery-autocontrast",
        dest="discovery_autocontrast",
        action="store_false",
        help="Disable discovery autocontrast preprocessing.",
    )
    parser.add_argument("--discovery-min-interval", type=float, default=2.0, help="Seconds between discovery attempts.")
    parser.add_argument(
        "--periodic-rediscover-interval",
        type=float,
        default=25.0,
        help="Force a fresh discovery if this many seconds pass without one, even with no detected trigger (self-healing safety net - see on_sample).",
    )
    parser.add_argument(
        "--startup-delay",
        type=float,
        default=6.0,
        help="Seconds to wait before the first discovery attempt, so the QAM sidebar (always open when this process is started) can be closed first.",
    )
    parser.add_argument("--translate-url", default="http://192.168.1.32:8787/translate", help="Translation HTTP endpoint.")
    parser.add_argument("--target-lang", default="Japanese")
    parser.add_argument("--output", type=Path, help="Write priority-sorted block+translation JSON here after each update.")
    parser.add_argument(
        "--pause-flag",
        type=Path,
        help="If this path exists, skip all discovery/tracking/translation work each frame (see on_sample()). Created/removed by main.py's toggle_dynamic_pause().",
    )
    parser.add_argument(
        "--tap-request",
        type=Path,
        help="Tap-to-translate: main.py's request_tap_translate() writes {x,y} (capture-pixel coords) here. Only polled while paused - see on_sample()/handle_tap_request().",
    )
    parser.add_argument(
        "--tap-result",
        type=Path,
        help="Tap-to-translate: handle_tap_request() writes its {ok,matched,text,translation,bbox} response here for main.py to read back.",
    )
    parser.add_argument(
        "--overlay-transition-guard-s",
        type=float,
        default=2.2,
        help=(
            "Seconds to suppress diff-tracking on a block right after its displayed "
            "translation changes (see write_active_blocks()) - covers the frontend's "
            "own poll interval (1.5s in index.tsx) plus repaint margin, so "
            "PositionedOverlay showing/hiding/updating its own rendered text doesn't "
            "look like a real game-content change and re-trigger itself."
        ),
    )
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
