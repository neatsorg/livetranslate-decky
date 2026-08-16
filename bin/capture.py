#!/usr/bin/env python3
import argparse
import json
import shutil
import subprocess
import time
import sys
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst


DEFAULT_CONFIG = "config.json"


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def run_pw_dump():
    if not shutil.which("pw-dump"):
        raise RuntimeError("pw-dump not found. Install PipeWire tools or pass --target-object/--path.")

    result = subprocess.run(
        ["pw-dump"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return json.loads(result.stdout)


def find_gamescope_node():
    for obj in run_pw_dump():
        info = obj.get("info") or {}
        props = info.get("props") or {}
        if props.get("node.name") == "gamescope" and props.get("media.class") == "Video/Source":
            return {
                "id": obj.get("id"),
                "object_serial": props.get("object.serial"),
                "node_name": props.get("node.name"),
                "media_class": props.get("media.class"),
            }
    return None


def region_number(region, key, source_size, default):
    percent_key = f"{key}_pct"
    normalized_key = f"{key}_rel"
    if percent_key in region:
        return int(round(float(region[percent_key]) * source_size / 100.0))
    if normalized_key in region:
        return int(round(float(region[normalized_key]) * source_size))
    return int(region.get(key, default))


def rect_from_region(region, width, height):
    left = max(region_number(region, "x", width, 0), 0)
    top = max(region_number(region, "y", height, 0), 0)
    rect_width = region_number(region, "width", width, width - left)
    rect_height = region_number(region, "height", height, height - top)
    right = min(left + rect_width, width)
    bottom = min(top + rect_height, height)
    if left >= right or top >= bottom:
        return None
    return left, top, right, bottom


def crop_from_config(config):
    crop = dict(config.get("crop") or {})
    if crop:
        return {
            "left": int(crop.get("left", 0)),
            "right": int(crop.get("right", 0)),
            "top": int(crop.get("top", 0)),
            "bottom": int(crop.get("bottom", 0)),
        }

    roi = config.get("roi") or {}
    source = config.get("source") or {}
    try:
        source_width = int(source["width"])
        source_height = int(source["height"])
    except KeyError as exc:
        raise ValueError("config requires either crop margins or source + roi") from exc

    x = region_number(roi, "x", source_width, 0)
    y = region_number(roi, "y", source_height, 0)
    width = region_number(roi, "width", source_width, source_width - x)
    height = region_number(roi, "height", source_height, source_height - y)

    return {
        "left": x,
        "right": max(source_width - x - width, 0),
        "top": y,
        "bottom": max(source_height - y - height, 0),
    }


def diff_roi_from_config(config):
    roi = config.get("diff_roi")
    if not roi:
        return None
    return dict(roi)


def source_element(args, config):
    if args.path is not None:
        return f"pipewiresrc path={args.path} do-timestamp=true"

    target = args.target_object or config.get("target_object")
    if target is None and config.get("auto_detect", True):
        node = find_gamescope_node()
        if not node:
            raise RuntimeError("gamescope Video/Source node was not found in pw-dump.")
        target = node.get("object_serial") or node.get("node_name")
        print(f"Using PipeWire node: {node}", flush=True)

    if target is None:
        target = "gamescope"

    return f"pipewiresrc target-object={target} do-timestamp=true"


class FrameCounter:
    def __init__(
        self,
        report_interval,
        diff_enabled=False,
        diff_threshold=0.02,
        diff_sample_stride=256,
        diff_every=3,
        diff_cooldown_frames=30,
        diff_reset_threshold=0.01,
        diff_reset_count=6,
        save_changes=False,
        save_dir=None,
        save_prefix="crop",
        save_max=50,
        save_settled=False,
        save_latest_name="last_settled.png",
        diff_roi=None,
        save_roi=None,
    ):
        self.report_interval = report_interval
        self.diff_enabled = diff_enabled
        self.diff_threshold = diff_threshold
        self.diff_sample_stride = diff_sample_stride
        self.diff_every = diff_every
        self.diff_cooldown_frames = diff_cooldown_frames
        self.diff_reset_threshold = diff_reset_threshold
        self.diff_reset_count = diff_reset_count
        self.save_changes = save_changes
        self.save_dir = Path(save_dir) if save_dir else None
        self.save_prefix = save_prefix
        self.save_max = save_max
        self.save_settled = save_settled
        self.save_latest_name = save_latest_name
        self.diff_roi = diff_roi
        self.save_roi = save_roi
        self.start = time.monotonic()
        self.last_report = self.start
        self.frames = 0
        self.last_frames = 0
        self.change_events = 0
        self.suppressed_changes = 0
        self.last_change_ratio = 0.0
        self.next_change_frame = 0
        self.diff_armed = True
        self.low_diff_streak = 0
        self.prev_signature = None
        self.caps_printed = False
        self.saved_changes = 0
        self.save_limit_reported = False
        self.last_change_time = time.monotonic()
        self.pending_settled_save = False
        self.saved_settled = 0

        if self.save_changes or self.save_settled:
            if not self.save_dir:
                raise ValueError("--save-changes/--save-settled requires --save-dir")
            self.save_dir.mkdir(parents=True, exist_ok=True)

    def save_frame_png(self, sample, label="change", update_latest=False):
        if not (self.save_changes or self.save_settled):
            return

        total_saved = self.saved_changes + self.saved_settled
        if self.save_max > 0 and total_saved >= self.save_max:
            if not self.save_limit_reported:
                print(f"save limit reached max={self.save_max}", flush=True)
                self.save_limit_reported = True
            return

        try:
            gi.require_version("GdkPixbuf", "2.0")
            from gi.repository import GLib, GdkPixbuf
        except (ImportError, ValueError) as exc:
            print(f"save failed: GdkPixbuf is not available: {exc}", flush=True)
            return

        caps = sample.get_caps()
        structure = caps.get_structure(0)
        success_width, width = structure.get_int("width")
        success_height, height = structure.get_int("height")
        format_name = structure.get_string("format")
        if not success_width or not success_height:
            print("save failed: sample caps do not include width/height", flush=True)
            return
        if format_name not in ("BGRx", "BGRA", "RGBx", "RGBA"):
            print(f"save failed: unsupported format={format_name}", flush=True)
            return

        buffer = sample.get_buffer()
        success, map_info = buffer.map(Gst.MapFlags.READ)
        if not success:
            print("save failed: could not map frame buffer", flush=True)
            return

        try:
            raw = map_info.data
            expected_len = width * height * 4
            if len(raw) < expected_len:
                print(f"save failed: buffer too small size={len(raw)} expected={expected_len}", flush=True)
                return

            rgb_width = width
            rgb_height = height
            rgb = bytearray(width * height * 3)
            dst = 0
            for y in range(height):
                row = raw[y * width * 4 : (y + 1) * width * 4]
                if format_name.startswith("BGR"):
                    for x in range(0, width * 4, 4):
                        rgb[dst] = row[x + 2]
                        rgb[dst + 1] = row[x + 1]
                        rgb[dst + 2] = row[x]
                        dst += 3
                else:
                    for x in range(0, width * 4, 4):
                        rgb[dst] = row[x]
                        rgb[dst + 1] = row[x + 1]
                        rgb[dst + 2] = row[x + 2]
                        dst += 3

            if self.save_roi:
                rect = rect_from_region(self.save_roi, width, height)
                if not rect:
                    print(f"save failed: save_roi is outside frame bounds image={width}x{height}", flush=True)
                    return
                left, top, right, bottom = rect
                crop_width = right - left
                crop_height = bottom - top
                cropped = bytearray(crop_width * crop_height * 3)
                dst = 0
                for y in range(top, bottom):
                    row_start = (y * width + left) * 3
                    row_end = (y * width + right) * 3
                    row = rgb[row_start:row_end]
                    cropped[dst : dst + len(row)] = row
                    dst += len(row)
                rgb = cropped
                rgb_width = crop_width
                rgb_height = crop_height
        finally:
            buffer.unmap(map_info)

        if label == "settled":
            self.saved_settled += 1
            sequence = self.saved_settled
        else:
            self.saved_changes += 1
            sequence = self.saved_changes
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        filename = (
            f"{self.save_prefix}_{label}_{timestamp}"
            f"_frame{self.frames:06d}_event{self.change_events:04d}_{sequence:04d}.png"
        )
        path = self.save_dir / filename
        pixbuf = GdkPixbuf.Pixbuf.new_from_bytes(
            GLib.Bytes.new(bytes(rgb)),
            GdkPixbuf.Colorspace.RGB,
            False,
            8,
            rgb_width,
            rgb_height,
            rgb_width * 3,
        )
        pixbuf.savev(str(path), "png", [], [])
        print(f"saved image={path}", flush=True)

        if update_latest:
            latest_path = self.save_dir / self.save_latest_name
            pixbuf.savev(str(latest_path), "png", [], [])
            print(f"saved latest={latest_path}", flush=True)

    def diff_signature_from_frame(self, raw, width, height):
        if not self.diff_roi:
            return bytes(raw[:: self.diff_sample_stride])

        rect = rect_from_region(self.diff_roi, width, height)
        if not rect:
            return bytes(raw[:: self.diff_sample_stride])
        left, top, right, bottom = rect

        signature = bytearray()
        step_pixels = max(self.diff_sample_stride // 4, 1)
        for y in range(top, bottom):
            row_start = (y * width + left) * 4
            row_end = (y * width + right) * 4
            row = raw[row_start:row_end]
            for x in range(0, len(row), step_pixels * 4):
                if x + 2 < len(row):
                    signature.extend(row[x : x + 3])
        return bytes(signature)

    def update_diff(self, sample):
        if self.frames % self.diff_every != 0:
            return

        caps = sample.get_caps()
        structure = caps.get_structure(0)
        success_width, width = structure.get_int("width")
        success_height, height = structure.get_int("height")
        if not success_width or not success_height:
            return

        buffer = sample.get_buffer()
        success, map_info = buffer.map(Gst.MapFlags.READ)
        if not success:
            return

        try:
            signature = self.diff_signature_from_frame(map_info.data, width, height)
        finally:
            buffer.unmap(map_info)

        if self.prev_signature is None:
            self.prev_signature = signature
            return

        compare_len = min(len(signature), len(self.prev_signature))
        if compare_len == 0:
            self.prev_signature = signature
            return

        changed = sum(1 for a, b in zip(signature[:compare_len], self.prev_signature[:compare_len]) if a != b)
        self.last_change_ratio = changed / compare_len
        if self.last_change_ratio <= self.diff_reset_threshold:
            self.low_diff_streak += 1
            if self.low_diff_streak >= self.diff_reset_count:
                was_armed = self.diff_armed
                self.diff_armed = True
                if self.save_settled and self.pending_settled_save and not was_armed:
                    self.save_frame_png(sample, label="settled", update_latest=True)
                    self.pending_settled_save = False
        else:
            self.low_diff_streak = 0

        if self.diff_armed and self.last_change_ratio >= self.diff_threshold:
            if self.frames >= self.next_change_frame:
                self.change_events += 1
                self.diff_armed = False
                self.pending_settled_save = True
                self.low_diff_streak = 0
                self.next_change_frame = self.frames + self.diff_cooldown_frames
                self.last_change_time = time.monotonic()
                print(
                    f"change frame={self.frames} ratio={self.last_change_ratio:.4f} events={self.change_events}",
                    flush=True,
                )
                if self.save_changes:
                    self.save_frame_png(sample, label="change")
            else:
                self.suppressed_changes += 1
        elif not self.diff_armed and self.last_change_ratio >= self.diff_threshold:
            self.suppressed_changes += 1

        self.prev_signature = signature

    def seconds_since_last_change(self):
        return time.monotonic() - self.last_change_time

    def reset_diff_state(self):
        """Clear state that only makes sense against the pipeline we were
        just reading from, after a stall-watchdog pipeline rebuild. frames/
        change_events/etc. are left alone since they're just run totals.
        """
        self.prev_signature = None
        self.diff_armed = True
        self.low_diff_streak = 0
        self.pending_settled_save = False
        self.last_change_time = time.monotonic()

    def on_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.ERROR

        self.frames += 1
        if not self.caps_printed:
            caps = sample.get_caps()
            print(f"appsink caps: {caps.to_string()}", flush=True)
            self.caps_printed = True

        if self.diff_enabled:
            self.update_diff(sample)

        now = time.monotonic()
        if now - self.last_report >= self.report_interval:
            elapsed_total = now - self.start
            elapsed_delta = now - self.last_report
            delta_frames = self.frames - self.last_frames
            avg_fps = self.frames / elapsed_total if elapsed_total > 0 else 0.0
            inst_fps = delta_frames / elapsed_delta if elapsed_delta > 0 else 0.0
            line = f"frames={self.frames} fps={inst_fps:.1f} avg_fps={avg_fps:.1f}"
            if self.diff_enabled:
                line += (
                    f" change_ratio={self.last_change_ratio:.4f}"
                    f" change_events={self.change_events}"
                    f" suppressed={self.suppressed_changes}"
                    f" armed={int(self.diff_armed)}"
                    f" saved={self.saved_changes}"
                    f" settled={self.saved_settled}"
                )
            print(line, flush=True)
            self.last_report = now
            self.last_frames = self.frames

        return Gst.FlowReturn.OK


def build_pipeline(args, config):
    source = source_element(args, config)
    crop_stage = config.get("crop_stage", "pipeline")
    if crop_stage == "save":
        pipeline = (
            f"{source} ! "
            "videoconvert ! "
            "video/x-raw,format=BGRx ! "
            "appsink name=sink emit-signals=true sync=false max-buffers=2 drop=true"
        )
    else:
        crop = crop_from_config(config)
        pipeline = (
            f"{source} ! "
            f"videocrop left={crop['left']} right={crop['right']} top={crop['top']} bottom={crop['bottom']} ! "
            "videoconvert ! "
            "video/x-raw,format=BGRx ! "
            "appsink name=sink emit-signals=true sync=false max-buffers=2 drop=true"
        )
    return pipeline


def main():
    parser = argparse.ArgumentParser(description="Capture Gamescope PipeWire frames through GStreamer appsink.")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="Path to config JSON.")
    parser.add_argument("--target-object", help="PipeWire object.serial or node.name for pipewiresrc target-object.")
    parser.add_argument("--path", type=int, help="PipeWire node id for older pipewiresrc path= fallback.")
    parser.add_argument("--duration", type=float, help="Stop after N seconds.")
    parser.add_argument("--report-interval", type=float, default=1.0, help="FPS report interval in seconds.")
    parser.add_argument("--print-pipeline", action="store_true", help="Print the GStreamer pipeline before running.")
    parser.add_argument("--list-node", action="store_true", help="Print the detected gamescope node and exit.")
    parser.add_argument("--diff", action="store_true", help="Enable lightweight sampled frame-diff reporting.")
    parser.add_argument(
        "--diff-threshold",
        type=float,
        default=0.02,
        help="Report a change event when sampled byte change ratio reaches this value.",
    )
    parser.add_argument(
        "--diff-sample-stride",
        type=int,
        default=256,
        help="Sample every Nth byte for frame diff. Larger is lighter but less sensitive.",
    )
    parser.add_argument(
        "--diff-every",
        type=int,
        default=3,
        help="Run frame diff once every N frames. 3 means about 20Hz at 60fps.",
    )
    parser.add_argument(
        "--diff-cooldown-frames",
        type=int,
        default=30,
        help="Minimum frames between printed change events.",
    )
    parser.add_argument(
        "--diff-reset-threshold",
        type=float,
        default=0.01,
        help="Re-arm change reporting after diff ratio stays at or below this value.",
    )
    parser.add_argument(
        "--diff-reset-count",
        type=int,
        default=6,
        help="Number of low-diff checks required before re-arming change reporting.",
    )
    parser.add_argument("--save-changes", action="store_true", help="Save a PNG whenever a change event is reported.")
    parser.add_argument("--save-settled", action="store_true", help="Save a PNG after a detected change settles.")
    parser.add_argument("--save-dir", help="Directory for PNGs saved by --save-changes.")
    parser.add_argument("--save-prefix", default="crop", help="Filename prefix for saved PNGs.")
    parser.add_argument("--save-latest-name", default="last_settled.png", help="Filename for the latest settled PNG.")
    parser.add_argument(
        "--save-max",
        type=int,
        default=50,
        help="Maximum number of PNGs to save per run. Use 0 for no limit.",
    )
    parser.add_argument(
        "--stall-timeout-s",
        type=float,
        default=90.0,
        help=(
            "If --diff is on and no change event fires for this many seconds, "
            "rebuild the capture pipeline (this is how gamescope's PipeWire "
            "source has been observed to silently stop delivering fresh "
            "frames while frames/fps still look healthy). Use 0 to disable."
        ),
    )
    parser.add_argument(
        "--resume-gap-s",
        type=float,
        default=5.0,
        help=(
            "If wall-clock time jumps forward by at least this much between "
            "main-loop iterations (which normally poll every ~100ms), treat "
            "it as a system suspend/resume and rebuild the capture pipeline "
            "after --resume-grace-s. gamescope's GPU/PipeWire stack has been "
            "observed not to recover cleanly from suspend on its own. Use 0 "
            "to disable."
        ),
    )
    parser.add_argument(
        "--resume-grace-s",
        type=float,
        default=3.0,
        help="Delay before rebuilding the pipeline after a detected resume, to give gamescope time to stabilize first.",
    )
    args = parser.parse_args()

    if args.list_node:
        print(json.dumps(find_gamescope_node(), ensure_ascii=False, indent=2))
        return 0

    config = load_config(args.config)
    diff_roi = diff_roi_from_config(config)
    save_roi = dict(config.get("roi") or {}) if config.get("crop_stage") == "save" else None
    Gst.init(None)

    def start_pipeline(counter):
        pipeline_text = build_pipeline(args, config)
        if args.print_pipeline:
            print(pipeline_text, flush=True)
        pipeline = Gst.parse_launch(pipeline_text)
        sink = pipeline.get_by_name("sink")
        sink.connect("new-sample", counter.on_sample)
        pipeline.set_state(Gst.State.PLAYING)
        return pipeline, pipeline.get_bus()

    counter = FrameCounter(
        args.report_interval,
        diff_enabled=args.diff,
        diff_threshold=args.diff_threshold,
        diff_sample_stride=args.diff_sample_stride,
        diff_every=args.diff_every,
        diff_cooldown_frames=args.diff_cooldown_frames,
        diff_reset_threshold=args.diff_reset_threshold,
        diff_reset_count=args.diff_reset_count,
        save_changes=args.save_changes,
        save_dir=args.save_dir,
        save_prefix=args.save_prefix,
        save_max=args.save_max,
        save_settled=args.save_settled,
        save_latest_name=args.save_latest_name,
        diff_roi=diff_roi,
        save_roi=save_roi,
    )
    pipeline, bus = start_pipeline(counter)
    started = time.monotonic()
    stall_watchdog_enabled = args.diff and args.stall_timeout_s > 0
    resume_watchdog_enabled = args.resume_gap_s > 0
    last_wall_time = time.time()

    try:
        while True:
            msg = bus.timed_pop_filtered(
                100 * Gst.MSECOND,
                Gst.MessageType.ERROR | Gst.MessageType.EOS | Gst.MessageType.WARNING,
            )
            if msg:
                if msg.type == Gst.MessageType.ERROR:
                    err, debug = msg.parse_error()
                    print(f"GStreamer error: {err}", file=sys.stderr)
                    if debug:
                        print(debug, file=sys.stderr)
                    return 1
                if msg.type == Gst.MessageType.EOS:
                    return 0
                if msg.type == Gst.MessageType.WARNING:
                    warn, debug = msg.parse_warning()
                    print(f"GStreamer warning: {warn}", flush=True)
                    if debug:
                        print(debug, flush=True)

            now_wall = time.time()
            wall_gap = now_wall - last_wall_time
            last_wall_time = now_wall

            if resume_watchdog_enabled and wall_gap >= args.resume_gap_s:
                print(
                    f"resume watchdog: wall clock jumped {wall_gap:.0f}s between loop "
                    f"iterations (likely system suspend/resume) - rebuilding capture "
                    f"pipeline in {args.resume_grace_s:.0f}s",
                    flush=True,
                )
                time.sleep(args.resume_grace_s)
                pipeline.set_state(Gst.State.NULL)
                counter.reset_diff_state()
                pipeline, bus = start_pipeline(counter)
                last_wall_time = time.time()
            elif stall_watchdog_enabled and counter.seconds_since_last_change() >= args.stall_timeout_s:
                print(
                    f"stall watchdog: no change event for {args.stall_timeout_s:.0f}s "
                    f"(frames={counter.frames} still incrementing) - rebuilding capture pipeline",
                    flush=True,
                )
                pipeline.set_state(Gst.State.NULL)
                counter.reset_diff_state()
                pipeline, bus = start_pipeline(counter)

            if args.duration and time.monotonic() - started >= args.duration:
                return 0
    finally:
        pipeline.set_state(Gst.State.NULL)


if __name__ == "__main__":
    raise SystemExit(main())
