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
    def __init__(self, hide_during_screenshot=None, capture_worker=None, window_title_override=None):
        from PySide6.QtWidgets import (
            QDialog, QDialogButtonBox, QLabel, QPushButton, QSlider, QVBoxLayout,
        )
        from PySide6.QtCore import Qt

        # _grab_screenshot() reads window_title from settings_store.load()
        # (disk), not from whatever's currently typed into Settings' own
        # form field - so opening this dialog right after picking a target
        # window in Settings, but before clicking OK, silently took the
        # screenshot against the *previously saved* target (the whole
        # screen, on a fresh install) instead. Passed in explicitly from
        # SettingsDialog's live form value rather than fixed by having this
        # dialog write Settings' not-yet-confirmed field to disk early,
        # which would also raise its own question of whether that write
        # should survive Settings then being Cancelled. None in standalone
        # use, where there's no separate form holding a newer value anyway.
        self._window_title_override = window_title_override

        # When given (the real tray-app path), _grab_screenshot() submits
        # its grab through this CaptureWorker's own dedicated dxcam thread
        # instead of creating a separate dxcam camera and calling it
        # directly from the Qt main thread here - see CaptureWorker's class
        # docstring (pipeline_loop.py) for why that's a real, previously
        # confirmed hazard (dxcam/DXGI calls off their one dedicated thread
        # have caused a permanent freeze before), not just unnecessary
        # caution. None in standalone use (`python roi_crop_dialog.py`),
        # where there's no other thread already using dxcam to collide with.
        self._capture_worker = capture_worker

        # Windows (e.g. the Settings dialog this is normally opened from)
        # whose own on-screen presence would otherwise get baked into the
        # screenshot below - hidden for the moment of each grab, then
        # restored. Defaults to none for standalone use (`python
        # roi_crop_dialog.py`), where there's no parent dialog to worry
        # about. This dialog's own window is *also* hidden during capture
        # (see _grab_screenshot()) - it's every bit as much "something
        # covering the game" as the Settings dialog is, just not passed in
        # here since this object already has a reference to itself.
        self._hide_during_screenshot = list(hide_during_screenshot or [])

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
        # hidden is built up (appended to) *inside* _hide_for_capture() as
        # it goes, not returned only at the end - so if it raises partway
        # through (e.g. the 2nd of 2 windows' DwmSetWindowAttribute/
        # ShowWindow calls throws), this still reflects whichever window(s)
        # were actually hidden before that, and _restore_after_capture()
        # below can still show them back rather than leaving them hidden
        # forever with nothing left holding a reference to them.
        hidden = []
        try:
            self._hide_for_capture(hidden)
            pixmap = self._grab_screenshot()
            self.preview.set_pixmap(pixmap)
            self.status_label.setText("")
        except Exception as exc:
            self.status_label.setText(t("roi.screenshot_failed", error=exc))
            print(f"[roi_crop] screenshot failed: {exc}")
        finally:
            self._restore_after_capture(hidden)

    def _hide_for_capture(self, hidden):
        """Hides this dialog and whatever else was passed in as
        hide_during_screenshot - only the ones actually visible right now,
        so this is a no-op the very first time a screenshot is taken (this
        dialog's own __init__ calls _retake_screenshot() before the caller
        has called .exec() on it yet, so it isn't shown yet at that point -
        the Settings dialog it was opened from still is, though, and still
        gets hidden here).

        Uses win32gui.ShowWindow() directly on the native HWND, not this
        widget's own Qt-level .hide()/.show() - confirmed live 2026-08-30
        that Qt's own hide()/show() on hide_during_screenshot's dialog (a
        *different*, independently application-modal QDialog, exec()'ing
        its own event loop concurrently with this one, with no Qt parent-
        child relationship between the two - both are plain QDialog(), no
        parent passed) corrupts Qt's own modal/activation bookkeeping badly
        enough that the other dialog reproducibly reactivates itself and
        this one stops responding to its own OK/Cancel afterward, forcing
        the whole app to be quit from the tray to recover. Toggling the raw
        WS_VISIBLE style at the Win32 level instead never touches Qt's own
        widget-visibility state machine at all, so there's nothing for it
        to get confused about - Qt never even finds out.

        Disables DWM's show/hide transition for each window before hiding it
        (DWMWA_TRANSITIONS_FORCEDISABLED) - confirmed live 2026-08-30 that
        without this, the hidden dialog still showed up semi-transparent in
        its own "clean" screenshot: SW_HIDE alone doesn't make DWM cut the
        window's cross-fade instantly, and the capture landed mid-fade
        rather than after it (increasing the delay below wouldn't have
        fixed this reliably either - the fade duration itself isn't
        something this code controls, so there's no delay guaranteed long
        enough short of disabling the fade). Also gives DWM ~50ms before the
        grab in _grab_screenshot() as a smaller safety margin for ordinary
        frame-composition latency on top of that."""
        import ctypes
        import time

        import win32con
        import win32gui

        DWMWA_TRANSITIONS_FORCEDISABLED = 3
        disable = ctypes.c_int(1)

        candidates = [w for w in [self.dialog] + self._hide_during_screenshot if w.isVisible()]
        for w in candidates:
            hwnd = int(w.winId())
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, DWMWA_TRANSITIONS_FORCEDISABLED, ctypes.byref(disable), ctypes.sizeof(disable)
            )
            win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
            hidden.append(w)  # only after it's actually hidden, not before
        if hidden:
            time.sleep(0.05)

    def _restore_after_capture(self, hidden):
        import ctypes

        import win32con
        import win32gui

        DWMWA_TRANSITIONS_FORCEDISABLED = 3
        enable = ctypes.c_int(0)

        for w in hidden:
            # One window's restore failing (either call - unlikely, but
            # these are raw Win32 calls with no guarantee) shouldn't skip
            # restoring the *other* one, same reasoning as
            # _hide_for_capture()'s per-window bookkeeping.
            try:
                hwnd = int(w.winId())
                win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
                # Re-enable transitions for this window afterward - the
                # force-disable in _hide_for_capture() is only meant to
                # cover this one capture, not to permanently strip this
                # window's normal show/hide fade for the rest of its life.
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, DWMWA_TRANSITIONS_FORCEDISABLED, ctypes.byref(enable), ctypes.sizeof(enable)
                )
            except Exception as exc:
                print(f"[roi_crop] failed to restore a hidden window: {exc}")

    def _grab_screenshot(self):
        """One-off dxcam grab matching the currently-configured capture
        target (window_title if set, else the whole monitor) - reads
        settings fresh each call so this reflects whatever's saved, not
        just what was loaded when the dialog opened.

        Goes through self._capture_worker (CaptureWorker.grab(), which
        submits to its own dedicated dxcam thread) when one was passed in,
        rather than creating and calling a separate dxcam camera directly
        from this method's own (Qt main) thread. Not what actually fixed
        the "hidden dialog still visible in its own screenshot" bug (that
        was the DWM transition disable in _hide_for_capture() - the frame
        this method got back was equally stale either way, on-thread or
        off) - kept anyway on separate grounds: CaptureWorker's own class
        docstring documents dxcam/DXGI calls off their one dedicated thread
        as a previously confirmed hazard elsewhere in this app (a
        reproducible freeze), and there's no reason to reintroduce that
        pattern here just because it wasn't *this* bug's cause too."""
        from window_finder import ensure_dpi_aware, find_window_rect

        ensure_dpi_aware()
        capture_cfg = settings_store.load().get("capture", {})
        if self._window_title_override is not None:
            window_title = self._window_title_override.strip()
        else:
            window_title = capture_cfg.get("window_title", "").strip()

        if self._capture_worker is not None:
            camera_width, camera_height = self._capture_worker.width, self._capture_worker.height
            grab = self._capture_worker.grab
        else:
            # Standalone use only (`python roi_crop_dialog.py`) - no
            # CaptureWorker exists to submit through, and nothing else on
            # this process is touching dxcam yet either, so calling it
            # directly here carries none of the cross-thread risk above.
            import dxcam

            camera = dxcam.create(
                device_idx=capture_cfg.get("device_idx", 0), output_idx=capture_cfg.get("output_idx", 0),
                output_color="RGB",
            )
            camera_width, camera_height = camera.width, camera.height
            grab = camera.grab

        region = None
        if window_title:
            rect = find_window_rect(window_title)
            if rect is not None:
                region = (
                    max(0, rect[0]), max(0, rect[1]),
                    min(camera_width, rect[2]), min(camera_height, rect[3]),
                )
        frame = grab(region=region) if region else grab()
        if frame is None:
            raise RuntimeError("grab() returned None")

        from PySide6.QtGui import QImage, QPixmap

        h, w, channels = frame.shape
        if channels == 4:
            # BGRA, 4 bytes/px - matches QImage.Format_RGB32's in-memory byte
            # order on this little-endian platform exactly (0xffRRGGBB packed
            # as B,G,R,A bytes), alpha byte simply ignored on display. This
            # is what CaptureWorker's camera always is (output_color="BGRA");
            # the standalone fallback above still requests "RGB" (3 bytes/px)
            # and gets it, since there's no other camera for dxcam.create()
            # to have already cached under that device/output there.
            image = QImage(frame.tobytes(), w, h, w * 4, QImage.Format_RGB32)
        else:
            image = QImage(frame.tobytes(), w, h, w * 3, QImage.Format_RGB888)
        return QPixmap.fromImage(image)

    def _save(self, enable):
        capture_cfg = self.settings.setdefault("capture", {})
        # Also persist window_title_override here, not just use it for the
        # screenshot preview - confirmed live 2026-08-30: this dialog saves
        # immediately on its own Save/Save-and-enable, independent of the
        # parent Settings dialog's own OK/Cancel (same as Keybindings' own
        # independent save). Without this, picking a new target window in
        # Settings, opening this dialog (which correctly previews *that*
        # window and computes fixed_roi's percentages against it), saving,
        # then Cancelling Settings left window_title on disk as the *old*
        # target - fixed_roi's percentages, chosen by looking at the new
        # window, would silently crop the wrong region against whatever the
        # old target's window shape happens to be.
        if self._window_title_override is not None:
            capture_cfg["window_title"] = self._window_title_override.strip()
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
