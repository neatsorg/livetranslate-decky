"""Settings UI for fixed-ROI capture mode - a PySide6 port of RoiCrop.tsx's
design (a screenshot with a live rectangle preview + four %-based sliders
for left/top/width/height, deliberately not a draggable editor since this
mode only ever tracks one region, so there's nothing to add/remove/name).

Standalone-runnable: `python roi_crop_dialog.py`.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # settings_store, window_finder

if sys.platform == "win32":
    from winlog import setup_stdio

    setup_stdio("roi_crop_dialog.log")

import settings_store
from i18n import t

DEFAULT_ROI = {"x_pct": 20.0, "y_pct": 60.0, "width_pct": 58.0, "height_pct": 36.0}


def _clamp(value, lo, hi):
    return max(lo, min(hi, value))


class RoiPreviewWidget:
    """Not a QWidget subclass at import time (this module needs to be
    importable without PySide6 already initialized) - built lazily in
    RoiCropDialog.__init__ instead, same reasoning as SettingsDialog."""

    @staticmethod
    def build():
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QColor, QPainter, QPen
        from PySide6.QtWidgets import QWidget

        class _Widget(QWidget):
            def __init__(self):
                super().__init__()
                self.pixmap = None
                self.roi = dict(DEFAULT_ROI)
                self.setMinimumHeight(320)

            def set_pixmap(self, pixmap):
                self.pixmap = pixmap
                self.update()

            def set_roi(self, roi):
                self.roi = roi
                self.update()

            def paintEvent(self, _event):
                painter = QPainter(self)
                if self.pixmap is None or self.pixmap.isNull():
                    painter.fillRect(self.rect(), QColor(40, 40, 40))
                    painter.setPen(QColor(200, 200, 200))
                    painter.drawText(self.rect(), Qt.AlignCenter, t("roi.no_screenshot"))
                    return
                scaled = self.pixmap.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
                x_off = (self.width() - scaled.width()) // 2
                y_off = (self.height() - scaled.height()) // 2
                painter.drawPixmap(x_off, y_off, scaled)

                rx = x_off + scaled.width() * self.roi["x_pct"] / 100
                ry = y_off + scaled.height() * self.roi["y_pct"] / 100
                rw = scaled.width() * self.roi["width_pct"] / 100
                rh = scaled.height() * self.roi["height_pct"] / 100
                painter.setPen(QPen(QColor(79, 195, 247), 2))
                painter.setBrush(QColor(79, 195, 247, 40))
                painter.drawRect(int(rx), int(ry), int(rw), int(rh))

        return _Widget()


class RoiCropDialog:
    def __init__(self):
        from PySide6.QtWidgets import (
            QDialog, QDialogButtonBox, QLabel, QPushButton, QSlider, QVBoxLayout,
        )
        from PySide6.QtCore import Qt

        self.settings = settings_store.load()
        capture_cfg = self.settings.get("capture", {})
        self.roi = dict(capture_cfg.get("fixed_roi") or DEFAULT_ROI)

        self.dialog = QDialog()
        self.dialog.setWindowTitle(t("roi.title"))
        self.dialog.resize(640, 620)
        if "--force-topmost" in sys.argv:
            self.dialog.setWindowFlags(self.dialog.windowFlags() | Qt.WindowStaysOnTopHint)

        layout = QVBoxLayout(self.dialog)
        layout.addWidget(QLabel(t("roi.intro")))

        self.preview = RoiPreviewWidget.build()
        self.preview.set_roi(self.roi)
        layout.addWidget(self.preview)

        retake_btn = QPushButton(t("roi.retake"))
        retake_btn.clicked.connect(self._retake_screenshot)
        layout.addWidget(retake_btn)

        self.sliders = {}
        for key, label_text, minimum in (
            ("x_pct", t("roi.left"), 0),
            ("y_pct", t("roi.top"), 0),
            ("width_pct", t("roi.width"), 1),
            ("height_pct", t("roi.height"), 1),
        ):
            row_label = QLabel(f"{label_text}: {self.roi[key]:.0f}%")
            layout.addWidget(row_label)
            slider = QSlider(Qt.Horizontal)
            slider.setMinimum(minimum)
            slider.setMaximum(100)
            slider.setValue(int(self.roi[key]))
            slider.valueChanged.connect(lambda value, k=key, lbl=row_label: self._on_slider(k, value, lbl))
            layout.addWidget(slider)
            self.sliders[key] = slider

        self.status_label = QLabel("")
        layout.addWidget(self.status_label)

        buttons_row = QDialogButtonBox()
        self.save_btn = buttons_row.addButton(t("roi.save"), QDialogButtonBox.AcceptRole)
        self.enable_btn = buttons_row.addButton(t("roi.save_and_enable"), QDialogButtonBox.AcceptRole)
        self.disable_btn = buttons_row.addButton(t("roi.disable"), QDialogButtonBox.ActionRole)
        close_btn = buttons_row.addButton(t("roi.close"), QDialogButtonBox.RejectRole)
        self.save_btn.clicked.connect(lambda: self._save(enable=False))
        self.enable_btn.clicked.connect(lambda: self._save(enable=True))
        self.disable_btn.clicked.connect(self._disable)
        close_btn.clicked.connect(self.dialog.reject)
        layout.addWidget(buttons_row)

        self._retake_screenshot()

    def _on_slider(self, key, value, label_widget):
        self.roi[key] = float(value)
        # Same clamping RoiCrop.tsx's updateRoi() does - width/height can't
        # push the region past the right/bottom edge.
        self.roi["x_pct"] = _clamp(self.roi["x_pct"], 0, 99)
        self.roi["y_pct"] = _clamp(self.roi["y_pct"], 0, 99)
        self.roi["width_pct"] = _clamp(self.roi["width_pct"], 1, 100 - self.roi["x_pct"])
        self.roi["height_pct"] = _clamp(self.roi["height_pct"], 1, 100 - self.roi["y_pct"])
        label_widget.setText(f"{label_widget.text().split(':')[0]}: {self.roi[key]:.0f}%")
        self.preview.set_roi(self.roi)

    def _retake_screenshot(self):
        try:
            pixmap = self._grab_screenshot()
            self.preview.set_pixmap(pixmap)
            self.status_label.setText("")
        except Exception as exc:
            self.status_label.setText(t("roi.screenshot_failed", error=exc))
            print(f"[roi_crop] screenshot failed: {exc}")

    def _grab_screenshot(self):
        """One-off dxcam grab matching the currently-configured capture
        target (window_title if set, else the whole monitor) - reads
        settings fresh each call so this reflects whatever's saved, not
        just what was loaded when the dialog opened."""
        import dxcam

        from window_finder import ensure_dpi_aware, find_window_rect

        ensure_dpi_aware()
        capture_cfg = settings_store.load().get("capture", {})
        camera = dxcam.create(
            device_idx=capture_cfg.get("device_idx", 0), output_idx=capture_cfg.get("output_idx", 0),
            output_color="RGB",
        )
        window_title = capture_cfg.get("window_title", "").strip()
        region = None
        if window_title:
            rect = find_window_rect(window_title)
            if rect is not None:
                region = (
                    max(0, rect[0]), max(0, rect[1]),
                    min(camera.width, rect[2]), min(camera.height, rect[3]),
                )
        frame = camera.grab(region=region) if region else camera.grab()
        if frame is None:
            raise RuntimeError("grab() returned None")

        from PySide6.QtGui import QImage, QPixmap

        h, w = frame.shape[0], frame.shape[1]
        image = QImage(frame.tobytes(), w, h, w * 3, QImage.Format_RGB888)
        return QPixmap.fromImage(image)

    def _save(self, enable):
        capture_cfg = self.settings.setdefault("capture", {})
        # Plain "Save" (enable=False) should update the rectangle if fixed
        # ROI is already the active mode, but not be the thing that flips it
        # on from disabled - that's what the separate "Save and enable"
        # button is for. Previously this branch wrote capture_cfg's
        # *existing* fixed_roi back over itself (a no-op) instead of the
        # just-edited self.roi, so "Save" never actually persisted a slider
        # change unless "Save and enable" was used instead.
        already_enabled = capture_cfg.get("fixed_roi") is not None
        if enable or already_enabled:
            capture_cfg["fixed_roi"] = dict(self.roi)
        settings_store.save(self.settings)
        enabled_now = capture_cfg.get("fixed_roi") is not None
        self.status_label.setText(t("roi.saved_enabled") if enable else t("roi.saved"))
        print(f"[roi_crop] saved roi={self.roi} enabled={enabled_now}")

    def _disable(self):
        capture_cfg = self.settings.setdefault("capture", {})
        capture_cfg["fixed_roi"] = None
        settings_store.save(self.settings)
        self.status_label.setText(t("roi.disabled"))
        print("[roi_crop] fixed_roi disabled")

    def exec(self):
        return self.dialog.exec()


def main():
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    dlg = RoiCropDialog()
    dlg.exec()


if __name__ == "__main__":
    main()
