#!/usr/bin/env python3
"""Per-block pixel-diff tracking for dynamically discovered OCR text regions.

Replaces the single-fixed-ROI settle detector in capture.py's FrameCounter
for the "wide/full-frame OCR" pipeline. Modeled on playtranslate-android's
CaptureService: tracked text blocks are sampled independently from the rest
of the frame ("background"), so a block's own content change can be detected
and re-OCR'd without re-running full-frame OCR, while ambient motion outside
tracked blocks (particle effects, idle animation) doesn't spuriously trigger
a change on a block whose pixels didn't move.

Pure Python, no GStreamer/PIL dependency, so it can be driven directly from
saved frames in tests. Frames are raw BGRx/RGBx buffers (4 bytes/pixel),
matching capture.py's appsink caps.
"""
import time
from dataclasses import dataclass, field


def _sample_points(x0, y0, x1, y1, stride):
    points = []
    for y in range(y0, y1, stride):
        for x in range(x0, x1, stride):
            points.append((x, y))
    return points


def _pixel_at(raw, width, height, x, y):
    if x < 0 or y < 0 or x >= width or y >= height:
        return (0, 0, 0)
    offset = (y * width + x) * 4
    # BGRx order (matches capture.py's `video/x-raw,format=BGRx` appsink caps)
    b, g, r = raw[offset], raw[offset + 1], raw[offset + 2]
    return (r, g, b)


def _sample(raw, width, height, points):
    return [_pixel_at(raw, width, height, x, y) for (x, y) in points]


def _diff_ratio(a, b, pixel_diff_threshold):
    if not a or len(a) != len(b):
        return 0.0
    changed = 0
    for (ar, ag, ab), (br, bg, bb) in zip(a, b):
        if abs(ar - br) > pixel_diff_threshold or abs(ag - bg) > pixel_diff_threshold or abs(ab - bb) > pixel_diff_threshold:
            changed += 1
    return changed / len(a)


@dataclass
class TrackedBlock:
    block_id: int
    bbox: tuple  # (x0, y0, x1, y1) in full-frame pixel coords
    text: str
    conf: float
    sample_points: list = field(default_factory=list)
    baseline: list = field(default_factory=list)
    previous: list = field(default_factory=list)
    armed: bool = True
    pending_change: bool = False
    low_diff_streak: int = 0
    stale_streak: int = 0
    settle_timestamps: list = field(default_factory=list)


class MultiRegionTracker:
    """Tracks a set of discovered text-block regions plus a background sample.

    Call `set_regions()` after a discovery (full-frame OCR) pass, then
    `update()` once per captured raw frame. `update()` returns which blocks
    changed-and-settled (candidates for targeted re-OCR) and whether the
    background looks like it went through a sustained scene change (signal
    to drop all blocks and re-run discovery).
    """

    def __init__(
        self,
        width,
        height,
        block_sample_stride=4,
        background_sample_stride=16,
        pixel_diff_threshold=30,
        block_change_threshold=0.12,
        block_settle_threshold=0.03,
        block_settle_count=3,
        background_change_threshold=0.15,
        background_settle_threshold=0.03,
        background_settle_count=5,
        block_stale_limit=45,
        flaky_settle_count=3,
        flaky_window_s=10.0,
        background_pending_timeout_s=15.0,
    ):
        self.width = width
        self.height = height
        self.block_sample_stride = block_sample_stride
        self.background_sample_stride = background_sample_stride
        self.pixel_diff_threshold = pixel_diff_threshold
        self.block_change_threshold = block_change_threshold
        self.block_settle_threshold = block_settle_threshold
        self.block_settle_count = block_settle_count
        self.background_change_threshold = background_change_threshold
        self.background_settle_threshold = background_settle_threshold
        self.background_settle_count = background_settle_count
        self.block_stale_limit = block_stale_limit
        self.flaky_settle_count = flaky_settle_count
        self.flaky_window_s = flaky_window_s
        self.background_pending_timeout_s = background_pending_timeout_s

        self.blocks = {}  # block_id -> TrackedBlock
        self.background_points = []
        self.background_previous = None
        self.background_pending = False
        self.background_low_diff_streak = 0
        self.background_pending_since = None

    def has_regions(self):
        return len(self.blocks) > 0

    def set_regions(self, discovered_blocks, raw, width, height):
        """(Re)initialize tracking. `discovered_blocks` is the list returned by
        ocr_worker's /discover_blocks: [{id, text, conf, bbox: {x0,y0,x1,y1}}].
        `raw` is the frame the discovery OCR ran against, used to seed baselines.
        """
        self.width, self.height = width, height
        self.blocks = {}
        occupied = []
        for b in discovered_blocks:
            bbox = (b["bbox"]["x0"], b["bbox"]["y0"], b["bbox"]["x1"], b["bbox"]["y1"])
            occupied.append(bbox)
            points = _sample_points(*bbox, stride=self.block_sample_stride)
            baseline = _sample(raw, width, height, points)
            self.blocks[b["id"]] = TrackedBlock(
                block_id=b["id"],
                bbox=bbox,
                text=b["text"],
                conf=b["conf"],
                sample_points=points,
                baseline=baseline,
                previous=baseline,
            )

        bg_points = []
        for y in range(0, height, self.background_sample_stride):
            for x in range(0, width, self.background_sample_stride):
                if not any(x0 <= x < x1 and y0 <= y < y1 for (x0, y0, x1, y1) in occupied):
                    bg_points.append((x, y))
        self.background_points = bg_points
        self.background_previous = _sample(raw, width, height, bg_points)
        self.background_pending = False
        self.background_low_diff_streak = 0
        self.background_pending_since = None

    def clear(self):
        self.blocks = {}
        self.background_points = []
        self.background_previous = None

    def update(self, raw, width, height):
        """Feed one raw frame. Returns (changed_block_ids, stale_block_ids, scene_changed)."""
        changed_block_ids = []
        stale_block_ids = []

        for block in self.blocks.values():
            current = _sample(raw, width, height, block.sample_points)
            if block.armed:
                diff = _diff_ratio(block.baseline, current, self.pixel_diff_threshold)
                if diff >= self.block_change_threshold:
                    block.armed = False
                    block.pending_change = True
                    block.low_diff_streak = 0
            else:
                step_diff = _diff_ratio(block.previous, current, self.pixel_diff_threshold)
                if step_diff <= self.block_settle_threshold:
                    block.low_diff_streak += 1
                else:
                    block.low_diff_streak = 0
                if block.low_diff_streak >= self.block_settle_count:
                    now = time.monotonic()
                    block.settle_timestamps = [
                        t for t in block.settle_timestamps if now - t < self.flaky_window_s
                    ]
                    block.settle_timestamps.append(now)
                    if len(block.settle_timestamps) > self.flaky_settle_count:
                        # Settled too many times too fast to be real dialogue turnover -
                        # more likely a flickering/animated art region OCR misread as
                        # text. Drop it instead of treating it as a legitimate change.
                        stale_block_ids.append(block.block_id)
                    else:
                        changed_block_ids.append(block.block_id)
                        block.baseline = current
                    block.armed = True
                    block.pending_change = False
                    block.low_diff_streak = 0
                    block.stale_streak = 0
                else:
                    block.stale_streak += 1
                    if block.stale_streak >= self.block_stale_limit:
                        # Never settled (e.g. dialogue box closing/fading) -
                        # stop trusting this block's cached text/position.
                        stale_block_ids.append(block.block_id)
            block.previous = current

        scene_changed = False
        if self.background_points:
            current_bg = _sample(raw, width, height, self.background_points)
            if not self.background_pending:
                diff = _diff_ratio(self.background_previous, current_bg, self.pixel_diff_threshold)
                if diff >= self.background_change_threshold:
                    self.background_pending = True
                    self.background_low_diff_streak = 0
                    self.background_pending_since = time.monotonic()
            else:
                step_diff = _diff_ratio(self.background_previous, current_bg, self.pixel_diff_threshold)
                if step_diff <= self.background_settle_threshold:
                    self.background_low_diff_streak += 1
                else:
                    self.background_low_diff_streak = 0
                timed_out = (
                    self.background_pending_since is not None
                    and time.monotonic() - self.background_pending_since >= self.background_pending_timeout_s
                )
                if self.background_low_diff_streak >= self.background_settle_count or timed_out:
                    # A real, confirmed-stable scene change is the common
                    # case (settle streak completes). The timeout is a
                    # safety net for games whose background never truly
                    # holds still (e.g. constant particle/lighting motion
                    # observed on Enigma of Fear) - without it, a genuine
                    # scene change (confirmed live: closing the Decky QAM
                    # overlay) could be detected as "pending" and then never
                    # confirmed, leaving discovery permanently stuck on
                    # whatever was on screen when the block set was last
                    # (re)discovered.
                    scene_changed = True
                    self.background_pending = False
                    self.background_low_diff_streak = 0
                    self.background_pending_since = None
            self.background_previous = current_bg

        for block_id in stale_block_ids:
            del self.blocks[block_id]

        return changed_block_ids, stale_block_ids, scene_changed
