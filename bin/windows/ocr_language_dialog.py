"""Small dialog for installing/removing Windows' per-language OCR components
(windows_ocr_lang.py) - one row per known locale, a single button per row
that reads Install or Remove depending on current state and triggers a real
UAC prompt for the user to approve themselves either way. One button rather
than two side by side, to keep the dialog narrow - only one of the two
actions ever makes sense for a given row at a time anyway.

Standalone-runnable: `python ocr_language_dialog.py`.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # for windows_ocr_lang

if sys.platform == "win32":
    from winlog import setup_stdio

    setup_stdio("ocr_language_dialog.log")

import windows_ocr_lang
from i18n import t


_ACTIVE_WORKERS = []  # see _ActionWorker.start()


class _ActionWorker:
    """Runs windows_ocr_lang.install() or .uninstall() (either blocks on an
    elevated process, up to several minutes) on a QThread so the UAC prompt
    and download/removal don't freeze this dialog.

    Routes through a Relay QObject rather than connecting straight to
    on_finished - see keybindings_dialog.py's _CaptureWorker (identical
    shape) for the full story: explicitly forcing Qt.QueuedConnection on a
    signal-to-plain-closure connection looked like it fixed the sibling's
    crash, but confirmed live via an explicit thread check that on_finished
    kept running on the *worker* thread regardless - a bare closure isn't
    enough of a QObject receiver for Qt to route the queued delivery
    correctly. worker.finished -> relay.fire (a real QObject with correct
    main-thread affinity) *does* get auto-detected and queued properly;
    relay.fire -> on_finished then only ever fires once already on the main
    thread, so that second connection is safely same-thread regardless."""

    def __init__(self, action_fn, locale):
        from PySide6.QtCore import QObject, QThread, Signal

        class Worker(QObject):
            finished = Signal(bool, str)

            def run(self):
                ok, message = action_fn(locale)
                self.finished.emit(ok, message)

        class Relay(QObject):
            fire = Signal(bool, str)

        self.thread = QThread()
        self.worker = Worker()
        self.relay = Relay()  # deliberately NOT moved to self.thread - stays on the main thread
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.finished.connect(self.relay.fire)

    def start(self, on_finished):
        # Same crash risk as keybindings_dialog.py's _CaptureWorker, worse
        # here since an install can run for minutes (elevated DISM +
        # download) - the dialog's Close button had no guard against being
        # clicked mid-install, and letting this worker (and its QThread) get
        # garbage-collected while still running is unsafe. Keeping it alive
        # here independent of the dialog means closing the dialog is always
        # safe regardless - the install just keeps running to completion in
        # the background and cleans itself up via thread.finished.
        _ACTIVE_WORKERS.append(self)
        self.thread.finished.connect(lambda: _ACTIVE_WORKERS.remove(self) if self in _ACTIVE_WORKERS else None)

        self.relay.fire.connect(on_finished)
        self.worker.finished.connect(self.thread.quit)
        self.thread.start()


class OcrLanguageDialog:
    def __init__(self):
        from PySide6.QtWidgets import QDialog, QDialogButtonBox, QGridLayout, QLabel, QPushButton, QVBoxLayout

        self.dialog = QDialog()
        self.dialog.setWindowTitle(t("ocr_lang.title"))
        self.dialog.setMinimumWidth(360)
        if "--force-topmost" in sys.argv:
            from PySide6.QtCore import Qt
            self.dialog.setWindowFlags(self.dialog.windowFlags() | Qt.WindowStaysOnTopHint)

        layout = QVBoxLayout(self.dialog)
        layout.addWidget(QLabel(t("ocr_lang.intro")))

        self.grid = QGridLayout()
        layout.addLayout(self.grid)
        self.rows = {}  # locale -> (status_label, install_button)
        self._installing_count = 0

        self._build_rows()

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.dialog.reject)
        buttons.accepted.connect(self.dialog.accept)
        layout.addWidget(buttons)
        self._close_button = buttons.button(QDialogButtonBox.Close)

    def _build_rows(self):
        from PySide6.QtWidgets import QLabel, QPushButton

        for row, locale in enumerate(windows_ocr_lang.KNOWN_LOCALES):
            installed = windows_ocr_lang.is_installed(locale)
            self.grid.addWidget(QLabel(windows_ocr_lang.locale_label(locale)), row, 0)
            status_label = QLabel(t("ocr_lang.installed") if installed else t("ocr_lang.not_installed"))
            self.grid.addWidget(status_label, row, 1)
            button = QPushButton(t("ocr_lang.uninstall_button") if installed else t("ocr_lang.install_button"))
            button.clicked.connect(lambda _checked, loc=locale: self._on_action_clicked(loc))
            self.grid.addWidget(button, row, 2)
            self.rows[locale] = (status_label, button)

    def _on_action_clicked(self, locale):
        # Decided once, at click time - do_install stays fixed for this one
        # run even though is_installed(locale) could in principle change
        # underneath it (it can't, in practice: the button is disabled for
        # the whole run, so this is really just "don't re-derive it from a
        # mutable read after the action already changed that same state").
        do_install = not windows_ocr_lang.is_installed(locale)
        action_fn = windows_ocr_lang.install if do_install else windows_ocr_lang.uninstall

        status_label, button = self.rows[locale]
        button.setEnabled(False)
        status_label.setText(t("ocr_lang.installing_status") if do_install else t("ocr_lang.uninstalling_status"))

        self._installing_count += 1
        self._close_button.setEnabled(False)  # see _ActionWorker.start()'s comment on why this matters

        worker = _ActionWorker(action_fn, locale)

        def on_finished(ok, message):
            installed_now = windows_ocr_lang.is_installed(locale)
            if ok:
                status_label.setText(t("ocr_lang.installed") if installed_now else t("ocr_lang.not_installed"))
            else:
                status_label.setText(t("ocr_lang.install_failed_prefix", message=message))
            button.setText(t("ocr_lang.uninstall_button") if installed_now else t("ocr_lang.install_button"))
            button.setEnabled(True)
            print(f"[ocr_lang] {'install' if do_install else 'uninstall'}({locale}) ok={ok} message={message}")
            self._installing_count -= 1
            self._close_button.setEnabled(self._installing_count == 0)

        worker.start(on_finished)

    def exec(self):
        return self.dialog.exec()


def main():
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    # See keybindings_dialog.py's main() for why this matters here too -
    # same QApplication-tears-down-while-a-worker-QThread-is-still-alive
    # risk, specific to this standalone entry point (tray_app.py already
    # disables this).
    app.setQuitOnLastWindowClosed(False)
    dlg = OcrLanguageDialog()
    dlg.exec()
    for worker in list(_ACTIVE_WORKERS):
        worker.thread.wait(600_000)  # matches windows_ocr_lang.install()'s own timeout_s=600


if __name__ == "__main__":
    main()
