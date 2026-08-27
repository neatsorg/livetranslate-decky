"""Chrome Screen AI OCR engine wrapper for Windows (chrome_screen_ai.dll),
factored out of ocr_screenai_test.py once it moved from a one-off smoke test
into a component the real capture->OCR->translate->HUD pipeline imports.

Same ctypes/Skia-struct approach as the Linux .so version in
../ocr_screenai.py - see that file for why the struct layout mirrors Skia's
C++ ABI. `ctypes.CDLL` (cdecl) is correct here, confirmed live 2026-08-25;
`ctypes.WinDLL` (stdcall) is NOT needed despite this being Windows.
"""
import ctypes
import os
import sys
from pathlib import Path

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
    def __init__(self, model_dir):
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

        self.lib = ctypes.CDLL(dll_path)
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

        self.lib.SetOCRLightMode(False)
        self.max_dim = int(self.lib.GetMaxImageDimension()) or 2048

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


def load_rgba_from_array(rgb_array, max_dim):
    """Like ocr_screenai.py's _load_rgba, but takes an in-memory HxWx3 (or
    HxWx4) array (e.g. straight from dxcam.grab()) instead of reading a PNG
    off disk - the real pipeline never touches disk for the capture frame."""
    from PIL import Image

    img = Image.fromarray(rgb_array)
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
        blocks.append({
            "id": block_id,
            "text": " ".join(b["texts"]),
            "conf": round(min(b["confs"]) * 100.0, 1),
            "bbox": (bx0, by0, bx1, by1),
        })
    return blocks
