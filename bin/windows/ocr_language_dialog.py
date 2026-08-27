"""Small dialog for installing Windows' per-language OCR components
(windows_ocr_lang.py) - one row per known locale, an install button that
triggers a real UAC prompt for the user to approve themselves.

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


_ACTIVE_WORKERS = []  # see _InstallWorker.start()


class _InstallWorker:
    """Runs windows_ocr_lang.install() (which blocks on the elevated
    process, up to several minutes) on a QThread so the UAC prompt and
    download don't freeze this dialog.

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

    def __init__(self, locale):
        from PySide6.QtCore import QObject, QThread, Signal

        class Worker(QObject):
            finished = Signal(bool, str)

            def run(self):
                ok, message = windows_ocr_lang.install(locale)
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

        states = windows_ocr_lang.list_states()
        for row, locale in enumerate(windows_ocr_lang.KNOWN_LOCALES):
            state = states.get(locale)
            self.grid.addWidget(QLabel(windows_ocr_lang.locale_label(locale)), row, 0)
            status_label = QLabel(t("ocr_lang.installed") if state == "Installed" else t("ocr_lang.not_installed"))
            self.grid.addWidget(status_label, row, 1)
            button = QPushButton(t("ocr_lang.install_button"))
            button.setEnabled(state != "Installed")
            button.clicked.connect(lambda _checked, loc=locale: self._on_install_clicked(loc))
            self.grid.addWidget(button, row, 2)
            self.rows[locale] = (status_label, button)

    def _on_install_clicked(self, locale):
        status_label, button = self.rows[locale]
        button.setEnabled(False)
        status_label.setText(t("ocr_lang.installing_status"))

        self._installing_count += 1
        self._close_button.setEnabled(False)  # see _InstallWorker.start()'s comment on why this matters

        worker = _InstallWorker(locale)

        def on_finished(ok, message):
            if ok:
                status_label.setText(t("ocr_lang.installed"))
            else:
                status_label.setText(t("ocr_lang.install_failed_prefix", message=message))
                button.setEnabled(True)
            print(f"[ocr_lang] install({locale}) ok={ok} message={message}")
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
