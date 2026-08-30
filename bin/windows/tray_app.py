"""Tray-resident entry point - the real day-to-day way to run the app, as
opposed to pipeline_loop.py's `main()` (a fixed-duration CLI harness used
for dev testing, e.g. the LTPipelineLoop scheduled task). Reuses the exact
same HUD/PipelineLoop/keybinding-thread setup via pipeline_loop._build_app()/
_shutdown() rather than duplicating it, and adds a QSystemTrayIcon with a
right-click menu (Settings / Exit).

A tray icon isn't just a nicety here - the HUD window is deliberately
frameless/click-through/borderless fullscreen (see pipeline_loop.py), so
without some other visible control surface there would be no way for a user
to open Settings or quit the app at all short of Task Manager.

Usage: pythonw tray_app.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # providers/, region_tracker
sys.path.insert(0, str(Path(__file__).resolve().parent))  # settings_store, i18n, pipeline_loop

if sys.platform == "win32":
    from winlog import setup_stdio

    setup_stdio("tray_app.log")

from i18n import t
from pipeline_loop import _build_app, _shutdown, pause_for_dialog, rebuild_keybinding_matcher, resume_after_dialog, show_toast


def _make_tray_icon():
    """No branded icon asset exists yet (packaging/branding is still an
    open item - see project memory). Draw a simple, recognizable glyph at
    runtime instead of shipping/depending on a placeholder image file."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap

    size = 64
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setBrush(QColor(45, 110, 200))
    painter.setPen(Qt.NoPen)
    painter.drawEllipse(2, 2, size - 4, size - 4)
    painter.setPen(QColor(255, 255, 255))
    painter.setFont(QFont("Yu Gothic UI", 30, QFont.Bold))
    painter.drawText(pixmap.rect(), Qt.AlignCenter, "訳")
    painter.end()
    return QIcon(pixmap)


def main():
    handles = _build_app()
    app = handles.app
    # The HUD QWidget counts as a top-level window to Qt, but closing it
    # isn't how this app is meant to quit - only the tray's Exit item is.
    app.setQuitOnLastWindowClosed(False)

    from PySide6.QtWidgets import QMenu, QSystemTrayIcon

    tray = QSystemTrayIcon(_make_tray_icon(), app)
    tray.setToolTip(t("app_name"))

    menu = QMenu()

    def _open_settings():
        from settings_dialog import SettingsDialog

        # Stops the main tick timer and the background keybinding-polling
        # thread for the whole time Settings (and anything it opens, e.g.
        # Keybindings/ROI/OCR-language - all opened in-process, synchronously,
        # from within this same dlg.exec() call) is on screen. Not optional
        # tidiness - see pause_for_dialog()'s docstring for the real Qt
        # access-violation crash this prevents.
        pause_for_dialog(handles)
        try:
            # Passed through to RoiCropDialog so its screenshot grabs reuse
            # CaptureWorker's own dedicated dxcam thread instead of touching
            # dxcam directly from the Qt main thread - see CaptureWorker's
            # class docstring for why that's a real hazard (a previously
            # confirmed live freeze from calling dxcam off its one dedicated
            # thread), not just a style preference.
            dlg = SettingsDialog(capture_worker=handles.pipeline.camera)
            dlg.exec()
        finally:
            resume_after_dialog(handles)
        # Reload unconditionally, not just when the dialog itself returns
        # Accepted - its Keybindings/ROI sub-dialogs save straight to
        # settings.json themselves the moment their own Save button is
        # clicked, independent of whether the *parent* Settings dialog is
        # later OK'd or Cancelled. Re-reading here is cheap and correct
        # either way (a no-op-equivalent if nothing actually changed).
        try:
            handles.pipeline.reload_settings()
            rebuild_keybinding_matcher(handles)
        except Exception as exc:
            print(f"[tray] settings reload failed: {exc}")
            tray.showMessage(t("app_name"), t("tray.reload_failed", error=exc), QSystemTrayIcon.Warning)

    settings_action = menu.addAction(t("tray.settings"))
    settings_action.triggered.connect(_open_settings)

    def _toggle_capture():
        pipeline = handles.pipeline
        if pipeline.paused and (pipeline.provider is None or pipeline.engine is None or pipeline.camera is None):
            # Confirmed real via code review 2026-08-28: without this
            # check, clicking "Start Capture" before Settings has a
            # working provider/OCR engine/capture device still flipped
            # paused=False and showed a "Resumed" toast, even though
            # tick()'s own "not configured" early-return means literally
            # nothing happens afterward - a real, if non-crashing,
            # misleading UX (the toast says success, the menu label then
            # reads "Pause" as if it's running, but no capture ever
            # occurs). Left in the paused state and tell the user why,
            # instead of silently pretending the toggle worked.
            tray.showMessage(t("app_name"), t("tray.not_configured"), QSystemTrayIcon.Warning)
            return
        # Reuses PipelineLoop.toggle_paused() as-is - the exact same
        # method the pad/keyboard pause_resume binding already calls, so
        # pausing/resuming from the tray behaves identically (immediate
        # hide on pause, force_discovery+stale-one-shot-timer-cancel on
        # resume) rather than introducing a second, parallel pause code
        # path that could drift from the tested one.
        pipeline.toggle_paused()
        show_toast(handles, t("toast.paused") if pipeline.paused else t("toast.resumed"))

    capture_action = menu.addAction(t("tray.start_capture"))
    capture_action.triggered.connect(_toggle_capture)

    def _update_capture_action_label():
        # Refreshed right before the menu actually opens (not just once at
        # startup) so it can't go stale if paused state changes via the
        # keybinding instead of this same menu - aboutToShow fires on
        # every right-click, cheap enough to not matter at that frequency.
        capture_action.setText(t("tray.start_capture") if handles.pipeline.paused else t("tray.pause_capture"))

    menu.aboutToShow.connect(_update_capture_action_label)

    menu.addSeparator()

    quit_action = menu.addAction(t("tray.quit"))
    quit_action.triggered.connect(app.quit)

    tray.setContextMenu(menu)
    tray.show()

    if handles.pipeline.provider is None or handles.pipeline.engine is None or handles.pipeline.camera is None:
        # A fresh install's default provider/OCR engine may not be usable
        # yet (see PipelineLoop._configure_from_settings()'s docstring -
        # this used to crash the whole app before the tray icon even
        # existed). Same reasoning now covers self.camera - CaptureWorker's
        # dxcam device init can fail/time out too (bad device_idx/
        # output_idx, or a genuine capture-device error), and the pipeline
        # just quietly skips ticks until that's fixed. Surface it so the
        # user actually notices why nothing is happening instead of
        # wondering why translations never appear.
        tray.showMessage(t("app_name"), t("tray.not_configured"), QSystemTrayIcon.Warning)

    print("[tray] icon shown - right-click for Settings/Exit")
    sys.stdout.flush()
    app.exec()
    _shutdown(handles)


if __name__ == "__main__":
    main()
