#!/usr/bin/env python3
"""OCR engine backed by Chrome's on-device Screen AI library
(libchromescreenai.so) - the same accessibility OCR engine Chromium uses to
read screenshots/PDFs for screen readers, extracted as a standalone .so.

Unlike ocr_worker.py's DiscoveryEngine (Tesseract PSM.SPARSE_TEXT), this
engine groups detected text lines into blocks/paragraphs itself
(LineBox.block_id in the wire format) - see proto/chrome_screen_ai.proto -
so the fragile geometric merge heuristics in group_lines_into_blocks() don't
apply here; blocks are built directly from the engine's own grouping.

Two invocation modes, mirroring the tesserocr/CLI split in ocr_worker.py:
  Oneshot: python3 ocr_screenai.py <image> <model_dir> <min_confidence>
  Worker:  python3 ocr_screenai.py --worker

Run as a subprocess (not imported in-process) so a native-library crash or
an incompatible libchromescreenai.so build can't take down the resident
ocr_worker.py HTTP server with it.
"""
import ctypes
import json
import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

_WORKER_MODE = "--worker" in sys.argv

_RGBA_8888 = 4
_ALPHA_OPAQUE = 1


# ---- ctypes mirror of the Skia structs PerformOCR reads off the pointer it
# is given. Field *names* below are ours; the layout (order/types) must
# match SkBitmap/SkPixmap/SkImageInfo/SkColorInfo's real memory layout for
# the .so to read the right bytes - that layout is Skia's public C++ ABI,
# not something PlayTranslate controls.
class _SkColorInfo(ctypes.Structure):
    _fields_ = [
        ("color_space", ctypes.c_void_p),
        ("color_type", ctypes.c_int32),
        ("alpha_type", ctypes.c_int32),
    ]


class _SkISize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_int32), ("height", ctypes.c_int32)]


class _SkImageInfo(ctypes.Structure):
    _fields_ = [("color_info", _SkColorInfo), ("dimensions", _SkISize)]


class _SkPixmap(ctypes.Structure):
    _fields_ = [
        ("pixels", ctypes.c_void_p),
        ("row_bytes", ctypes.c_size_t),
        ("info", _SkImageInfo),
    ]


class _SkBitmap(ctypes.Structure):
    _fields_ = [
        ("pixel_ref", ctypes.c_void_p),
        ("pixmap", _SkPixmap),
        ("flags", ctypes.c_uint32),
    ]


_SIZE_CB = ctypes.CFUNCTYPE(ctypes.c_uint32, ctypes.c_char_p)
_CONTENT_CB = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_uint32, ctypes.c_void_p)


class ScreenAIEngine:
    """Loads libchromescreenai.so and wraps its PerformOCR entry point."""

    def __init__(self, model_dir):
        self.model_dir = model_dir
        so_path = os.path.join(model_dir, "libchromescreenai.so")
        if not os.path.exists(so_path):
            raise FileNotFoundError(f"libchromescreenai.so not found at {so_path}")

        # The .so pulls its own TFLite model files through these callbacks
        # rather than opening them itself - anchor the ctypes trampolines on
        # self so they aren't GC'd while the .so still holds a raw pointer
        # to them.
        @_SIZE_CB
        def get_size(rel_path):
            try:
                full = os.path.join(self.model_dir, rel_path.decode("utf-8"))
                return os.path.getsize(full) if os.path.exists(full) else 0
            except Exception:
                return 0

        @_CONTENT_CB
        def get_content(rel_path, max_len, out_ptr):
            try:
                full = os.path.join(self.model_dir, rel_path.decode("utf-8"))
                if not os.path.exists(full):
                    return
                with open(full, "rb") as f:
                    chunk = f.read(max_len)
                ctypes.memmove(out_ptr, chunk, len(chunk))
            except Exception:
                pass

        self._size_cb = get_size
        self._content_cb = get_content

        self.lib = ctypes.CDLL(so_path, mode=getattr(os, "RTLD_LAZY", 1))
        self.lib.SetFileContentFunctions.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.lib.SetFileContentFunctions.restype = None
        self.lib.InitOCRUsingCallback.restype = ctypes.c_bool
        self.lib.SetOCRLightMode.argtypes = [ctypes.c_bool]
        self.lib.SetOCRLightMode.restype = None
        self.lib.PerformOCR.argtypes = [
            ctypes.POINTER(_SkBitmap), ctypes.POINTER(ctypes.c_uint32)
        ]
        self.lib.PerformOCR.restype = ctypes.c_void_p
        self.lib.FreeLibraryAllocatedCharArray.argtypes = [ctypes.c_void_p]
        self.lib.FreeLibraryAllocatedCharArray.restype = None
        self.lib.GetMaxImageDimension.restype = ctypes.c_uint32

        self.lib.SetFileContentFunctions(
            ctypes.cast(self._size_cb, ctypes.c_void_p),
            ctypes.cast(self._content_cb, ctypes.c_void_p),
        )
        if not self.lib.InitOCRUsingCallback():
            raise RuntimeError("InitOCRUsingCallback() returned false")

        # Full model, not the smaller/faster "light" variant. The resident
        # worker process (see below) is what removes the latency cost this
        # project cares about (see project_playtranslate_speed) - accuracy
        # matters more than shaving engine-internal inference time here.
        self.lib.SetOCRLightMode(False)
        self.max_dim = int(self.lib.GetMaxImageDimension()) or 2048

    def perform(self, rgba_bytes, width, height):
        bitmap = _SkBitmap()
        # Anchored on self: PerformOCR reads through this pointer
        # synchronously within the call below, but keep it alive past the
        # call just in case the engine retains it for a following call.
        self._buffer = ctypes.c_char_p(rgba_bytes)
        bitmap.pixmap.pixels = ctypes.cast(self._buffer, ctypes.c_void_p)
        bitmap.pixmap.row_bytes = width * 4
        bitmap.pixmap.info.color_info.color_type = _RGBA_8888
        bitmap.pixmap.info.color_info.alpha_type = _ALPHA_OPAQUE
        bitmap.pixmap.info.dimensions.width = width
        bitmap.pixmap.info.dimensions.height = height

        out_len = ctypes.c_uint32(0)
        ptr = self.lib.PerformOCR(ctypes.byref(bitmap), ctypes.byref(out_len))
        if not ptr:
            return None
        try:
            return ctypes.string_at(ptr, out_len.value)
        finally:
            self.lib.FreeLibraryAllocatedCharArray(ptr)


def _load_rgba(image_path, max_dim):
    """Returns (rgba_bytes, w, h, scale_x, scale_y) - scale maps engine
    pixel coords back to the original (possibly downscaled-for-engine)
    image, matching the scale-back convention discover_blocks() callers
    already expect (see DiscoveryEngine.discover in ocr_worker.py)."""
    from PIL import Image

    img = Image.open(image_path)
    orig_w, orig_h = img.size
    if max(orig_w, orig_h) > max_dim:
        factor = min(max_dim / orig_w, max_dim / orig_h)
        new_w, new_h = max(1, int(orig_w * factor)), max(1, int(orig_h * factor))
        img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    w, h = img.size
    return img.tobytes(), w, h, (orig_w / w if w else 1.0), (orig_h / h if h else 1.0)


def _proto_to_blocks(proto_bytes, scale_x, scale_y, min_confidence):
    from proto.chrome_screen_ai_pb2 import VisualAnnotation

    ann = VisualAnnotation()
    ann.ParseFromString(proto_bytes)

    # Chrome Screen AI assigns each line a block_id itself - trust that
    # instead of re-deriving paragraph structure the way
    # ocr_worker.group_lines_into_blocks() has to for Tesseract's flat,
    # unstructured SPARSE_TEXT output.
    by_block = {}
    order = []
    for line in ann.lines:
        text = (line.utf8_string or "").strip()
        if not text:
            continue
        if line.confidence < min_confidence:
            continue

        bb = line.bounding_box
        x0 = int(round(bb.x * scale_x))
        y0 = int(round(bb.y * scale_y))
        x1 = int(round((bb.x + bb.width) * scale_x))
        y1 = int(round((bb.y + bb.height) * scale_y))

        block = by_block.get(line.block_id)
        if block is None:
            block = {"texts": [], "confs": [], "bbox": [x0, y0, x1, y1]}
            by_block[line.block_id] = block
            order.append(line.block_id)
        block["texts"].append(text)
        block["confs"].append(float(line.confidence))
        block["bbox"][0] = min(block["bbox"][0], x0)
        block["bbox"][1] = min(block["bbox"][1], y0)
        block["bbox"][2] = max(block["bbox"][2], x1)
        block["bbox"][3] = max(block["bbox"][3], y1)

    blocks = []
    for block_id in order:
        b = by_block[block_id]
        bx0, by0, bx1, by1 = b["bbox"]
        blocks.append(
            {
                "id": block_id,
                "text": " ".join(b["texts"]),
                # Rescaled to 0-100 to match ocr_worker.py's existing
                # Tesseract confidence convention (conf_threshold defaults,
                # region_tracker comparisons, etc. all assume 0-100).
                "conf": round(min(b["confs"]) * 100.0, 1),
                "bbox": {"x0": bx0, "y0": by0, "x1": bx1, "y1": by1},
            }
        )
    return blocks


def run_oneshot(image_path, model_dir, min_confidence):
    debug = []
    try:
        engine = ScreenAIEngine(model_dir)
        debug.append(f"max_dim={engine.max_dim}")
        rgba, w, h, sx, sy = _load_rgba(image_path, engine.max_dim)
        debug.append(f"image={w}x{h} scale=({sx:.3f},{sy:.3f})")
        proto = engine.perform(rgba, w, h)
        if proto is None:
            return {"error": "PerformOCR returned null", "blocks": [], "debug": debug}
        blocks = _proto_to_blocks(proto, sx, sy, min_confidence)
        return {"error": None, "blocks": blocks, "debug": debug}
    except Exception as exc:
        debug.append(traceback.format_exc())
        return {"error": str(exc), "blocks": [], "debug": debug}


def _write_line(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def worker_main():
    """Long-lived worker so repeated discovery calls skip the .so/model load
    cost. Loading the engine per call would reintroduce exactly the kind of
    fixed startup delay this project measured out of the capture path
    before (see project_playtranslate_speed) - not acceptable for an engine
    meant to run on every discovery pass.

      >>> {"type":"init","model_dir":"...","min_confidence":0.5}
      <<< {"type":"ready","error":null}
      >>> {"type":"recognize","image_path":"/tmp/x.png","min_confidence":0.3}
      <<< {"type":"result","error":null,"blocks":[...]}
      >>> {"type":"shutdown"}

    "min_confidence" on a "recognize" message overrides the init default for
    that call only - capture_dynamic.py retries discovery at a lower
    threshold when the first pass finds nothing (see its
    conf_threshold_retry), and that retry has to actually reach the engine
    per-call, not just get silently ignored because the worker latched a
    fixed threshold at startup.
    """
    engine = None
    default_min_confidence = 0.5

    for raw_line in sys.stdin:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            msg = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            _write_line({"type": "result", "error": f"bad json: {exc}", "blocks": []})
            continue

        mtype = msg.get("type")
        if mtype == "init":
            try:
                engine = ScreenAIEngine(msg["model_dir"])
                default_min_confidence = float(msg.get("min_confidence", 0.5))
                _write_line({"type": "ready", "error": None})
            except Exception as exc:
                _write_line({"type": "ready", "error": f"{exc}\n{traceback.format_exc()}"})
        elif mtype == "recognize":
            try:
                if engine is None:
                    _write_line({"type": "result", "error": "not initialized", "blocks": []})
                    continue
                image_path = msg.get("image_path", "")
                if not image_path or not os.path.exists(image_path):
                    _write_line({"type": "result", "error": "image not found", "blocks": []})
                    continue
                call_min_confidence = float(msg.get("min_confidence", default_min_confidence))
                rgba, w, h, sx, sy = _load_rgba(image_path, engine.max_dim)
                proto = engine.perform(rgba, w, h)
                if proto is None:
                    _write_line({"type": "result", "error": "PerformOCR returned null", "blocks": []})
                    continue
                blocks = _proto_to_blocks(proto, sx, sy, call_min_confidence)
                _write_line({"type": "result", "error": None, "blocks": blocks})
            except Exception as exc:
                _write_line({"type": "result", "error": str(exc), "blocks": [], "trace": traceback.format_exc()})
        elif mtype == "shutdown":
            break
        else:
            _write_line({"type": "result", "error": f"unknown type: {mtype}", "blocks": []})


def main():
    if _WORKER_MODE:
        worker_main()
        return
    if len(sys.argv) < 4:
        print(json.dumps({
            "error": "usage: ocr_screenai.py <image> <model_dir> <min_confidence>",
            "blocks": [],
        }))
        sys.exit(1)
    print(json.dumps(run_oneshot(sys.argv[1], sys.argv[2], float(sys.argv[3]))))


if __name__ == "__main__":
    main()
