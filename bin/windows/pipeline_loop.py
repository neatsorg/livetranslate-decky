"""Continuous version of pipeline_test.py: capture -> region-tracked
discovery -> translate -> HUD, ticking on a QTimer instead of doing one
discovery pass and exiting.

Reuses region_tracker.py's MultiRegionTracker completely unchanged (it's
pure Python, no GStreamer/PIL dependency - see its own docstring) by
capturing frames in dxcam's native `output_color="BGRA"` mode instead of the
default "RGB": that's a straight passthrough of the raw DXGI buffer with no
cv2 color conversion, which happens to already match the flat BGRx byte
layout region_tracker._pixel_at() expects (same 4-bytes/pixel convention as
capture.py's GStreamer BGRx caps on Linux) - `frame.tobytes()` just works.

Deliberate simplification vs. the Linux DynamicCaptureRunner for this
increment: MultiRegionTracker.update()'s changed_block_ids are meant to
trigger a *targeted* re-OCR of just that block's crop (see its docstring -
"candidates for targeted re-OCR"). This script doesn't implement per-block
crop re-OCR yet; instead a changed/scene-changed signal just forces the next
tick's periodic discovery to run early. Correct, but less efficient than the
real per-block crop path - fine for proving the tracking loop shape, worth
tightening in a later pass.

Also runs OCR/translate synchronously on the Qt main thread (blocks ~1-2s
during a discovery tick, per pipeline_test.py's earlier timing) rather than
on a worker thread - acceptable for this prototype, a real concern before
this becomes the shipped implementation.

Usage: python pipeline_loop.py <model_resources_dir> [duration_s]
"""
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # for providers/, region_tracker
sys.path.insert(0, str(Path(__file__).resolve().parent))  # for windows_ocr_engine, settings_store

import settings_store

if sys.platform == "win32":
    from winlog import setup_stdio

    setup_stdio("pipeline_loop.log")

MIN_CONFIDENCE = 50.0
MIN_TEXT_LEN = 2
# Matches Linux's --periodic-rediscover-interval default exactly
# (capture_dynamic.py:1109, 25.0) - confirmed via a dedicated Linux-vs-
# Windows comparison 2026-08-27 that this is meant to be a rare, "self-
# healing catch-all, not a primary trigger" (that file's own comment),
# NOT the primary way new/changed content gets picked up - Linux's actual
# primary mechanism is calling tracker.update() (targeted per-block
# settle/re-OCR) on *every* captured frame, with full-frame discovery only
# triggered by force_discovery, an empty tracker, a directly-observed
# scene_changed, or this long safety-net interval. This constant was
# previously 2.0s, which made full-frame discovery run on a tight ~4-tick
# cycle *unconditionally* - every one of those discovery ticks skipped
# calling tracker.update() entirely (tick()'s should_discover branch is
# mutually exclusive with the update()-driven branch), starving the
# per-block settle-streak counters of the consecutive update() calls they
# need to actually confirm a block has stopped changing before trusting it.
# Confirmed as the real root cause of "typewriter/character-reveal text
# translates before it finishes" - not a settle-count tuning problem (an
# earlier fix that day raised WINDOWS_BLOCK_SETTLE_COUNT, which only
# affects *re*-settling of an already-tracked block and never even got a
# chance to run consistently under the old 2.0s cadence, since discovery
# kept interrupting it every ~2s via merge_regions()'s non-settle-gated
# "expand" self-heal path, which retranslates immediately on any bbox
# growth with zero wait). update() now gets to run on nearly every tick as
# intended, matching Linux's actual design.
DISCOVERY_MIN_INTERVAL_S = 25.0
TICK_MS = 500
# Consecutive ticks the "window found" / "window is foreground" checks must
# both fail before actually hiding the HUD - confirmed live 2026-08-27 that
# a single missed tick happens routinely during real gameplay (most likely
# GetWindowText()'s SendMessage call transiently blocking/timing out while
# the target game's own message queue is briefly busy, e.g. during a scene
# transition - a real watch-loop test with no HUD/capture/OCR load running
# alongside it stayed 100% stable for 15s straight, so this isn't the game
# window itself flickering) - without debouncing, each isolated miss called
# _hide_all_labels() for that one tick, then the very next successful tick's
# normal _sync_labels() call silently re-showed the exact same content -
# net effect was a visible on/off/on flicker on every transient miss, not a
# real disappearance. Matches this project's existing debounce pattern for
# the same class of problem (region_tracker.py's block_trigger_confirm_count).
WINDOW_MISS_CONFIRM_COUNT = 2
# How long a manual refresh's one-shot result stays on screen while still
# paused, before auto-hiding again (see trigger_refresh()/tick()) - loosely
# matches Linux's separate legacy tap-to-translate SubtitleHud's own
# auto-hide duration (src/index.tsx's showHud(), 6000ms default).
ONE_SHOT_DISPLAY_MS = 6000
# Matches capture_dynamic.py's DISCOVERY_TRANSLATE_MAX_WORKERS on the Linux
# side exactly - live-measured there against the same Ollama backend
# (RTX 2070 SUPER 8GB, translategemma Q4_K_M): 8 concurrent calls finished
# in ~4.3s vs ~8.4s serial, no errors/GPU OOM. Capped, not unbounded, so a
# pathological text-dense frame can't fire dozens of simultaneous requests.
TRANSLATE_MAX_WORKERS = 8
# region_tracker.py's default block_settle_count=3 was tuned on the Linux
# side against GStreamer's native per-frame capture cadence (on_sample()
# fires on every captured video frame, effectively tens of ms apart) - a
# block "settles" once pixel diffs stay quiet for 3 *consecutive samples*,
# which is a very short wall-clock window there. Windows instead polls on a
# fixed TICK_MS=500ms QTimer, so the same count=3 means "quiet for 1.5s"
# here - confirmed live 2026-08-27 that this is too short for some games'
# typewriter/character-reveal text effects: a natural dramatic pause
# between clauses (longer than 1.5s but well short of the message actually
# being done) gets misjudged as "finished," and the still-incomplete text
# on screen at that moment gets OCR'd and translated - exactly the
# "translation starts before the typewriter finishes" symptom reported.
# Overridden here (not in region_tracker.py's own default, which Linux's
# call sites still use unchanged) to require a longer quiet window before
# trusting a block has actually stopped changing - a direct implementation
# of the user's own suggested fix ("wait a bit longer after judged
# stopped"). 6 ticks = 3.0s, roughly double the original 1.5s - a starting
# point, not a precisely measured value; may need further tuning based on
# real play-testing against specific games' actual typewriter pacing.
WINDOWS_BLOCK_SETTLE_COUNT = 6
# See _watchdog_thread_main()'s docstring - well above any legitimate
# single-tick worst case (OCR_CALL_TIMEOUT_S + provider HTTP timeouts, even
# run concurrently), so this only fires on a genuine stall.
WATCHDOG_STALL_S = 60.0
# See region_tracker.py's use of max_block_age_s / created_at for the full
# mechanism - confirmed live 2026-08-27 on a real long NORCO session that an
# *armed* block whose own sample points never again cross
# block_change_threshold has literally no staleness path at all (that logic
# only runs once a block is unarmed/pending) and is also invisible to
# scene_changed (its own bbox is excluded from background sampling) - two
# blocks from the very start of the session (the title-card text) were
# still being displayed, unchanged, over completely unrelated later scenes.
# This backstop drops any block untouched by a real confirm/re-OCR for this
# long, regardless of armed state - generous enough that a long, slow-paced
# monologue/walking scene's caption shouldn't get cut off prematurely
# (created_at resets on every successful re-OCR or discovery match, so an
# *actively* re-confirmed block never ages out), short enough that a truly
# forgotten block doesn't linger for the rest of a play session.
WINDOWS_MAX_BLOCK_AGE_S = 60.0
# region_tracker.py's default background_settle_count=5 has the same
# platform-timing mismatch as block_settle_count (see that constant's
# comment) but in the *opposite* practical direction: Linux's near-
# continuous frame-driven sampling means 5 consecutive quiet samples is a
# very short real-world window (confirming a scene transition has actually
# finished, not just a single transient blip), so scene_changed fires
# promptly there. At Windows' TICK_MS=500ms polling rate, the same count=5
# means "5 consecutive clean 500ms samples with zero jitter above
# background_settle_threshold" - a full 2.5s of coarse sampling, and real-
# world capture/render jitter over that much wall-clock time makes hitting
# a clean streak that long harder to land reliably, compounding the delay
# further in practice. User reported 2026-08-27 (after the discovery-
# cadence fix above) that scene transitions still felt noticeably less
# responsive than Linux - even though the *initial* background_change_
# threshold trigger fires just as fast on both platforms (needs only one
# sample over threshold), the settle-confirm phase was structurally much
# slower here. Lowered to require only 2 consecutive quiet ticks (1.0s) -
# still real anti-noise protection (a single-tick blip alone can't trigger
# it), matching the same "require 2, not 1" philosophy region_tracker.py
# already uses for block_trigger_confirm_count. Unlike the discovery-
# cadence fix, this one is a reasoned starting guess, not something traced
# to a confirmed root cause via comparison - flagged as such, may need
# further tuning (the real risk of going too low: a visually busy/animated
# scene falsely triggering scene_changed and wiping still-valid tracked
# blocks mid-scene, which would look like a *new* kind of flicker).
WINDOWS_BACKGROUND_SETTLE_COUNT = 2
# See CaptureWorker's own docstring for the full mechanism this guards
# against - a real call normally takes a few ms, this is a safety ceiling.
CAPTURE_CALL_TIMEOUT_S = 5.0


class CaptureWorker:
    """Confines every dxcam/DXGI call (both the initial device creation and
    every grab()) to one dedicated background thread, for exactly the same
    reason WindowsOcrEngine (windows_ocr_engine.py) does the identical
    thing for WinRT.Media.Ocr - see that class's module docstring for the
    original discovery of this whole class of bug.

    Confirmed live 2026-08-28: the identical fingerprint recurred with
    dxcam instead of WinRT OCR - a completely separate, freshly-started
    process's `find_window_rect("norco")` succeeded 30/30 times against
    the exact same window, at the exact moment the real long-running app's
    *own* `find_window_rect()` call (on its Qt main thread, the same
    thread that was also calling `camera.grab()` directly) had been
    silently failing ("window not found") for a long stretch - the same
    "works fine from any other process/thread, permanently broken only on
    this specific thread of this specific process" shape already root-
    caused once for WinRT. The freeze immediately followed a `camera.grab()`
    call returning `None` (a real, if usually benign, DXGI "no new frame"
    case - but plausibly also how a genuine DXGI-level error, e.g. from
    the game briefly changing display mode, first surfaces) - consistent
    with dxcam's own DXGI/COM machinery on the main thread being the actual
    trigger, the same way WinRT's async COM machinery was for OCR. Moving
    dxcam's calls to their own dedicated thread, isolated from the thread
    that calls win32gui.EnumWindows()/GetForegroundWindow(), is the same
    proven fix applied to the next-most-likely source of the same
    mechanism - not yet independently confirmed the way the OCR fix was
    (that took a dedicated bisection script; this was inferred from the
    freeze's own shape under real play, not synthetically reproduced)."""

    def __init__(self, device_idx, output_idx):
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dxcam-capture")
        self._camera = None
        self.width = None
        self.height = None
        self._consecutive_timeouts = 0
        self._device_idx = device_idx
        self._output_idx = output_idx
        self._executor.submit(self._ensure_camera).result(timeout=CAPTURE_CALL_TIMEOUT_S)

    def _ensure_camera(self):
        """Runs on the dedicated worker thread only."""
        if self._camera is None:
            import dxcam

            self._camera = dxcam.create(
                device_idx=self._device_idx, output_idx=self._output_idx, output_color="BGRA"
            )
            self.width = self._camera.width
            self.height = self._camera.height

    def _grab_on_worker(self, region):
        """Runs on the dedicated worker thread only."""
        return self._camera.grab(region=region) if region is not None else self._camera.grab()

    def _restart_worker(self):
        print("[capture-worker] worker thread appears hung - abandoning it and starting a fresh one")
        # cancel_futures=True drops anything still queued (harmless - we're
        # abandoning this executor anyway); wait=False doesn't block on the
        # actual hung thread, which can't be force-killed regardless - this
        # just stops holding a reference to the old executor/queue instead
        # of leaving it to a GC pass. The hung worker thread itself still
        # leaks (Python can't force-kill a thread), unchanged from before.
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dxcam-capture")
        self._camera = None
        self._executor.submit(self._ensure_camera).result(timeout=CAPTURE_CALL_TIMEOUT_S)
        self._consecutive_timeouts = 0

    def grab(self, region=None):
        try:
            frame = self._executor.submit(self._grab_on_worker, region).result(timeout=CAPTURE_CALL_TIMEOUT_S)
            self._consecutive_timeouts = 0
            return frame
        except FutureTimeoutError:
            self._consecutive_timeouts += 1
            print(
                f"[capture-worker] grab() timed out after {CAPTURE_CALL_TIMEOUT_S}s "
                f"({self._consecutive_timeouts} consecutive) - treating as no frame this tick"
            )
            # Restart on the very first timeout, not the second - with
            # max_workers=1, if this call really is hung, the worker thread
            # is occupied for as long as that hang lasts, so any *second*
            # submission to the same executor just queues behind the first
            # and is guaranteed to also time out without ever actually
            # running - it confirms nothing a single timeout didn't already
            # establish, it just doubles the recovery latency for no
            # benefit. A single false-positive restart (a genuinely slow
            # but real capture call) costs one fresh dxcam device creation,
            # which is cheap - a real, correctly-diagnosed hang staying
            # unrecovered for an extra CAPTURE_CALL_TIMEOUT_S is worse.
            self._try_restart()
            return None
        except Exception as exc:
            # Not a timeout - confirmed real 2026-08-28 via code review:
            # if _restart_worker()'s own _ensure_camera() call previously
            # failed (e.g. dxcam.create() itself raised, not just hung),
            # self._executor ends up a fresh *working* executor but
            # self._camera stays None (see _restart_worker() - the
            # executor is swapped in before the possibly-failing
            # _ensure_camera() call). The *next* grab() then submits fine
            # (no timeout - the worker thread is healthy), but
            # _grab_on_worker() raises AttributeError on self._camera.grab
            # for a real (non-None) object - not a FutureTimeoutError, so
            # it fell through this method uncaught into tick()'s Qt timer
            # callback. Bounding *all* exceptions here, not just timeouts,
            # matches this file's own "never let a capture-layer failure
            # escape into tick()" intent - a general exception also
            # retries the restart, since a None self._camera is exactly
            # the case a restart is meant to fix.
            print(f"[capture-worker] grab() failed: {exc!r} - treating as no frame this tick")
            self._try_restart()
            return None

    def _try_restart(self):
        try:
            self._restart_worker()
        except Exception as exc:
            print(f"[capture-worker] worker restart failed: {exc!r}")


def normalize_text(text):
    """Same as capture_dynamic.py's normalize_text() on the Linux side -
    duplicated rather than imported since that module pulls in Linux-only
    (GStreamer/PipeWire) dependencies at import time."""
    return "\n".join(line.strip() for line in text.splitlines() if line.strip()).strip()


def is_useful_text(text):
    """Same as capture_dynamic.py's is_useful_text() - duplicated for the
    same import-boundary reason as normalize_text() above."""
    compact = "".join(ch for ch in text if ch.isalnum())
    return len(compact) >= 2


def looks_like_text(text, min_letters=2, min_letter_ratio=0.5):
    """Same as capture_dynamic.py's looks_like_text() (duplicated, same
    import-boundary reason). Confirmed on the Linux side to be the actual
    fix for garbage OCR from flickery/animated content ('wy' -> 'vr |',
    '4]' -> 'wy', etc.) - this is the check the Windows port was missing
    entirely until 2026-08-27, confirmed live as a real gap after the user
    reported flicker/garbage specifically on constantly-moving-background
    scenes and asked whether Linux's noise filtering carried over."""
    letters = sum(1 for ch in text if ch.isalpha())
    non_space = sum(1 for ch in text if not ch.isspace())
    if non_space == 0 or letters < min_letters:
        return False
    return (letters / non_space) >= min_letter_ratio


def is_probably_name_label(text):
    """Same as capture_dynamic.py's is_probably_name_label() (duplicated,
    same import-boundary reason) - a short, single-word, all-caps token
    with no sentence punctuation is more likely a speaker-name tag than a
    line of dialogue, and translating it through the same dialogue-oriented
    prompt as real text can misfire."""
    if len(text) > 15 or " " in text:
        return False
    if any(ch.islower() for ch in text):
        return False
    return not any(ch in ".!?…" for ch in text)


def _mask_regions(frame_bgra, regions):
    """Blank (black out) rectangles in a copy of the frame before handing it
    to OCR - never mutates the original, which the tracker still needs for
    real baseline/diff sampling. Same purpose as capture_dynamic.py's
    _mask_regions_bgrx() on Linux: the HUD's own rendered labels are on-
    screen pixels too, and dxcam captures them right along with the game -
    without this, discovery/re-OCR reads our own translated text back as if
    it were new source content (see the suppress() call in _sync_labels for
    the other half of this: suppress() stops an already-tracked block's
    pixel-diff baseline from firing on its own overlay; this stops fresh
    discovery from creating brand-new ghost blocks out of it)."""
    if not regions:
        return frame_bgra
    masked = frame_bgra.copy()
    h, w = masked.shape[:2]
    for (x0, y0, x1, y1) in regions:
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(w, x1), min(h, y1)
        if x1 > x0 and y1 > y0:
            masked[y0:y1, x0:x1] = 0
    return masked


def _to_bbox_dict(blocks):
    """WindowsOcrEngine.discover_blocks() returns bbox as a plain tuple;
    region_tracker's set_regions()/merge_regions() expect the
    ocr_worker.py /discover_blocks wire shape (bbox as a dict)."""
    out = []
    for b in blocks:
        x0, y0, x1, y1 = b["bbox"]
        out.append({"id": b["id"], "text": b["text"], "conf": b["conf"],
                     "bbox": {"x0": x0, "y0": y0, "x1": x1, "y1": y1}})
    return out


class PipelineLoop:
    def __init__(self):
        from region_tracker import MultiRegionTracker
        from translate_server import TranslationCache

        self.settings = settings_store.load()
        capture_cfg = self.settings["capture"]
        try:
            self.camera = CaptureWorker(
                device_idx=capture_cfg["device_idx"], output_idx=capture_cfg["output_idx"]
            )
        except Exception as exc:
            # Matches the provider/OCR-engine "never let __init__ itself
            # crash the whole app" pattern (_configure_from_settings()
            # below) - confirmed live 2026-08-26 for those two that raising
            # here took down the app before the tray icon (the only way to
            # reach Settings) had even been created. A bad device_idx/
            # output_idx from a stale settings.json, or dxcam.create()
            # itself failing/hanging, shouldn't be any different - tick()
            # already has an early "not configured" return for provider/
            # engine; the same self.camera is None check covers this too.
            print(f"[settings] capture device failed to initialize: {exc} - open Settings to fix it; pausing capture until then")
            self.camera = None
        self.window_offset = (0, 0)  # (x, y) of the captured region's origin in screen physical px
        self._window_missing_logged = False
        self._window_unfocused_logged = False
        self._window_miss_streak = 0  # consecutive ticks the window/focus check has failed - see tick()
        self._frame_none_streak = 0  # consecutive ticks camera.grab() has returned None - see tick()
        self._frame_none_logged = False

        # Reuses the exact same class the Linux side's translate_server.py
        # already has (an in-memory LRU over (backend, langs, speaker, text),
        # thread-safe) rather than reimplementing it - it's already pure
        # stdlib (OrderedDict + threading.Lock), no HTTP-server baggage to
        # drag in just by importing the class.
        self.translation_cache = TranslationCache()

        self.tracker_cls = MultiRegionTracker
        self.tracker = None

        self.translations = {}  # block_id -> translated text
        self.labels = {}  # block_id -> QLabel
        self.last_discovery = 0.0
        self.force_discovery = True
        self._last_rendered = {}  # block_id -> translation last shown, for suppress()
        # Starts paused, not running - a fresh install (or any launch
        # before the user has picked a target window in Settings) would
        # otherwise immediately start capturing/OCR-ing/translating the
        # *entire* desktop on a tight tick cadence the moment the process
        # starts, before the user has had any chance to configure
        # anything. User-requested 2026-08-28: the intended flow is now
        # launch -> tray icon appears (already paused) -> open Settings,
        # configure -> close Settings -> explicitly start capture from the
        # tray menu (see tray_app.py's capture-toggle action, which just
        # calls toggle_paused()) - or, before ever starting capture,
        # nothing has been discovered/translated yet at all, since tick()
        # does no work at all while paused.
        self.paused = True
        self.one_shot_while_paused = False  # see trigger_refresh()/tick()
        self.bypass_foreground_once = False  # see trigger_refresh()/tick()
        self._one_shot_hide_timer = None  # created lazily in tick(), see there and toggle_paused()
        self.last_tick_at = time.monotonic()  # updated at the top of every tick() call - see _watchdog_thread_main()
        self._tick_count = 0

        self._configure_from_settings()

    def _configure_from_settings(self):
        """(Re-)creates the provider + OCR engine and re-reads the capture
        target from self.settings - split out of __init__ so tray_app.py's
        "Settings" menu item can call this again after the dialog closes and
        have changes actually take effect, instead of requiring the whole
        app to be quit and relaunched. Camera device/output aren't exposed
        in the settings UI (dev-laptop-only concern, see project memory), so
        the DXCamera itself is deliberately not recreated here.

        Never raises. A fresh install's default provider needs an API key
        that hasn't been entered yet (or the user later switches to one and
        leaves it unconfigured, or an OCR engine fails to construct) -
        confirmed live 2026-08-26 that raising here used to take down the
        whole app *before* the tray icon (the only way to reach Settings)
        had even been created, with no way to recover short of hand-editing
        settings.json. Instead, self.provider/self.engine end up None and
        tick() skips all work until Settings is fixed and this runs again.

        Everything new is built into local variables first and only
        committed to self.* at the very end, once nothing above raised -
        confirmed live this matters: reload_settings() replaces self.settings
        before calling this, so a construction failure partway through used
        to be able to leave e.g. self.ocr_engine_name already updated to
        "windows_ocr" while self.engine was still the old ScreenAIEngine
        instance, which would raise AttributeError on the very next tick's
        self.engine.discover_blocks() call - a half-applied, mismatched
        state rather than a clean old-vs-new split."""
        from providers import create_provider

        provider_name = self.settings["provider"]
        provider_cfg = self.settings["provider_config"].get(provider_name, {})
        provider = None
        try:
            provider = create_provider(provider_name, **provider_cfg)
            if not provider.is_available():
                print(
                    f"[settings] provider {provider_name!r} is not configured (missing API key?) - "
                    f"open Settings to fix it; pausing capture until then"
                )
                provider = None
        except Exception as exc:
            # create_provider() raises ValueError for an unrecognized name,
            # and a malformed provider_cfg (e.g. from a hand-edited or
            # stale settings.json) can raise TypeError out of the provider
            # class's own __init__ - either way this must not be allowed to
            # propagate out of this method, for the same reason the OCR
            # engine's construction below is already guarded.
            print(
                f"[settings] provider {provider_name!r} failed to initialize: {exc} - "
                f"open Settings to fix it; pausing capture until then"
            )

        ocr_engine_name = self.settings.get("ocr_engine", "windows_ocr")
        engine = None
        try:
            from windows_ocr_engine import WindowsOcrEngine

            engine = WindowsOcrEngine(self.settings.get("windows_ocr_language", "en-US"))
        except Exception as exc:
            print(
                f"[settings] OCR engine {ocr_engine_name!r} failed to initialize: {exc} - "
                f"open Settings to fix it; pausing capture until then"
            )

        capture_cfg = self.settings["capture"]
        window_title = capture_cfg.get("window_title", "").strip()
        fixed_roi = capture_cfg.get("fixed_roi")  # None = discovery mode, dict = single fixed region

        self.provider = provider
        self.ocr_engine_name = ocr_engine_name
        self.engine = engine
        self.window_title = window_title
        self.fixed_roi = fixed_roi

    def reload_settings(self):
        """Called by tray_app.py after the Settings dialog is accepted.
        target_lang/source_lang need no extra work here since _translate()
        already reads them fresh from self.settings on every call - only the
        provider/OCR-engine/capture-target need explicit re-creation.
        Whatever was being tracked is very possibly no longer valid once the
        capture target or OCR engine changes, so this clears it and forces a
        fresh discovery pass next tick, same as a manual refresh."""
        self.settings = settings_store.load()
        self._configure_from_settings()
        if self.tracker is not None:
            self.tracker.clear()
        self.translations.clear()
        self.force_discovery = True
        self._window_missing_logged = False
        self._window_unfocused_logged = False
        provider_name = self.provider.name if self.provider is not None else "<unconfigured>"
        print(
            f"[settings] reloaded: provider={provider_name} ocr_engine={self.ocr_engine_name} "
            f"window_title={self.window_title!r} fixed_roi={self.fixed_roi is not None}"
        )

    def toggle_paused(self):
        self.paused = not self.paused
        if self.paused:
            # Matches Linux: main.py clears active_blocks.json the instant
            # the pause flag is set, so the HUD disappears immediately
            # rather than freezing in place. Confirmed via a dedicated
            # Linux-vs-Windows comparison 2026-08-27 that Windows never had
            # this - pausing here previously just stopped tick() from doing
            # further work while leaving whatever was already on screen
            # untouched, which is a real, if minor, behavioral divergence
            # (and plausibly why a pause press could look like "nothing
            # happened" even though self.paused really did flip).
            self._hide_all_labels()
        else:
            # Resuming should immediately reflect whatever's actually on
            # screen right now, not wait for the next periodic discovery
            # interval or trust however-stale the tracked region set
            # already was from before pausing - confirmed live 2026-08-26
            # that resuming otherwise appeared to do nothing at all.
            self.force_discovery = True
            # A pending one-shot auto-hide (from a refresh-while-paused
            # just before this resume - see trigger_refresh()/tick()) would
            # otherwise still fire a few seconds later and wipe out the
            # display that resuming just correctly brought back, since it
            # unconditionally calls _hide_all_labels() with no awareness
            # that we're no longer paused/one-shot by the time it goes off.
            # Confirmed live 2026-08-26: this exact race is why "resume"
            # appeared to work for a moment and then still end up showing
            # nothing - my own verification of the resume fix didn't wait
            # long enough afterward to notice the stale timer wiping it
            # back out again.
            if self._one_shot_hide_timer is not None:
                self._one_shot_hide_timer.stop()
        print(f"[keybinding] paused={self.paused}")

    def trigger_refresh(self):
        # Matches Linux's fixed-roi/discovery-reset behavior for a manual
        # refresh: drop everything tracked and force a full rediscovery on
        # the next tick, rather than trying to patch the existing block set.
        print("[keybinding] refresh requested - clearing tracker, forcing discovery next tick")
        if self.tracker is not None:
            self.tracker.clear()
        self.translations.clear()
        self.force_discovery = True
        # Manual refresh must always actually do something, regardless of
        # automatic display-gating state - matches Linux, which has no
        # foreground-focus concept at all and so never had this problem.
        # Confirmed live 2026-08-27 that without this, a refresh pressed at
        # a moment when _is_target_window_foreground() reads False (the
        # game visually filling the screen doesn't guarantee Windows' own
        # GetForegroundWindow() agrees, especially right after a gamepad
        # button press) set force_discovery=True as usual, but every
        # subsequent tick() kept returning early from the foreground check
        # *before* ever reaching the discovery code that would have acted
        # on it - the request just sat queued, silently doing nothing until
        # focus happened to naturally return on its own. This flag lets
        # tick() bypass that one specific check for exactly one pass
        # (still respects the window *not found* case - there's no valid
        # region to capture at all if the window doesn't exist).
        self.bypass_foreground_once = True
        # Immediately hide whatever's currently drawn, not just clear the
        # backend state - tick() is the only place that reconciles labels
        # against self.translations (via _sync_labels()), and while paused
        # tick() doesn't run at all, so without this the on-screen labels
        # and the now-empty backend state would silently diverge (stale
        # text staying visible even though nothing tracks it anymore).
        self._hide_all_labels()
        if self.paused:
            # A manual refresh while paused is a deliberate "just show me
            # what's on screen right now, once" request, not a request to
            # resume continuous tracking - confirmed live 2026-08-26 that
            # without this, tick()'s very first line (paused -> return)
            # silently discarded the request entirely *after* the
            # confirmation toast had already fired, producing a confusing
            # "says Refreshed, shows nothing" result. Setting this lets
            # tick() run through exactly one full pass despite still being
            # paused; tick() clears it again immediately (single-use) and
            # schedules an auto-hide afterward via ONE_SHOT_DISPLAY_MS,
            # since nothing is tracking whether this stays accurate while
            # still paused - loosely mirrors Linux's separate legacy tap-
            # to-translate SubtitleHud's auto-hide (6000ms), a deliberately
            # different, single-shot-flavored display mode from Dynamic
            # Capture's normal continuously-reverified persistence.
            self.one_shot_while_paused = True

    def _is_target_window_foreground(self):
        """True if the currently-focused top-level window's title matches
        self.window_title - see tick()'s call site for why this matters.
        Deliberately re-checks GetForegroundWindow() directly rather than
        comparing hwnds against find_window_rect()'s result, so this stays a
        single self-contained check with no signature change needed on
        find_window_rect() (also used by roi_crop_dialog.py)."""
        import win32gui

        try:
            hwnd = win32gui.GetForegroundWindow()
            if not hwnd:
                return False
            title = win32gui.GetWindowText(hwnd)
        except Exception:
            return False
        return self.window_title.lower() in title.lower()

    def _hide_all_labels(self):
        for label in self.labels.values():
            label.hide()

    def _ocr_blocks(self, frame_bgra):
        """OCR one BGRA frame (full or cropped) via WindowsOcrEngine. Returns
        [{"id","text","conf","bbox":(x0,y0,x1,y1)}, ...]."""
        t0 = time.monotonic()
        h, w = frame_bgra.shape[0], frame_bgra.shape[1]
        blocks = self.engine.discover_blocks(frame_bgra.tobytes(), w, h)
        print(f"[timing] ocr ({self.ocr_engine_name}, {frame_bgra.shape[1]}x{frame_bgra.shape[0]}) took {time.monotonic() - t0:.3f}s")
        return blocks

    def _run_discovery(self, frame_bgra):
        return self._ocr_blocks(frame_bgra)

    def _reocr_crop(self, frame_bgra, bbox):
        """Targeted re-OCR of just one tracked block's region, instead of a
        full-frame discovery pass - this is what MultiRegionTracker.update()'s
        changed_block_ids is actually meant to drive (see its docstring).
        Much cheaper than full discovery too, since the crop is tiny."""
        x0, y0, x1, y1 = bbox
        pad = 4
        x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
        x1, y1 = min(frame_bgra.shape[1], x1 + pad), min(frame_bgra.shape[0], y1 + pad)
        crop = frame_bgra[y0:y1, x0:x1]
        if crop.size == 0:
            return ""
        blocks = self._ocr_blocks(crop)
        return " ".join(b["text"] for b in blocks).strip()

    def _own_overlay_regions_physical(self, dpr):
        """Current HUD label rects, converted from Qt's logical (DPI-scaled)
        *screen* coordinates back into physical pixels *relative to the
        captured frame* - the frame is cropped to the game window when
        window_title is set, so labels (placed in absolute screen space,
        since the HUD window itself still spans the whole monitor) need the
        window's screen offset subtracted back out to line up with it.

        Only *currently visible* labels - confirmed live 2026-08-27 that
        `_hide_all_labels()` only calls `.hide()`, leaving each QLabel's
        stale last geometry sitting in `self.labels` (that dict is never
        cleared, only individual entries are replaced on the next sync) -
        this method used to mask out *every* label's rect regardless of
        visibility, so the very next OCR call after a manual refresh
        (which hides everything, then immediately forces a fresh discovery
        pass) still blanked out exactly the areas where the *previous*
        translations used to render, before that OCR ever ran. The user
        correctly noticed this as "refresh only finds newly-appeared text,
        ignoring what was already shown" - real game content sitting under
        an already-hidden label was invisible to OCR until something
        eventually moved those exact pixels enough to shift a label's
        geometry elsewhere. Masking should only ever cover pixels our own
        HUD is *actually currently drawing over* - a hidden label draws
        nothing, so it has nothing to protect OCR from misreading."""
        ox, oy = self.window_offset
        regions = []
        for label in self.labels.values():
            if not label.isVisible():
                continue
            geo = label.geometry()
            x0 = int(geo.x() * dpr) - ox
            y0 = int(geo.y() * dpr) - oy
            x1 = int((geo.x() + geo.width()) * dpr) - ox
            y1 = int((geo.y() + geo.height()) * dpr) - oy
            regions.append((x0, y0, x1, y1))
        return regions

    def _translate(self, text):
        """The one chokepoint every translation call funnels through
        (discovery, changed-block re-OCR, and fixed-ROI seeding all call
        this) - matches capture_dynamic.py's translate_block() on the Linux
        side, including its noise-filter gate. Ported 2026-08-27 after the
        user asked whether Linux's noise filtering survived the port -
        confirmed live it hadn't: MIN_TEXT_LEN/MIN_CONFIDENCE alone don't
        catch letter-ratio garbage ('wy'/'vr |'-style OCR misreads) or
        speaker-name labels, which Linux's own comments document as the
        dominant real-world source of visible junk on flickery/animated
        content - exactly what the user reported."""
        from providers import ProviderError

        cleaned = normalize_text(text)
        if not is_useful_text(cleaned) or not looks_like_text(cleaned) or is_probably_name_label(cleaned):
            return ""
        text = cleaned

        target_lang = self.settings["target_lang"]
        source_lang = self.settings["source_lang"]
        cache_key = (self.provider.name, source_lang, target_lang, "", text)
        cached = self.translation_cache.get(cache_key)
        if cached is not None:
            print(f"[translate] cache hit for {text[:30]!r}")
            return cached

        t0 = time.monotonic()
        try:
            result = self.provider.translate(
                speaker=None, text=text,
                target_lang=target_lang, source_lang=source_lang,
                profile=None, context_text="",
            )
            translation = str(result or "").strip()
            print(f"[timing] translate() took {time.monotonic() - t0:.3f}s")
            self.translation_cache.put(cache_key, translation)
            return translation
        except ProviderError as exc:
            # A real network API can fail transiently (rate limit, timeout,
            # bad key) - unlike DummyProvider, which never raised. Don't let
            # one failed call take down the whole tick/HUD.
            print(f"[translate] failed: {type(exc).__name__}: {exc}")
            return ""

    def _translate_many(self, id_text_pairs):
        """Translate several blocks concurrently instead of one HTTP round-
        trip at a time - measured live 2026-08-26 that sequential calls
        (~0.15-0.22s each against Google Cloud Translate) were erasing most
        of the win from switching to the much-faster Windows OCR engine, 2
        blocks alone costing about as much wall-clock time as the OCR step
        itself.

        Mirrors capture_dynamic.py's discovery-path translate pool on the
        Linux side exactly (same TRANSLATE_MAX_WORKERS=8 cap, same reasoning)
        rather than inventing a separate scheme - that pool already covers
        Ollama too, live-measured safe at this concurrency against a real
        backend (see TRANSLATE_MAX_WORKERS' comment). An earlier version of
        this change put a self-throttling semaphore inside OllamaProvider
        itself, on the mistaken assumption that Linux serialized every
        translate call - it doesn't (only capture_dynamic.py's single-block
        re-OCR/tap-adhoc paths call translate_block() one at a time; the
        *batch* discovery path this mirrors was already parallelized and
        tuned). That provider-side lock was reverted since the real Linux
        design puts the concurrency limit at this orchestration layer, not
        inside the provider."""
        if not id_text_pairs:
            return
        from concurrent.futures import ThreadPoolExecutor

        t0 = time.monotonic()
        with ThreadPoolExecutor(max_workers=min(len(id_text_pairs), TRANSLATE_MAX_WORKERS)) as pool:
            results = pool.map(lambda pair: (pair[0], self._translate(pair[1])), id_text_pairs)
            for block_id, translation in results:
                self.translations[block_id] = translation
        print(f"[timing] _translate_many({len(id_text_pairs)} blocks) wall time {time.monotonic() - t0:.3f}s")

    def tick(self, win, dpr):
        # Updated first, unconditionally, before any early-return path below
        # - lets _watchdog_thread_main() (and anyone reading the log after
        # the fact) tell "tick() is still being invoked by Qt's timer, just
        # silently doing nothing interesting" apart from "Qt's own timer
        # dispatch has actually stopped firing tick() at all". A real
        # 2026-08-27 investigation initially suspected the latter (a live
        # py-spy dump showed the Qt main thread idle), but that turned out
        # to be a false alarm - the game window had simply lost foreground
        # focus, which ticks through silently by design (see the "not
        # foreground" branch below, which used to print nothing at all -
        # now fixed to log once per streak, same as "not found") - tick()
        # was running normally the whole time. Kept anyway: real value as a
        # log-only survival signal if a genuine stall ever does happen.
        self.last_tick_at = time.monotonic()
        self._tick_count += 1
        # Consumed exactly once per call regardless of what happens below -
        # see trigger_refresh()'s docstring comment for why this exists.
        one_shot = self.one_shot_while_paused
        self.one_shot_while_paused = False
        bypass_foreground = self.bypass_foreground_once
        self.bypass_foreground_once = False
        if self.paused and not one_shot:
            # Matches capture_dynamic.py's pause behavior: do no discovery/
            # tracking/translation/HUD-update work at all, leaving whatever
            # was last displayed on screen exactly as it was - unless a
            # manual refresh explicitly asked for exactly one pass anyway.
            return
        if self.provider is None or self.engine is None or self.camera is None:
            # Not configured yet (see _configure_from_settings()'s
            # docstring) - nothing useful to do until Settings is fixed and
            # reload_settings() runs again with a working provider/engine.
            # self.camera can be None too if CaptureWorker's own dxcam
            # device init failed/timed out (bad device_idx/output_idx from
            # a stale settings.json, or a genuine capture-device error) -
            # same "leave the tray icon alive so Settings is still
            # reachable" reasoning as provider/engine, see __init__.
            return
        if self.window_title:
            from window_finder import find_window_rect

            try:
                rect = find_window_rect(self.window_title)
            except Exception as exc:
                # EnumWindows occasionally raises a transient pywintypes
                # error if a window is being created/destroyed mid-
                # enumeration (confirmed live, different error codes on
                # different ticks) - treat like "not found this tick"
                # rather than letting it propagate out of the QTimer
                # callback and spam a traceback every 500ms.
                print(f"[capture] find_window_rect transient error: {exc!r}")
                rect = None
            if rect is not None:
                # A window's client rect can extend a few px past the
                # monitor edge (title bar/border decorations pushing the
                # overall window slightly off-screen even when its content
                # looks fully on-screen) - confirmed live: NORCO reported
                # (6, 38, 1926, 1118) against a 1920x1080 output, which
                # dxcam's region validation rejects outright. Clip to what's
                # actually capturable.
                rect = (
                    max(0, rect[0]), max(0, rect[1]),
                    min(self.camera.width, rect[2]), min(self.camera.height, rect[3]),
                )
            if rect is None or rect[2] <= rect[0] or rect[3] <= rect[1]:
                self._window_miss_streak += 1
                if self._window_miss_streak >= WINDOW_MISS_CONFIRM_COUNT:
                    if not self._window_missing_logged:
                        print(f"[capture] window matching {self.window_title!r} not found (or minimized) - waiting")
                        self._window_missing_logged = True
                    self._hide_all_labels()
                # Below the confirm count: a single missed tick, most likely
                # transient (see WINDOW_MISS_CONFIRM_COUNT's comment) - skip
                # this tick's capture but leave whatever's currently on
                # screen exactly as it is, don't hide on a first miss alone.
                return
            # Confirmed live 2026-08-26 (real hands-on testing, not just dev
            # SSH testing): translations kept showing on screen even when
            # the target game wasn't the window actually in focus - the HUD
            # is a single fullscreen overlay spanning the whole desktop, so
            # labels positioned for the game's *last known* screen rect just
            # sit there visually on top of whatever window the user has
            # actually switched to, looking like "translations for the
            # wrong window" even though the underlying tracking is still
            # technically correct for the (currently backgrounded) game.
            # Linux's Dynamic Capture mode never had to handle this - the
            # game *is* the compositor's primary surface there, there's no
            # "another window is now on top" concept the way a normal
            # multi-window desktop has - so this is a genuinely new
            # Windows-specific requirement, not a porting gap.
            if not self._is_target_window_foreground() and not bypass_foreground:
                self._window_miss_streak += 1
                if self._window_miss_streak >= WINDOW_MISS_CONFIRM_COUNT:
                    if not self._window_unfocused_logged:
                        # Confirmed live 2026-08-27: this branch previously
                        # printed nothing at all, ever - a long real stretch
                        # of "game window found but not focused" (which
                        # ticks through silently by design, see the comment
                        # above) was indistinguishable in the log from a
                        # genuine freeze, and led to real debugging time
                        # lost chasing a Qt-hang theory that turned out to
                        # be wrong. One-time log per streak, same pattern
                        # as the "window not found" case just above.
                        print(
                            f"[capture] window matching {self.window_title!r} found but not "
                            f"foreground - waiting (tick #{self._tick_count})"
                        )
                        self._window_unfocused_logged = True
                    self._hide_all_labels()
                return
            self._window_miss_streak = 0
            self._window_missing_logged = False
            self._window_unfocused_logged = False
            self.window_offset = (rect[0], rect[1])
            frame = self.camera.grab(region=rect)
        else:
            self.window_offset = (0, 0)
            frame = self.camera.grab()
        if frame is None:
            # Confirmed live 2026-08-28 as a real silent-path gap, same
            # class as the "window not found"/"not foreground" ones found
            # the day before: dxcam's grab() can legitimately return None
            # (DXGI Desktop Duplication has no new frame to hand over if
            # the screen genuinely hasn't changed - a real, not-buggy case)
            # - but this branch previously had zero logging, so a long
            # stretch of consecutive None returns (whatever the cause) was
            # indistinguishable from a genuine hang: self.last_tick_at
            # still updates every cycle (set unconditionally at the very
            # top of tick(), before this point), so neither the 60s hard
            # watchdog nor the tick-timer self-heal check ever have reason
            # to fire - tick() really is running on schedule, just silently
            # producing nothing, every single time, for as long as this
            # condition persists. Logged once per streak (same guarded
            # pattern as the other two silent paths) so a future
            # occurrence is immediately diagnosable instead of looking
            # identical to a real freeze in the log.
            self._frame_none_streak += 1
            if not self._frame_none_logged:
                print(
                    f"[capture] camera.grab() returned None (streak={self._frame_none_streak}, "
                    f"tick #{self._tick_count}) - no new frame available this tick"
                )
                self._frame_none_logged = True
            return
        if self._frame_none_streak:
            if self._frame_none_streak > 1:
                print(f"[capture] camera.grab() recovered after {self._frame_none_streak} consecutive None returns")
            self._frame_none_streak = 0
            self._frame_none_logged = False
        h, w = frame.shape[0], frame.shape[1]
        raw = frame.tobytes()

        if self.tracker is None:
            self.tracker = self.tracker_cls(
                w, h,
                block_settle_count=WINDOWS_BLOCK_SETTLE_COUNT,
                max_block_age_s=WINDOWS_MAX_BLOCK_AGE_S,
                background_settle_count=WINDOWS_BACKGROUND_SETTLE_COUNT,
            )

        mask_regions = self._own_overlay_regions_physical(dpr)
        masked_frame = _mask_regions(frame, mask_regions)

        now = time.monotonic()
        if self.fixed_roi:
            # No full-frame OCR in this mode - the region is given, not
            # discovered, so re-running it every DISCOVERY_MIN_INTERVAL_S
            # like normal discovery would just be redundant work. Only
            # (re-)seed when nothing is tracked yet - matches
            # capture_dynamic.py's maybe_discover() fixed-roi branch.
            should_discover = self.force_discovery or not self.tracker.has_regions()
        else:
            should_discover = (
                self.force_discovery
                or not self.tracker.has_regions()
                or (now - self.last_discovery) >= DISCOVERY_MIN_INTERVAL_S
            )

        if should_discover and self.fixed_roi:
            roi = self.fixed_roi
            x0 = int(w * roi["x_pct"] / 100)
            y0 = int(h * roi["y_pct"] / 100)
            x1 = int(w * (roi["x_pct"] + roi["width_pct"]) / 100)
            y1 = int(h * (roi["y_pct"] + roi["height_pct"]) / 100)
            seed = [{"id": 0, "text": "", "conf": 100.0, "bbox": {"x0": x0, "y0": y0, "x1": x1, "y1": y1}}]
            if not self.tracker.has_regions():
                self.tracker.set_regions(seed, raw, w, h)
                print(f"[fixed-roi] seeded region bbox=({x0},{y0},{x1},{y1})")
            block = self.tracker.blocks.get(0)
            if block is not None:
                # Seeding alone has no real text yet (unlike normal
                # discovery, which OCRs the whole frame) - re-OCR this one
                # region immediately, same crop-and-OCR path used for a
                # settled content change, so the block isn't stuck showing
                # nothing until its content happens to change once.
                text = self._reocr_crop(masked_frame, block.bbox)
                block.text = text
                if len(text.strip()) >= MIN_TEXT_LEN:
                    self.translations[0] = self._translate(text)
            self.last_discovery = now
            self.force_discovery = False
        elif should_discover:
            raw_blocks = _to_bbox_dict(self._run_discovery(masked_frame))
            filtered = [
                b for b in raw_blocks
                if len(b["text"].strip()) >= MIN_TEXT_LEN and b["conf"] >= MIN_CONFIDENCE
            ]
            if not self.tracker.has_regions():
                self.tracker.set_regions(filtered, raw, w, h)
                to_translate = [(b["id"], b["text"]) for b in filtered]
            else:
                # Blocks whose translation is already missing (hidden by a
                # pending-change event - see the `pending_ids` handling
                # below - that then never settled, e.g. continuous
                # background animation under otherwise-static text) would
                # otherwise sit untouched by plain merge_regions() overlap
                # matching until block_stale_limit finally times them out
                # (~22s) and re-adds them from scratch. force_refresh_ids
                # lets this periodic rediscovery pass recover them
                # immediately instead of waiting - see merge_regions()'s
                # own docstring for the full mechanism.
                stale_translation_ids = {
                    bid for bid in self.tracker.blocks if self.translations.get(bid) is None
                }
                added, updated = self.tracker.merge_regions(
                    filtered, raw, w, h, force_refresh_ids=stale_translation_ids
                )
                to_translate = [(bid, text) for (bid, text, _conf) in added + updated]
            self._translate_many(to_translate)
            self.last_discovery = now
            self.force_discovery = False
            print(f"[discover] {len(filtered)} blocks pass filter, {len(to_translate)} (re)translated, tracker has {len(self.tracker.blocks)}")
            for bid, tb in self.tracker.blocks.items():
                print(f"    block {bid}: bbox={tb.bbox} translation={self.translations.get(bid)!r}")
        else:
            changed_ids, pending_ids, stale_ids, scene_changed = self.tracker.update(raw, w, h)
            if scene_changed and not self.fixed_roi:
                print("[scene_changed] clearing tracker, forcing discovery next tick")
                self.tracker.clear()
                self.translations.clear()
                self.force_discovery = True
            else:
                for block_id in pending_ids:
                    # Content mid-change - hide the now-stale translation
                    # immediately rather than let it linger on new pixels.
                    self.translations.pop(block_id, None)
                # Re-OCR each changed block first (sequentially - the OCR
                # engines' thread-safety under concurrent calls hasn't been
                # verified, unlike the translate HTTP calls below), then
                # translate whatever came back concurrently in one batch.
                to_translate = []
                for block_id in changed_ids:
                    block = self.tracker.blocks.get(block_id)
                    if block is None:
                        continue
                    text = self._reocr_crop(masked_frame, block.bbox)
                    block.text = text
                    if len(text.strip()) >= MIN_TEXT_LEN:
                        to_translate.append((block_id, text))
                        # Real, current content just confirmed by an actual
                        # re-OCR - reset the max_block_age_s clock (see
                        # region_tracker.py's use of created_at) so an
                        # actively-updating block never gets dropped just
                        # for having existed a while, only one that's gone
                        # quiet/stale for real.
                        block.created_at = time.monotonic()
                    else:
                        self.translations.pop(block_id, None)
                        print(f"[changed] block {block_id} re-OCR'd empty/too short: {text!r}")
                self._translate_many(to_translate)
                for block_id, text in to_translate:
                    print(f"[changed] block {block_id} re-OCR'd: {text!r} -> {self.translations.get(block_id)!r}")
                for block_id in stale_ids:
                    self.translations.pop(block_id, None)

        self._sync_labels(win, dpr)

        if one_shot:
            # Nothing keeps re-verifying this is still accurate while still
            # paused (no ongoing tracking runs), so rather than leaving a
            # single, possibly-already-stale snapshot on screen forever,
            # hide it again after a bit - see trigger_refresh()'s comment.
            # Reuses one persistent timer (created once, restarted on each
            # one-shot) rather than a fresh QTimer per call - confirmed live
            # 2026-08-26 that a fresh-timer-per-call version left a *stale*
            # pending timer around from an earlier one-shot, which could
            # still fire minutes later and wipe out a legitimately-resumed
            # continuous display that had nothing to do with it (toggle_
            # paused() stops this same timer on resume specifically to
            # prevent that - both fixes only work together). Parented to
            # win (a real, long-lived QWidget), not a bare QTimer.singleShot
            # - see keybindings_dialog.py's _CaptureWorker for why a timer
            # needs a real owner to reliably fire at all. QTimer.start()
            # restarts an already-running singleShot timer from zero, which
            # is exactly the "resettable" behavior a second refresh-while-
            # paused should have (matches Linux's own SubtitleHud
            # resetHideTimer()).
            if self._one_shot_hide_timer is None:
                from PySide6.QtCore import QTimer

                self._one_shot_hide_timer = QTimer(win)
                self._one_shot_hide_timer.setSingleShot(True)
                self._one_shot_hide_timer.timeout.connect(self._hide_all_labels)
            self._one_shot_hide_timer.start(ONE_SHOT_DISPLAY_MS)

    def _sync_labels(self, win, dpr):
        from PySide6.QtGui import QFont
        from PySide6.QtWidgets import QLabel

        live_ids = set(self.tracker.blocks.keys()) if self.tracker else set()

        for block_id in list(self.labels.keys()):
            if block_id not in live_ids or block_id not in self.translations:
                self.labels[block_id].deleteLater()
                del self.labels[block_id]

        for block_id in live_ids:
            translation = self.translations.get(block_id)
            if not translation:
                continue
            if translation != self._last_rendered.get(block_id):
                # Our own HUD label is itself on-screen pixels dxcam will
                # capture on the next frame - without this, discovery/re-OCR
                # reads back the translated text we just drew as if it were
                # new game content (confirmed live: a duplicated, double-
                # translated "[ja draft] [ja draft] norco" ghost block).
                # suppress() re-baselines this block against whatever's
                # currently on screen instead of diffing it, for a window
                # comfortably longer than one tick.
                self.tracker.suppress(block_id, TICK_MS / 1000.0 * 4)
                self._last_rendered[block_id] = translation
            block = self.tracker.blocks[block_id]
            x0, y0, x1, y1 = block.bbox
            ox, oy = self.window_offset
            lx0, ly0 = (x0 + ox) / dpr, (y0 + oy) / dpr
            lw = max((x1 - x0) / dpr, 40)
            lh = (y1 - y0) / dpr
            lcy = ly0 + lh / 2  # vertical center of the *original* detected region

            label = self.labels.get(block_id)
            if label is None:
                label = QLabel(win)
                label.setFont(QFont("Yu Gothic UI", 12))
                # Fully opaque, not the translucent rgba(20,20,20,200) this
                # used to be. Confirmed live 2026-08-26 (via a real user's
                # tray_app.log, not just code reading) that a block sitting
                # over animated game background (NORCO's title screen has a
                # continuously moving crane/lights) got stuck in permanent
                # pending_change limbo shortly after being shown and never
                # recovered - not even a manual refresh helped, since the
                # very next fresh display over the same spot hit the same
                # problem again within a couple seconds. Root cause: this
                # label's ~78%-opaque background let the animated pixels
                # underneath bleed through, so region_tracker.py's per-block
                # pixel-diff sampling kept seeing real, continuous change
                # under a label whose *text* never changed - suppress()'s
                # 2-second self-capture grace window (see _sync_labels()
                # below) isn't a fix for this, since it only covers the
                # brief render-transition moment, not indefinite ongoing
                # background motion for as long as the label stays up.
                # Opaque removes the whole class of failure - whatever's
                # behind the label physically can't factor into the diff at
                # all once nothing shows through.
                label.setStyleSheet(
                    "color: white; background-color: rgb(20, 20, 20); padding: 4px;"
                )
                label.setWordWrap(True)
                self.labels[block_id] = label
            label.setFixedWidth(int(max(lw, 120)))
            label.setText(translation)
            # Confirmed live 2026-08-28 (real screenshot, plus the user
            # directly correcting an initial misreading of it): the box
            # must never be shorter than the original detected region -
            # since it sits exactly over the original on-screen text, a
            # shorter box doesn't fully cover it and the *original*,
            # untranslated game text peeks out from both above and below
            # (confirmed by the user: both edges, not just one - this had
            # misread as garbled/overlapping text in an earlier pass of
            # this same investigation, but was actually two genuinely
            # different, individually-correct texts overlapping, not a
            # rendering-corruption bug).
            #
            # label.adjustSize() used to be the other half of this (max()'d
            # against int(lh) below) but confirmed live 2026-08-29 it isn't
            # trustworthy for multi-line word-wrapped CJK text - real
            # logged case: two blocks with the same 2-line wrap and similar
            # width came out 244x34 and 254x51, a ~1.5x height difference
            # for comparable content. The 34 one visibly clipped the
            # middle sliver of each glyph on screen (top/bottom of every
            # character cut off, exactly matching a user report of the
            # bottommost of several close-together blocks being
            # "illegible, top and bottom missing"), and it wasn't rescued
            # by the int(lh) floor because that floor is the *original*
            # English text's own bbox height, which has no necessary
            # relationship to how many lines the *translated* text wraps
            # into - the same mismatch also explains the opposite-looking
            # complaint (a block with excessive top/bottom margin): a
            # multi-line original English paragraph's tall bbox floors a
            # Japanese translation that compacts into fewer/shorter lines.
            #
            # KNOWN REMAINING ISSUE (2026-08-29, deferred - clipping above
            # was the priority for the initial release): the int(lh) floor
            # itself still occasionally makes a box noticeably taller than
            # its own translated text needs, for the exact reason above.
            # A real fix needs the box to cover the original region without
            # necessarily being *sized* to it - e.g. painting over just the
            # original bbox separately from the (independently, correctly
            # sized) translation label - not attempted this pass.
            #
            # heightForWidth(), not a separate QFontMetrics simulation:
            # tried computing the wrapped height by hand via
            # QFontMetrics.boundingRect(Qt.TextWordWrap, ...) first, but
            # confirmed live 2026-08-29 that has its own real mismatch
            # against how this QLabel actually wraps - right at the wrap
            # boundary (one real case: a single trailing character wrapping
            # to its own second line got only one line's worth of height
            # reserved instead of two), i.e. a *different* wrapping
            # algorithm/metrics path than QLabel's own, not just a wrong
            # padding constant. heightForWidth() asks this exact QLabel
            # (already given its real font, text, and fixed width) what
            # *it* needs - the same computation it uses for its own
            # sizeHint() - so there is no second implementation of word
            # wrap left to disagree with the real one.
            final_h = max(label.heightForWidth(label.width()), int(lh))
            label.setFixedHeight(final_h)
            label.move(int(lx0), int(lcy - final_h / 2))
            label.show()
            print(f"    [label] block={block_id} at logical=({int(lx0)},{int(ly0)}) size={label.size()} visible={label.isVisible()} text={translation!r}")


def show_toast(handles, text, duration_ms=2000):
    """Brief on-screen confirmation that a keybinding actually fired -
    matches Linux's src/index.tsx showToast() (top-right corner, ~2s,
    resettable per-call rather than stacking multiple) which the Windows
    port had no equivalent of at all before this: a user pressing a bound
    button had no way to tell whether it registered. Reuses one QLabel
    parented to the existing full-desktop HUD widget (handles.win) rather
    than a separate top-level window - cheaper, and the HUD is already
    topmost/click-through/frameless so a child label inherits all of that
    for free. The QTimer is parented to the label itself (not a bare
    QTimer.singleShot) - see keybindings_dialog.py's _CaptureWorker for why
    that distinction matters when a timer needs to reliably fire."""
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QLabel

    win = handles.win
    label = getattr(handles, "toast_label", None)
    if label is None:
        label = QLabel(win)
        # Opaque, not translucent - see the translation labels' own comment
        # in _sync_labels() for why (this widget isn't itself a tracked
        # block, but it's cheap consistency while touching this styling
        # anyway, and the toast isn't in self.labels so it isn't part of
        # the pre-OCR masking either - a real, separate, lower-priority gap
        # noted in project memory, not fixed here).
        label.setStyleSheet(
            "background-color: rgb(8, 10, 12); color: #f7f4ee; "
            "padding: 8px 14px; border-radius: 8px;"
        )
        label.setFont(QFont("Yu Gothic UI", 11))
        label.setAlignment(Qt.AlignCenter)
        handles.toast_label = label
        handles.toast_timer = QTimer(label)
        handles.toast_timer.setSingleShot(True)
        handles.toast_timer.timeout.connect(label.hide)

    label.setText(text)
    label.adjustSize()
    margin = 24
    label.move(win.width() - label.width() - margin, margin)
    label.show()
    label.raise_()
    handles.toast_timer.start(duration_ms)


def _build_app():
    """Shared setup between main() (a fixed-duration CLI harness used for
    dev testing, e.g. the LTPipelineLoop scheduled task) and tray_app.py
    (the real day-to-day entry point, which only quits when the user picks
    Exit from the tray menu) - both need the identical HUD window +
    PipelineLoop + keybinding-detection-thread wiring, just with different
    top-level lifetimes, so that part lives here once instead of twice."""
    from window_finder import ensure_dpi_aware

    ensure_dpi_aware()  # must happen before QApplication() - see window_finder's docstring

    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import QApplication, QWidget

    app = QApplication(sys.argv)
    screen = app.primaryScreen()
    dpr = screen.devicePixelRatio()
    geo = screen.geometry()
    print(f"screen logical={geo.width()}x{geo.height()} devicePixelRatio={dpr}")

    win = QWidget()
    win.setWindowFlags(
        Qt.FramelessWindowHint
        | Qt.WindowStaysOnTopHint
        | Qt.Tool
        | Qt.WindowTransparentForInput
        # WS_EX_NOACTIVATE - without this, showing/updating the HUD can
        # itself become the foreground window (confirmed live 2026-08-27:
        # GetForegroundWindow() returned title 'pythonw' - Qt's default
        # window title, i.e. this HUD widget itself - the moment it was
        # shown, with the actual game no longer foreground at all).
        # PipelineLoop._is_target_window_foreground() then permanently sees
        # the HUD, not the game, as focused and hides everything every tick
        # from then on - exactly the "shows once, then never comes back,
        # refresh/resume don't help" bug: the foreground check runs before
        # any of that logic, so nothing downstream gets a chance to matter.
        | Qt.WindowDoesNotAcceptFocus
    )
    win.setAttribute(Qt.WA_TranslucentBackground, True)
    win.setGeometry(0, 0, geo.width(), geo.height())
    # Plain show() instead of showFullScreen(): geometry is already set
    # explicitly above to cover the whole screen, and showFullScreen()'s
    # OS-level "this is the active fullscreen app" window state is a
    # plausible extra source of the same foreground-steal - not needed for
    # a click-through overlay that isn't a real fullscreen application.
    win.show()

    pipeline = PipelineLoop()

    import os
    import queue
    import threading

    import input_state
    from keybinding_matcher import KeybindingMatcher

    # Detection runs on its own background thread, independent of the Qt
    # main thread - confirmed live 2026-08-26 that a synchronous OCR/
    # translate tick() call (already flagged as a known limitation, see this
    # file's module docstring) can block the main thread long enough to
    # skip every 150ms poll for over a second straight, silently dropping
    # an entire press-and-release (proven two ways: a standalone script
    # polling input_state.current_held() correctly saw a 1.2s F9 hold the
    # whole way through, while the same hold produced zero polls in this
    # process's own [keybinding-debug] log at the time). The *detection*
    # (was this a tap or a threshold-crossing hold) must not depend on the
    # main thread's availability - only *applying* the resulting action can
    # tolerate a short queued delay, since pause/refresh don't need
    # millisecond timing. fired_commands is the thread-safe handoff: the
    # background thread only ever enqueues a command string, never touches
    # PipelineLoop's own state (tracker/translations) directly.
    fired_commands = queue.Queue()
    KEYBINDING_POLL_MS = 150  # matches index.tsx's startHotkeyPolling() interval on Linux

    def on_binding_fire(binding, while_held):
        fired_commands.put(binding["command"])
        print(f"[keybinding] fired {binding['command']!r} (while_held={while_held}) keys={binding['keys']}")

    from types import SimpleNamespace

    # handles.matcher is read fresh by the background thread on every poll
    # (not captured into a local closure variable) specifically so
    # rebuild_keybinding_matcher() can swap it out after a live settings
    # reload and have the thread pick up the new one on its very next
    # iteration - a single attribute reassignment is safe to read from
    # another thread without extra locking (the GIL makes the swap atomic;
    # the thread only ever sees the old or the new object, never a mix).
    handles = SimpleNamespace(app=app, win=win, dpr=dpr, pipeline=pipeline, on_binding_fire=on_binding_fire)
    rebuild_keybinding_matcher(handles)

    stop_keybinding_thread = threading.Event()
    # Set by pause_for_dialog() while a settings-type dialog (Settings,
    # Keybindings, ROI, OCR-language) is open in the foreground - see that
    # function's docstring for why this exists (a real Qt access-violation
    # crash, not just tidiness).
    keybinding_poll_paused = threading.Event()

    def _keybinding_thread_main():
        last_held = set()
        while not stop_keybinding_thread.is_set():
            if not keybinding_poll_paused.is_set():
                held = input_state.current_held()
                if held != last_held:
                    # Edge-triggered (only on an actual change, not every
                    # 150ms poll) so this doesn't flood the log. Added after
                    # a real gameplay test where a configured gamepad
                    # trigger binding (pad:0:LT) silently did nothing, with
                    # no way to tell whether input_state.py ever even
                    # detected the press in the first place vs. the matcher
                    # failing to act on a correctly-detected press - this
                    # makes that observable on the next test.
                    print(f"[keybinding-debug] held changed: {held or '(none)'}")
                    last_held = held
                handles.matcher.poll(held)
            time.sleep(KEYBINDING_POLL_MS / 1000.0)

    keybinding_thread = threading.Thread(target=_keybinding_thread_main, daemon=True)
    keybinding_thread.start()

    def _watchdog_thread_main():
        """Detects a genuinely frozen main thread and forces the process to
        exit, so a freeze is visible (tray icon disappears, a clear log
        line is written) instead of silently sitting unresponsive forever
        with no signal. Added 2026-08-27 after a real freeze was diagnosed
        live with py-spy: both the Qt main thread and the WinRT OCR worker
        thread were genuinely *idle* (not blocked inside any Python call) -
        Qt's own QTimer dispatch had apparently just stopped firing tick()
        at all, a different and still-unexplained mechanism from the
        earlier-fixed OCR-call-hang class of bug. This doesn't fix that
        root cause (not yet understood) - it only bounds the damage the
        same way OCR_CALL_TIMEOUT_S does for the OCR call specifically:
        turn an unbounded silent failure into a bounded, observable one.

        WATCHDOG_STALL_S is set well above any *legitimate* single-tick
        worst case (OCR_CALL_TIMEOUT_S=5s + up to provider.translate()'s
        own ~20s HTTP timeout, even run concurrently across
        TRANSLATE_MAX_WORKERS - so a legitimately slow but bounded tick
        should never come close to this) - this must only ever fire on a
        real stall, not a slow-but-working tick.

        No auto-relaunch here (no supervisor process exists yet to restart
        this one after it exits) - that's a real next step if this recurs
        often enough to be worth building, not attempted in this pass.

        Must skip entirely while keybinding_poll_paused is set - pause_for_dialog()
        stops the tick timer on purpose while Settings/Keybindings/ROI/
        OCR-language is open, and OCR-language's install flow alone (a real
        UAC prompt + DISM download + confirm-retry loop) can easily run past
        WATCHDOG_STALL_S under completely normal use. Confirmed live
        2026-08-28: without this check, the watchdog can't tell "the user is
        still looking at an intentionally-paused dialog" from "genuinely
        stuck" and force-exits mid-install - this is what a user reported as
        the app randomly crashing right after installing an OCR language."""
        while True:
            time.sleep(5.0)
            if keybinding_poll_paused.is_set():
                continue
            stalled_for = time.monotonic() - pipeline.last_tick_at
            if stalled_for >= WATCHDOG_STALL_S:
                print(
                    f"[watchdog] tick() has not run in {stalled_for:.1f}s "
                    f"(last tick #{pipeline._tick_count}) - main thread appears "
                    f"genuinely stuck, forcing process exit"
                )
                sys.stdout.flush()
                sys.stderr.flush()
                os._exit(1)

    watchdog_thread = threading.Thread(target=_watchdog_thread_main, daemon=True)
    watchdog_thread.start()

    def _drain_fired_commands():
        from i18n import t

        while True:
            try:
                command = fired_commands.get_nowait()
            except queue.Empty:
                break
            if command == "refresh":
                pipeline.trigger_refresh()
                show_toast(handles, t("toast.refreshed"))
            elif command == "pause_resume":
                pipeline.toggle_paused()
                show_toast(handles, t("toast.paused") if pipeline.paused else t("toast.resumed"))

    drain_timer = QTimer()
    drain_timer.timeout.connect(_drain_fired_commands)
    drain_timer.start(KEYBINDING_POLL_MS)

    timer = QTimer()
    timer.timeout.connect(lambda: pipeline.tick(win, dpr))
    timer.start(TICK_MS)

    def _check_tick_timer_alive():
        # Self-healing check: confirmed live 2026-08-27 that `timer` (the
        # one driving tick()) can end up stopped/inactive while `drain_timer`
        # keeps firing normally on the very same event loop - py-spy showed
        # every Python thread genuinely idle in that state (nothing blocked
        # anywhere), consistent with the underlying QTimer simply not being
        # active rather than anything Python-level stuck. The exact trigger
        # wasn't pinned down (pause_for_dialog()/resume_after_dialog() is
        # the one *known*, intentional way this timer gets stopped - already
        # try/finally-guarded in tray_app.py's _open_settings(), and the log
        # showed Settings was never opened in the session where this was
        # caught, so something else can apparently also stop it). Since
        # `drain_timer` reliably keeps running even when `timer` doesn't,
        # it's a safe place to supervise `timer` from - runs on the same
        # (main) thread, so calling QTimer methods here is safe, unlike
        # doing this from the background watchdog thread. Skips the restart
        # while a dialog has us intentionally paused (keybinding_poll_paused
        # doubles as that signal - see pause_for_dialog()), so this doesn't
        # fight a real, intended pause.
        if not timer.isActive() and not keybinding_poll_paused.is_set():
            print(
                f"[watchdog] tick timer was inactive (last tick #{pipeline._tick_count}, "
                f"{time.monotonic() - pipeline.last_tick_at:.1f}s ago) - restarting it"
            )
            timer.start(TICK_MS)

    tick_health_timer = QTimer()
    tick_health_timer.timeout.connect(_check_tick_timer_alive)
    tick_health_timer.start(2000)

    handles.stop_keybinding_thread = stop_keybinding_thread
    handles.keybinding_poll_paused = keybinding_poll_paused
    handles.keybinding_thread = keybinding_thread
    handles.drain_timer = drain_timer
    handles.timer = timer
    handles.tick_health_timer = tick_health_timer
    return handles


def pause_for_dialog(handles):
    """Call before opening any settings-type dialog (Settings itself, or
    its Keybindings/ROI/OCR-language sub-dialogs) and call
    resume_after_dialog() once it closes.

    Stops the main tick timer (no more OCR/translate/HUD-label churn while
    the dialog is up) and pauses the background keybinding-polling thread.
    This mirrors the *intent* of the Linux side's qam_open/roi_editor_open
    flags (capture_dynamic.py's on_sample() skip) - "don't do capture work
    while our own settings UI is on screen" - but for a different, more
    serious reason than Linux has: Linux's equivalent key-capture path
    (main.py's capture_input_signal()) already runs safely unsynchronized
    alongside the regular hotkey poll, because hidraw allows independent
    concurrent readers and there's no native GUI toolkit state involved -
    nothing to corrupt. Windows has real shared Qt widget state, and
    confirmed live 2026-08-26: opening KeybindingsDialog's "press to bind"
    capture flow while this background thread was still polling input, then
    capturing a key, produced a genuine native access violation
    (0xc0000005 in Qt6Widgets.dll per Windows' own Event Viewer record) -
    reproduced twice with two different keys. The exact Qt-internal
    mechanism wasn't isolated further, but stopping both potential sources
    of concurrent Qt-adjacent activity during the capture window removes
    the hazard rather than chasing the precise trigger. This also fixes a
    real (non-crash) UX bug for free: without it, an existing binding like
    F9 firing a real refresh/pause while the user is mid-way through
    binding a *new* key to something else."""
    handles.timer.stop()
    handles.keybinding_poll_paused.set()


def resume_after_dialog(handles):
    # Reset before clearing keybinding_poll_paused, not after: the watchdog
    # thread wakes up on its own 5s cycle independent of this call, so
    # without this it could see the stale pre-pause last_tick_at (however
    # long the dialog was actually open for) in the instant after the pause
    # flag clears but before the restarted timer's first real tick() has
    # run - a false "stuck" reading exactly at dialog-close, not just during
    # a still-open dialog (see _watchdog_thread_main()'s docstring).
    handles.pipeline.last_tick_at = time.monotonic()
    handles.keybinding_poll_paused.clear()
    handles.timer.start(TICK_MS)


def rebuild_keybinding_matcher(handles):
    """(Re-)builds the live KeybindingMatcher from handles.pipeline.settings
    - called once from _build_app() and again by tray_app.py after the
    Settings dialog (or its Keybindings sub-dialog) saves, so an edited
    keybinding actually takes effect without restarting the whole app.
    Reassigning handles.matcher is enough - the background polling thread
    always reads handles.matcher fresh, never a stale captured reference."""
    from keybinding_matcher import KeybindingMatcher

    handles.matcher = KeybindingMatcher(handles.pipeline.settings.get("keybindings", []), handles.on_binding_fire)
    print(f"[keybinding] matcher (re)built with {len(handles.matcher.groups)} key-set group(s)")


def _shutdown(handles):
    handles.stop_keybinding_thread.set()
    handles.keybinding_thread.join(timeout=1.0)
    print("loop stopped")


def main():
    duration_s = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0

    handles = _build_app()

    from PySide6.QtCore import QTimer

    QTimer.singleShot(int(duration_s * 1000), handles.app.quit)
    print(f"loop running for {duration_s}s, tick={TICK_MS}ms")
    sys.stdout.flush()
    handles.app.exec()
    _shutdown(handles)


if __name__ == "__main__":
    main()
