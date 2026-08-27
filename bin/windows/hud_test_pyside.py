"""HUD prototype smoke test: a topmost, click-through, translucent overlay
window showing sample translated text, using PySide6 (already installed on
the test laptop) rather than hand-rolled Win32 layered-window ctypes code -
Qt.WindowTransparentForInput maps directly to the WS_EX_TRANSPARENT click-
through behavior we need, and Qt handles DPI/painting/text layout for free.

Stays open for a fixed duration so a separate capture script (dxcam, already
validated) can screenshot the desktop while this is showing, to visually
confirm it actually composites on top of other windows.
"""
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QApplication, QLabel, QWidget


def main():
    duration_s = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0

    app = QApplication(sys.argv)

    win = QWidget()
    win.setWindowFlags(
        Qt.FramelessWindowHint
        | Qt.WindowStaysOnTopHint
        | Qt.Tool  # no taskbar entry
        | Qt.WindowTransparentForInput  # click-through -> WS_EX_TRANSPARENT
    )
    win.setAttribute(Qt.WA_TranslucentBackground, True)

    screen = app.primaryScreen().geometry()
    win.setGeometry(0, 0, screen.width(), screen.height())

    label = QLabel(win)
    label.setText("HUD疎通テスト / HUD smoke test\n翻訳結果はここに表示されます")
    label.setFont(QFont("Yu Gothic UI", 20))
    label.setStyleSheet(
        "color: white; background-color: rgba(0, 0, 0, 180);"
        "padding: 16px; border-radius: 8px;"
    )
    label.adjustSize()
    label.move(60, 60)

    win.showFullScreen()
    print(f"HUD shown, geometry={screen.width()}x{screen.height()}, staying up for {duration_s}s")
    sys.stdout.flush()

    QTimer.singleShot(int(duration_s * 1000), app.quit)
    app.exec()
    print("HUD closed")


if __name__ == "__main__":
    main()
