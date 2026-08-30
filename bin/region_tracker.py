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
import os
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


def _bbox_area(bbox):
    x0, y0, x1, y1 = bbox
    return max(1, (x1 - x0) * (y1 - y0))


def _bbox_overlap_ratio(a, b):
    """Intersection area relative to the smaller of the two boxes - a
    containment-style overlap check (matches ocr_worker._drop_contained_
    groups' "mostly inside" heuristic) rather than strict IoU, since OCR
    bbox edges can shift a few pixels between discovery passes for what's
    really the same on-screen text.
    """
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    iw, ih = max(0, ix1 - ix0), max(0, iy1 - iy0)
    inter = iw * ih
    if inter == 0:
        return 0.0
    return inter / min(_bbox_area(a), _bbox_area(b))


def _containment_ratio(inner, outer):
    """Fraction of `inner`'s area that sits inside `outer` - directional,
    unlike _bbox_overlap_ratio's symmetric min-area version. Used to tell
    "outer fully swallows inner" (a real expansion of inner) apart from
    "these two boxes happen to overlap a lot" (which _bbox_overlap_ratio
    alone can't distinguish, since inter/min(area) is the same regardless
    of which box is bigger).
    """
    ix0, iy0 = max(inner[0], outer[0]), max(inner[1], outer[1])
    ix1, iy1 = min(inner[2], outer[2]), min(inner[3], outer[3])
    iw, ih = max(0, ix1 - ix0), max(0, iy1 - iy0)
    return (iw * ih) / _bbox_area(inner)


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
    high_diff_streak: int = 0
    stale_streak: int = 0
    settle_timestamps: list = field(default_factory=list)
    suppress_until: float = 0.0
    created_at: float = field(default_factory=time.monotonic)


class MultiRegionTracker:
    """Tracks a set of discovered text-block regions plus a background sample.

    Call `set_regions()` after a discovery (full-frame OCR) pass, then
    `update()` once per captured raw frame. `update()` returns which blocks
    changed-and-settled (candidates for targeted re-OCR), which blocks just
    started changing but haven't settled yet (candidates for immediately
    hiding a now-stale displayed translation - see update()'s docstring),
    and whether the background looks like it went through a sustained scene
    change (signal to drop all blocks and re-run discovery).
    """

    def __init__(
        self,
        width,
        height,
        block_sample_stride=4,
        background_sample_stride=16,
        pixel_diff_threshold=30,
        block_change_threshold=0.12,
        block_trigger_confirm_count=2,
        block_settle_threshold=0.03,
        block_settle_count=3,
        background_change_threshold=0.15,
        background_settle_threshold=0.03,
        background_settle_count=5,
        block_stale_limit=45,
        flaky_settle_count=3,
        flaky_window_s=10.0,
        background_pending_timeout_s=15.0,
        max_block_age_s=None,
    ):
        self.width = width
        self.height = height
        self.block_sample_stride = block_sample_stride
        self.background_sample_stride = background_sample_stride
        self.pixel_diff_threshold = pixel_diff_threshold
        self.block_change_threshold = block_change_threshold
        self.block_trigger_confirm_count = block_trigger_confirm_count
        self.block_settle_threshold = block_settle_threshold
        self.block_settle_count = block_settle_count
        self.background_change_threshold = background_change_threshold
        self.background_settle_threshold = background_settle_threshold
        self.background_settle_count = background_settle_count
        self.block_stale_limit = block_stale_limit
        self.flaky_settle_count = flaky_settle_count
        self.flaky_window_s = flaky_window_s
        self.background_pending_timeout_s = background_pending_timeout_s
        # Opt-in only (None = disabled, matches every existing caller
        # including all of Linux's - see update()'s use of this for why).
        self.max_block_age_s = max_block_age_s

        self.blocks = {}  # block_id -> TrackedBlock
        self.background_points = []
        self.background_previous = None
        self.background_pending = False
        self.background_low_diff_streak = 0
        self.background_pending_since = None
        # Well above any raw OCR discover_blocks id (which restarts at 0
        # each call) so merge_regions()'s allocated ids never collide with
        # a later set_regions() call's ids.
        self._next_block_id = 100000

    def has_regions(self):
        return len(self.blocks) > 0

    def suppress(self, block_id, duration_s):
        """Freeze diff-tracking on one block for `duration_s` seconds.

        Call this whenever a caller is about to change what's rendered at
        this block's bbox by means other than the tracked game content
        itself (e.g. PlayTranslate's own overlay showing/hiding/updating
        text there). Without this, that self-caused pixel change is
        indistinguishable from a real content change and re-triggers
        tracking, which re-triggers the display, forever - the positioned-
        overlay feedback loop documented in PHASE_A_HANDOFF.md. While
        suppressed, the block continuously re-baselines to whatever's
        currently on screen instead of diffing against a stale baseline, so
        it resumes clean the moment the window ends regardless of how the
        overlay actually transitioned in the meantime.
        """
        block = self.blocks.get(block_id)
        if block is not None:
            block.suppress_until = time.monotonic() + duration_s

    def set_regions(self, discovered_blocks, raw, width, height):
        """(Re)initialize tracking, discarding anything previously tracked.
        `discovered_blocks` is the list returned by ocr_worker's
        /discover_blocks: [{id, text, conf, bbox: {x0,y0,x1,y1}}]. `raw` is
        the frame the discovery OCR ran against, used to seed baselines.

        Only appropriate when the old block set is *known* to be invalid -
        the very first discovery, or right after a confirmed scene_changed.
        A periodic/self-healing rediscovery that might simply have missed
        an already-tracked block on this particular OCR pass should use
        merge_regions() instead - see its docstring.
        """
        self.width, self.height = width, height
        self.blocks = {}
        self._next_block_id = 100000
        for b in discovered_blocks:
            bbox = (b["bbox"]["x0"], b["bbox"]["y0"], b["bbox"]["x1"], b["bbox"]["y1"])
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
        self._recompute_background(raw, width, height)

    def merge_regions(
        self,
        discovered_blocks,
        raw,
        width,
        height,
        overlap_threshold=0.5,
        expand_containment_ratio=0.9,
        expand_growth_ratio=1.2,
        force_refresh_ids=None,
    ):
        """Add newly discovered blocks that don't overlap anything already
        tracked; existing tracked blocks (baseline, armed state, current
        translation) are otherwise left untouched, with one exception (see
        "expand" below) plus an opt-in second exception (`force_refresh_ids`).

        `force_refresh_ids`: block ids the caller already knows have no
        usable translation right now (e.g. hidden by a pending-change event
        that then never settled - see PipelineLoop.tick()'s discovery branch
        on the Windows port). Without this, a block stuck `armed=False` /
        `pending_change=True` because its content genuinely never settles
        (e.g. continuous background animation under otherwise-static text)
        is invisible to plain overlap-matching: it isn't "expand"-eligible
        (same bbox, not bigger), so periodic rediscovery leaves it untouched
        forever per the policy above, and it only recovers once
        `block_stale_limit` finally times it out (~22s) and it gets dropped
        + freshly re-added. `force_refresh_ids` lets a caller that already
        knows a block's translation is missing skip that wait: same
        re-arm/reset treatment as "expand", added to `updated` so it gets
        retranslated on this pass. Unused by default (`None`) - existing
        callers (Linux's capture_dynamic.py) never pass this and see no
        behavior change.

        Use this for periodic/self-healing rediscovery instead of
        set_regions(). Confirmed live: full-frame OCR confidence is frame-
        flaky - the same static title-card text is found on some discovery
        passes and missed on others (PHASE_A_HANDOFF.md's positioned-
        overlay session; the specific miss that motivated this method is in
        project memory). A rediscovery pass that fails to redetect an
        already-tracked block must not blank it just because this
        particular OCR pass didn't happen to find it again - real content
        changes within an already-tracked block are the per-block pixel-
        diff tracker's job (see update()), not discovery's.

        "Expand" exception (added after a live NORCO session found the
        following): a block first discovered while its text was still
        partial (e.g. a typewriter-reveal mid-animation, or a message
        window mid-move) gets tracked with a too-small bbox covering only
        that partial text - and the pixel-diff tracker only ever samples
        *inside* that bbox, so it can sit there static and correct-looking
        forever even after the real on-screen text grows well past it.
        Plain overlap-skip (the rule above) made this permanent: any later,
        more-complete rediscovery of the same region always overlaps the
        stale small bbox enough to be treated as "already tracked" and
        silently dropped, so the block's bbox/text could never self-heal.
        Detected via _containment_ratio (directional - is the *old* bbox
        almost entirely inside the *new* one, not just "these overlap a
        lot", which alone can't tell an expansion apart from a fragment
        overlapping a correct block the other way around) plus a minimum
        growth ratio, so a same-extent rediscovery (jittered a few px) or a
        smaller/noisy fragment landing inside an already-correct block
        (the original protection this method exists for) still don't
        trigger anything. Restricted to exactly one matching existing
        block - if a new bbox would plausibly expand/absorb more than one
        already-tracked block at once, that's ambiguous enough (which one
        keeps the id? are they really the same message?) to leave for a
        future session and just skip, same as before.

        Returns (added, updated): added is [(block_id, text, conf), ...]
        for brand-new blocks; updated is [(block_id, text, conf), ...] for
        existing blocks whose bbox/text were just expanded/refined. Both
        need translating - the caller doesn't need to treat them
        differently beyond that.
        """
        added = []
        updated = []
        force_refresh_ids = force_refresh_ids or ()
        for b in discovered_blocks:
            bbox = (b["bbox"]["x0"], b["bbox"]["y0"], b["bbox"]["x1"], b["bbox"]["y1"])
            overlapping = [
                existing for existing in self.blocks.values() if _bbox_overlap_ratio(bbox, existing.bbox) >= overlap_threshold
            ]
            if not overlapping:
                block_id = self._next_block_id
                self._next_block_id += 1
                points = _sample_points(*bbox, stride=self.block_sample_stride)
                baseline = _sample(raw, width, height, points)
                self.blocks[block_id] = TrackedBlock(
                    block_id=block_id,
                    bbox=bbox,
                    text=b["text"],
                    conf=b["conf"],
                    sample_points=points,
                    baseline=baseline,
                    previous=baseline,
                )
                added.append((block_id, b["text"], b["conf"]))
                continue
            expandable = [
                existing
                for existing in overlapping
                if _containment_ratio(existing.bbox, bbox) >= expand_containment_ratio
                and _bbox_area(bbox) >= _bbox_area(existing.bbox) * expand_growth_ratio
            ]
            refreshable = [existing for existing in overlapping if existing.block_id in force_refresh_ids]
            if not expandable and len(refreshable) == 1:
                expandable = refreshable
            if len(expandable) == 1:
                existing = expandable[0]
                points = _sample_points(*bbox, stride=self.block_sample_stride)
                baseline = _sample(raw, width, height, points)
                existing.bbox = bbox
                existing.text = b["text"]
                existing.conf = b["conf"]
                existing.sample_points = points
                existing.baseline = baseline
                existing.previous = baseline
                existing.armed = True
                existing.pending_change = False
                existing.low_diff_streak = 0
                existing.high_diff_streak = 0
                existing.stale_streak = 0
                existing.settle_timestamps = []
                existing.created_at = time.monotonic()  # just re-confirmed by real discovery - reset the age clock
                existing.suppress_until = 0.0
                updated.append((existing.block_id, b["text"], b["conf"]))
        if added or updated:
            self._recompute_background(raw, width, height)
        return added, updated

    def _recompute_background(self, raw, width, height):
        occupied = [block.bbox for block in self.blocks.values()]
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
        self._next_block_id = 100000

    def update(self, raw, width, height):
        """Feed one raw frame. Returns (changed_block_ids, pending_block_ids,
        stale_block_ids, scene_changed).

        changed_block_ids: settled onto new content - safe to show.
        pending_block_ids: just started changing this tick, not settled yet -
          the block's last-shown translation is now stale/unreliable (e.g. a
          scrolling text box mid-scroll) and callers should hide/invalidate
          it immediately rather than leaving the old content on screen until
          the new content happens to settle.
        """
        changed_block_ids = []
        pending_block_ids = []
        stale_block_ids = []
        now_mono = time.monotonic()

        for block in self.blocks.values():
            current = _sample(raw, width, height, block.sample_points)
            if block.suppress_until and now_mono < block.suppress_until:
                # Own-render guard window (see suppress()) - track the
                # moving target silently, don't evaluate diffs this tick.
                if os.environ.get("PT_DEBUG_DIFF"):
                    print(
                        f"[diff-debug] block={block.block_id} suppressed ({block.suppress_until - now_mono:.2f}s left)",
                        flush=True,
                    )
                block.baseline = current
                block.previous = current
                block.high_diff_streak = 0
                block.low_diff_streak = 0
                continue
            block.suppress_until = 0.0
            if self.max_block_age_s is not None and now_mono - block.created_at >= self.max_block_age_s:
                # Confirmed live 2026-08-27 (real long NORCO session): an
                # *armed* block that never again crosses
                # block_change_threshold has no staleness path at all - the
                # block_stale_limit/stale_streak logic below only runs once
                # a block is already unarmed/pending, and a block whose own
                # sample points happen to sit on content that stays visually
                # static forever (even as the rest of the screen moves on to
                # a completely different scene) can just sit there holding
                # an old translation indefinitely. Also invisible to
                # scene_changed: _recompute_background() deliberately
                # excludes every tracked block's own bbox from the
                # background sample points, so a change happening *inside*
                # an already-tracked (and now-stale) block's own area can't
                # trigger scene_changed either - neither mechanism covers
                # this case. This age cap is a blunt but simple backstop:
                # drop it and let the next periodic discovery re-add it
                # fresh (correctly, if real text is still there) or leave it
                # gone (correctly, if it wasn't). Opt-in only
                # (max_block_age_s=None by default) - Linux's own call
                # sites don't pass this, so this is a no-op there unless
                # explicitly backported and tuned for that environment too.
                if os.environ.get("PT_DEBUG_DIFF"):
                    print(
                        f"[diff-debug] block={block.block_id} exceeded max_block_age_s "
                        f"({now_mono - block.created_at:.1f}s) - dropping",
                        flush=True,
                    )
                stale_block_ids.append(block.block_id)
                continue
            if block.armed:
                diff = _diff_ratio(block.baseline, current, self.pixel_diff_threshold)
                if os.environ.get("PT_DEBUG_DIFF"):
                    print(f"[diff-debug] block={block.block_id} diff={diff:.4f} text={block.text[:20]!r}", flush=True)
                if diff >= self.block_change_threshold:
                    block.high_diff_streak += 1
                else:
                    block.high_diff_streak = 0
                if block.high_diff_streak >= self.block_trigger_confirm_count:
                    # Require the threshold crossing on consecutive frames,
                    # not just once - confirmed live that small blocks (few
                    # sample points, so a handful of noisy pixels swings the
                    # ratio a lot) can cross block_change_threshold on a
                    # single-frame blip alone with no real content change
                    # (seen on a NORCO forum screen: freshly discovered,
                    # correctly-read post text going straight to '' a
                    # frame or two later with nothing on screen actually
                    # different). This also fixes pending_block_ids firing
                    # too eagerly and flickering the position-anchored
                    # overlay off on frames that were never a real change.
                    block.armed = False
                    block.pending_change = True
                    block.low_diff_streak = 0
                    block.high_diff_streak = 0
                    pending_block_ids.append(block.block_id)
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

        return changed_block_ids, pending_block_ids, stale_block_ids, scene_changed
