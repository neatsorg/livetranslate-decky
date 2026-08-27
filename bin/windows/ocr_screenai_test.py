"""One-shot smoke test: run Chrome's Screen AI OCR engine (Windows build,
chrome_screen_ai.dll) against a real screenshot. Adapted from
ocr_screenai.py's ScreenAIEngine - same ctypes/Skia-struct approach, only
the DLL filename and (potentially) the loader class differ from Linux.

Usage: python ocr_screenai_test.py <image.png> <model_resources_dir>
"""
import ctypes
import json
import os
import sys
from pathlib import Path

# Windows defaults stdout/file I/O to the system ANSI codepage (e.g. cp932 on
# a Japanese-locale machine), not UTF-8 - OCR/translation text is arbitrary
# Unicode (CJK, Korean, Cyrillic, emoji...) and WILL contain characters
# outside that codepage. Every place this project does text I/O on Windows
# needs this same fix (or PYTHONUTF8=1 set for the process).
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # for proto/

_RGBA_8888 = 4
_ALPHA_OPAQUE = 1

_SIZE_CB = ctypes.CFUNCTYPE(ctypes.c_uint32, ctypes.c_char_p)
_CONTENT_CB = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_uint32, ctypes.c_void_p)


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


class ScreenAIEngine:
    def __init__(self, model_dir, dll_loader=ctypes.CDLL):
        self.model_dir = model_dir
        dll_path = os.path.join(model_dir, "chrome_screen_ai.dll")
        if not os.path.exists(dll_path):
            raise FileNotFoundError(f"chrome_screen_ai.dll not found at {dll_path}")

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

        print(f"loading {dll_path} via {dll_loader.__name__}...")
        self.lib = dll_loader(dll_path)
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
        print("calling InitOCRUsingCallback()...")
        if not self.lib.InitOCRUsingCallback():
            raise RuntimeError("InitOCRUsingCallback() returned false")
        print("init OK")

        self.lib.SetOCRLightMode(False)
        self.max_dim = int(self.lib.GetMaxImageDimension()) or 2048
        print(f"max_dim={self.max_dim}")

    def perform(self, rgba_bytes, width, height):
        bitmap = _SkBitmap()
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


def load_rgba(image_path, max_dim):
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


def proto_to_blocks(proto_bytes, scale_x, scale_y, min_confidence=0.3):
    from proto.chrome_screen_ai_pb2 import VisualAnnotation

    ann = VisualAnnotation()
    ann.ParseFromString(proto_bytes)

    by_block = {}
    order = []
    for line in ann.lines:
        text = (line.utf8_string or "").strip()
        if not text or line.confidence < min_confidence:
            continue
        bb = line.bounding_box
        block = by_block.get(line.block_id)
        if block is None:
            block = {"texts": [], "confs": []}
            by_block[line.block_id] = block
            order.append(line.block_id)
        block["texts"].append(text)
        block["confs"].append(float(line.confidence))

    blocks = []
    for block_id in order:
        b = by_block[block_id]
        blocks.append({
            "id": block_id,
            "text": " ".join(b["texts"]),
            "conf": round(min(b["confs"]) * 100.0, 1),
        })
    return blocks


def main():
    if len(sys.argv) < 3:
        print("usage: ocr_screenai_test.py <image.png> <model_resources_dir>")
        sys.exit(1)
    image_path, model_dir = sys.argv[1], sys.argv[2]

    dll_loader = ctypes.CDLL
    if len(sys.argv) > 3 and sys.argv[3] == "--windll":
        dll_loader = ctypes.WinDLL

    engine = ScreenAIEngine(model_dir, dll_loader=dll_loader)
    rgba, w, h, sx, sy = load_rgba(image_path, engine.max_dim)
    print(f"image={w}x{h} scale=({sx:.3f},{sy:.3f})")

    proto = engine.perform(rgba, w, h)
    if proto is None:
        print("PerformOCR returned null")
        sys.exit(1)

    blocks = proto_to_blocks(proto, sx, sy)
    print(f"=== {len(blocks)} blocks ===")
    print(json.dumps(blocks, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
