"""Windows' built-in OCR (Windows.Media.Ocr via the `winrt` package) as an
alternative to chrome_screen_ai.dll (screenai_engine.py) - much faster
(~0.19s/full-frame vs screenai's 1-2s+, confirmed live 2026-08-26) but with
three real caveats the caller needs to know about:

1. No per-line confidence score at all - OcrWord/OcrLine just don't expose
   one. Every block gets a flat placeholder conf; noise filtering has to
   lean entirely on text-shape heuristics (is_useful_text/looks_like_text
   in capture_dynamic.py), not a confidence threshold.
2. Needs the target language's OCR component installed as a separate
   Windows optional feature (see windows_ocr_lang.py) - independent of
   display/input language. Only whatever's installed shows up in
   `OcrEngine.available_recognizer_languages`.
3. **Every WinRT call this class makes (engine construction AND each
   recognize_async()) must happen on one single dedicated background
   thread, never the caller's own thread.** Confirmed live 2026-08-27: on
   the real Windows port, calling recognize_async() from PipelineLoop's Qt
   main thread (the same thread that also calls window_finder's
   win32gui.EnumWindows()/GetForegroundWindow() to find/focus-check the
   target game window) permanently breaks EnumWindows-based window lookups
   for the rest of the process's life, after just the *first* OCR call -
   this was the real cause of "HUD shows once right after startup, then
   never recovers, refresh/resume don't help either." A deterministic
   bisection script confirmed: WinRT import + OcrEngine construction alone
   is harmless (checked immediately after, still fine); the very first
   actual recognize_async() call is what breaks it, and switching from
   asyncio.run() per call to a single reused event loop did NOT fix it
   (ruling out "event loop churn" as the mechanism) - only moving the
   WinRT calls to a separate thread did (verified live: 9 checks across a
   background worker doing 6 real OCR calls, zero EnumWindows failures on
   the main thread throughout). This class handles that isolation
   internally via a dedicated single-worker ThreadPoolExecutor so callers
   (PipelineLoop.discover_blocks()) don't need to know or care - the public
   API is still a plain synchronous call.

Windows.Media.Ocr only returns flat lines, no paragraph/block grouping
(same shape problem ocr_worker.py's Tesseract SPARSE_TEXT path already has) -
reuses group_lines_into_blocks() from ocr_grouping.py (not from ocr_worker.py
directly - confirmed live 2026-08-29 that ocr_worker.py imports PIL
unconditionally at module level for its own Tesseract-specific code, so
importing anything from it at all pulled in a Pillow dependency this port
never otherwise needed and never had in requirements.txt - a clean install
hit ModuleNotFoundError: PIL here, silently leaving the OCR engine
unconfigured) rather than reinventing the same wrapping-paragraph-merge
heuristic here.
"""
import asyncio
import sys
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # for ocr_grouping.group_lines_into_blocks

from ocr_grouping import group_lines_into_blocks

PLACEHOLDER_CONF = 100.0  # no real confidence signal from this engine - see module docstring
# A real call normally takes ~0.1-0.3s (confirmed live) - this is a safety
# ceiling, not a tuned expected latency, so it can afford to be generous.
OCR_CALL_TIMEOUT_S = 5.0


class WindowsOcrEngine:
    def __init__(self, language_tag="en-US"):
        self.language_tag = language_tag
        # See module docstring point 3 - every WinRT call (construction and
        # recognize) is confined to this one dedicated thread for the
        # object's whole lifetime, isolated from whatever thread constructs
        # this class (the Qt main thread in practice).
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="winrt-ocr")
        self._loop = None  # created inside the worker thread - see _ensure_engine()
        self._engine = None
        # Block until the worker thread has actually constructed the OCR
        # engine, so __init__'s existing "raise if the language isn't
        # available" contract still holds synchronously for the caller.
        self._executor.submit(self._ensure_engine).result()
        self._consecutive_timeouts = 0

    def _ensure_engine(self):
        """Runs on the dedicated worker thread only - never call directly
        from any other thread."""
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
        if self._engine is None:
            from winrt.windows.globalization import Language
            from winrt.windows.media.ocr import OcrEngine

            self._engine = OcrEngine.try_create_from_language(Language(self.language_tag))
            if self._engine is None:
                available = [l.language_tag for l in OcrEngine.available_recognizer_languages]
                raise RuntimeError(
                    f"no Windows OCR engine for language {self.language_tag!r} "
                    f"(installed: {available!r} - install more via windows_ocr_lang.py)"
                )

    async def _recognize(self, bgra_bytes, width, height):
        from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
        from winrt.windows.security.cryptography import CryptographicBuffer

        bitmap = SoftwareBitmap(BitmapPixelFormat.BGRA8, width, height, BitmapAlphaMode.IGNORE)
        buffer = CryptographicBuffer.create_from_byte_array(bytearray(bgra_bytes))
        bitmap.copy_from_buffer(buffer)
        return await self._engine.recognize_async(bitmap)

    def _discover_blocks_on_worker(self, bgra_bytes, width, height):
        """Runs on the dedicated worker thread only - see module docstring
        point 3 for why this can never run on the caller's own thread."""
        result = self._loop.run_until_complete(self._recognize(bgra_bytes, width, height))

        lines = []
        for line in result.lines:
            text = (line.text or "").strip()
            if not text or not line.words:
                continue
            x0 = min(w.bounding_rect.x for w in line.words)
            y0 = min(w.bounding_rect.y for w in line.words)
            x1 = max(w.bounding_rect.x + w.bounding_rect.width for w in line.words)
            y1 = max(w.bounding_rect.y + w.bounding_rect.height for w in line.words)
            lines.append({"text": text, "conf": PLACEHOLDER_CONF, "bbox": (int(x0), int(y0), int(x1), int(y1))})

        blocks = group_lines_into_blocks(lines)
        return [
            {"id": b["id"], "text": b["text"], "conf": b["conf"],
             "bbox": (b["bbox"]["x0"], b["bbox"]["y0"], b["bbox"]["x1"], b["bbox"]["y1"])}
            for b in blocks
        ]

    def _restart_worker(self):
        """Abandon the current worker thread/executor and spin up a fresh
        one - called after a confirmed hang (see discover_blocks()). Python
        can't forcibly kill a stuck thread, so the old one is simply
        orphaned (leaks one thread, harmless for the rest of the process's
        life) rather than joined."""
        print("[windows-ocr] worker thread appears hung - abandoning it and starting a fresh one")
        # See CaptureWorker._restart_worker() (pipeline_loop.py) for the
        # identical fix and full reasoning - drop the reference to whatever
        # this executor still has queued instead of leaving it to GC.
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="winrt-ocr")
        self._loop = None
        self._engine = None
        self._executor.submit(self._ensure_engine).result(timeout=OCR_CALL_TIMEOUT_S)
        self._consecutive_timeouts = 0

    def discover_blocks(self, bgra_bytes, width, height):
        """Same return shape as screenai_engine.proto_to_blocks(): a list of
        {"id", "text", "conf", "bbox": (x0,y0,x1,y1)}, already grouped into
        paragraph-like blocks. Takes BGRA bytes directly (no RGBA reorder
        needed - unlike Screen AI, Windows.Media.Ocr's SoftwareBitmap is
        happy with BGRA8, which is dxcam's native capture format here).
        Synchronous from the caller's perspective - internally dispatches to
        and blocks on the dedicated WinRT worker thread, see module
        docstring point 3.

        Bounded by OCR_CALL_TIMEOUT_S: confirmed live 2026-08-27 that a
        real-world heavy-load scene (rapid repeated OCR/HUD-update ticks -
        e.g. a constantly-animated background or a video cutscene with
        subtitles) can make the worker thread's WinRT call itself hang
        indefinitely - since PipelineLoop.tick() calls this synchronously
        from the Qt main thread, an unbounded wait there froze the *entire*
        app (HUD, keybinding response, everything - matches the user's
        report of refresh/keybindings going completely unresponsive, not
        just the OCR/display). A timeout here means one bad call costs at
        most OCR_CALL_TIMEOUT_S instead of forever; a caller sees an empty
        block list for that tick (same as "no text found"), and every
        timeout triggers a real worker-thread restart so a truly wedged
        WinRT call doesn't leave the app timing out on every single tick
        forever after."""
        try:
            return self._executor.submit(
                self._discover_blocks_on_worker, bgra_bytes, width, height
            ).result(timeout=OCR_CALL_TIMEOUT_S)
        except FutureTimeoutError:
            self._consecutive_timeouts += 1
            print(
                f"[windows-ocr] OCR call timed out after {OCR_CALL_TIMEOUT_S}s "
                f"({self._consecutive_timeouts} consecutive) - treating as no text found this tick"
            )
            # Restart on the very first timeout, not the second - see
            # CaptureWorker.grab() (pipeline_loop.py) for the full
            # reasoning: with max_workers=1, a second submission to an
            # executor whose worker is genuinely hung just queues behind
            # the first and is guaranteed to also time out without ever
            # running, so waiting for "2 consecutive" only doubles the
            # recovery latency without confirming anything new.
            self._try_restart()
            return []
        except Exception as exc:
            # Not a timeout - same real gap as CaptureWorker.grab()
            # (pipeline_loop.py), confirmed via code review 2026-08-28: if
            # _restart_worker()'s own _ensure_engine() call previously
            # failed, self._executor ends up a fresh *working* executor
            # but self._loop/self._engine stay None (both are reset before
            # the possibly-failing _ensure_engine() call). The *next*
            # discover_blocks() then submits fine (no timeout - the worker
            # thread is healthy), but _discover_blocks_on_worker() raises
            # AttributeError on self._loop.run_until_complete for a real
            # (non-None) object - not a FutureTimeoutError, so it fell
            # through this method uncaught into tick()'s Qt timer
            # callback. Bounding all exceptions here, not just timeouts,
            # matches this class's own "never let an OCR-layer failure
            # escape into tick()" intent.
            print(f"[windows-ocr] discover_blocks() failed: {exc!r} - treating as no text found this tick")
            self._try_restart()
            return []

    def _try_restart(self):
        try:
            self._restart_worker()
        except Exception as exc:
            print(f"[windows-ocr] worker restart failed: {exc!r}")
