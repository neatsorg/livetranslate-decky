"""Smoke test: Windows' own built-in OCR (Windows.Media.Ocr, via the winrt
package) against a real dxcam capture, to compare against chrome_screen_ai.dll
(ocr_screenai_test.py) for both feasibility and speed - the user's reason for
wanting this as an alternative engine is that it's supposedly fast.

Only "ja" is installed as an available recognizer language on this laptop
(`OcrEngine.available_recognizer_languages`, checked live 2026-08-26) - the
OS's OCR language packs are separate opt-in components per language
(Settings > Time & language > Language > <lang> > Optional features > OCR),
independent of display/input language. English text (e.g. NORCO) would need
the English OCR component installed first; this test uses whatever's on
screen in Japanese to validate the mechanism itself.
"""
import asyncio
import sys
import time

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


async def run_ocr(bgra_bytes, width, height, language_tag="ja"):
    from winrt.windows.globalization import Language
    from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.security.cryptography import CryptographicBuffer

    engine = OcrEngine.try_create_from_language(Language(language_tag))
    if engine is None:
        raise RuntimeError(f"no OCR engine available for language {language_tag!r}")

    bitmap = SoftwareBitmap(BitmapPixelFormat.BGRA8, width, height, BitmapAlphaMode.IGNORE)
    buffer = CryptographicBuffer.create_from_byte_array(bytearray(bgra_bytes))
    bitmap.copy_from_buffer(buffer)

    t0 = time.monotonic()
    result = await engine.recognize_async(bitmap)
    dt = time.monotonic() - t0
    return result, dt


def main():
    import dxcam
    import numpy as np

    camera = dxcam.create(device_idx=0, output_idx=0, output_color="BGRA")
    frame = None
    for attempt in range(5):
        frame = camera.grab()
        if frame is not None and float(np.mean(frame)) > 0.5:
            break
        time.sleep(0.1)
    if frame is None:
        print("capture failed")
        sys.exit(1)

    h, w = frame.shape[0], frame.shape[1]
    print(f"captured {w}x{h}")
    bgra_bytes = frame.tobytes()

    result, dt = asyncio.run(run_ocr(bgra_bytes, w, h))
    print(f"recognize_async took {dt:.3f}s")
    print(f"text_angle={result.text_angle}")
    print(f"=== {len(result.lines)} lines ===")
    for line in result.lines:
        words_info = [(word.text, word.bounding_rect) for word in line.words]
        print(f"  line text={line.text!r}")
        for text, rect in words_info:
            print(f"    word={text!r} rect=({rect.x:.0f},{rect.y:.0f},{rect.width:.0f},{rect.height:.0f})")


if __name__ == "__main__":
    main()
