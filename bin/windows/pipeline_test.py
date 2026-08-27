"""First end-to-end wiring test: capture (dxcam) -> OCR discovery
(chrome_screen_ai.dll) -> translate (DummyProvider, no network - this test
is about proving the plumbing, not translation quality) -> HUD (PySide6
topmost/click-through overlay), all validated individually in prior test
scripts in this directory.

Deliberately a single discovery pass, not a continuous tracking loop yet -
the Linux side's capture_dynamic.py's ~760-line DynamicCaptureRunner adds
frame-diffing, region tracking, overlay-feedback-loop suppression, and
priority ordering on top of this same basic shape (see
project_playtranslate_windows_port memory) - that's the next increment
after this one is confirmed to hang together, not something to reproduce
in one shot.

Usage: python pipeline_test.py <model_resources_dir> [duration_s]
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # for providers/
sys.path.insert(0, str(Path(__file__).resolve().parent))  # for screenai_engine

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

MIN_CONFIDENCE = 50.0
MIN_TEXT_LEN = 2


def capture_frame():
    import dxcam
    import numpy as np

    camera = dxcam.create(device_idx=0, output_idx=0)
    frame = None
    for attempt in range(5):
        frame = camera.grab()
        if frame is not None and float(np.mean(frame)) > 0.5:
            break
        time.sleep(0.1)
    if frame is None:
        raise RuntimeError("dxcam grab() failed on every attempt")
    return frame


def run_ocr(frame, model_dir):
    from screenai_engine import ScreenAIEngine, load_rgba_from_array, proto_to_blocks

    engine = ScreenAIEngine(model_dir)
    rgba, w, h, sx, sy = load_rgba_from_array(frame, engine.max_dim)
    print(f"OCR input: {w}x{h} (scale back to capture px: {sx:.3f},{sy:.3f})")
    proto = engine.perform(rgba, w, h)
    if proto is None:
        return []
    return proto_to_blocks(proto, sx, sy, min_confidence=0.3)


def translate_blocks(blocks):
    from providers import create_provider

    provider = create_provider("dummy")
    results = []
    for b in blocks:
        text = b["text"].strip()
        if len(text) < MIN_TEXT_LEN or b["conf"] < MIN_CONFIDENCE:
            continue
        translation = provider.translate(
            speaker=None, text=text, target_lang="ja", source_lang="auto",
            profile=None, context_text="",
        )
        results.append({**b, "translation": translation})
    return results


def show_hud(blocks, duration_s):
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication, QLabel, QWidget

    app = QApplication(sys.argv)
    screen = app.primaryScreen()
    dpr = screen.devicePixelRatio()  # physical-px -> logical-px factor (measured, not hardcoded)
    geo = screen.geometry()
    print(f"screen logical={geo.width()}x{geo.height()} devicePixelRatio={dpr}")

    win = QWidget()
    win.setWindowFlags(
        Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.WindowTransparentForInput
    )
    win.setAttribute(Qt.WA_TranslucentBackground, True)
    win.setGeometry(0, 0, geo.width(), geo.height())

    labels = []
    for b in blocks:
        x0, y0, x1, y1 = b["bbox"]
        lx0, ly0 = x0 / dpr, y0 / dpr
        lw = max((x1 - x0) / dpr, 40)
        label = QLabel(win)
        label.setText(b["translation"])
        label.setFont(QFont("Yu Gothic UI", 12))
        label.setStyleSheet(
            "color: white; background-color: rgba(20, 20, 20, 200); padding: 4px;"
        )
        label.setWordWrap(True)
        label.setFixedWidth(int(max(lw, 120)))
        label.move(int(lx0), int(ly0))
        label.adjustSize()
        label.show()
        labels.append(label)

    print(f"HUD showing {len(labels)} translated blocks for {duration_s}s")
    sys.stdout.flush()
    win.showFullScreen()
    QTimer.singleShot(int(duration_s * 1000), app.quit)
    app.exec()
    print("HUD closed")


def main():
    if len(sys.argv) < 2:
        print("usage: pipeline_test.py <model_resources_dir> [duration_s]")
        sys.exit(1)
    model_dir = sys.argv[1]
    duration_s = float(sys.argv[2]) if len(sys.argv) > 2 else 20.0

    t0 = time.monotonic()
    frame = capture_frame()
    print(f"captured {frame.shape} in {time.monotonic() - t0:.2f}s")

    t0 = time.monotonic()
    blocks = run_ocr(frame, model_dir)
    print(f"OCR found {len(blocks)} raw blocks in {time.monotonic() - t0:.2f}s")
    for b in blocks:
        print(f"  [{b['id']}] conf={b['conf']} bbox={b['bbox']} text={b['text']!r}")

    translated = translate_blocks(blocks)
    print(f"{len(translated)} blocks survived filtering + translation")
    for b in translated:
        print(f"  -> {b['translation']!r}")

    if not translated:
        print("nothing to show, exiting")
        return

    show_hud(translated, duration_s)


if __name__ == "__main__":
    main()
