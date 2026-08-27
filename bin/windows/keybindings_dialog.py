"""Settings UI for keybindings - a PySide6 port of Keybindings.tsx's design
(same command list, same "press a button/key now" capture flow, same
max-3-keys/long-press/threshold/duplicate-and-unset validation), driven by
input_state.py + keybinding_matcher.py instead of hidraw + the Decky RPCs.

Standalone-runnable: `python keybindings_dialog.py`.
"""
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # settings_store, input_state, keybinding_matcher

if sys.platform == "win32":
    from winlog import setup_stdio

    setup_stdio("keybindings_dialog.log")

import settings_store
from i18n import t
from keybinding_matcher import key_set_signature

MAX_KEYS_PER_BINDING = 3
DEFAULT_THRESHOLD_MS = 900

COMMAND_ORDER = ["refresh", "pause_resume"]


def _command_meta(command):
    return {
        "label": t(f"keybindings.command.{command}"),
        "supports_long_press": True,
        "description": t(f"keybindings.command.{command}_desc"),
    }


def _new_binding_id():
    return f"binding_{uuid.uuid4().hex[:12]}"


def _binding_signature(binding):
    return (key_set_signature(binding["keys"]), bool(binding["long_press"]))


_ACTIVE_WORKERS = []  # see _CaptureWorker.start()


class _CaptureWorker:
    """Runs input_state.capture_one() (blocks up to several seconds waiting
    for a press) on a QThread so the dialog doesn't freeze while listening.

    Confirmed live 2026-08-26, in two stages:

    Stage 1 - connecting the worker's finished signal straight to
    on_finished (a plain Python closure, not a bound method of a QObject)
    with no explicit connection type let PySide6's automatic connection-type
    detection guess wrong - it has no QObject/thread affinity to inspect for
    a bare closure - and in practice ran on_finished directly on the
    *worker* thread. Symptoms were nondeterministic: sometimes a genuine
    native access violation (0xc0000005 in Qt6Widgets.dll, per Windows'
    Event Viewer) touching widgets from the wrong thread, sometimes a hung
    "Not Responding" window.

    Stage 2 - explicitly forcing Qt.QueuedConnection on that same connection
    looked like it fixed things (no more crash/hang in several retests), but
    turned out not to have actually fixed the *thread* on_finished ran on at
    all - confirmed by printing `QThread.currentThread() is
    QCoreApplication.instance().thread()` inside on_finished: still False,
    every time, even with QueuedConnection explicitly requested. A bare
    Python closure apparently still isn't enough of a "receiver" for Qt to
    route a queued delivery correctly - it just kept calling on_finished
    directly on the worker thread regardless. This explains why the crash
    stopped reproducing (the specific operations on_finished happened to run
    first - plain dict/attribute mutations, not widget touches - don't
    inherently crash off-thread) while a *new*, quieter symptom appeared:
    _rebuild()'s widget construction, running the whole time on the wrong
    thread, produced a scroll area that stayed visibly blank until the
    dialog was closed and reopened, even though it "completed successfully"
    and the underlying data was correct.

    Real fix: route through _Relay, a genuine QObject created (and left) on
    the main thread. worker.finished -> relay.fire is a signal-to-signal
    connection, which Qt *can* correctly auto-detect as cross-thread (relay
    has real, inspectable thread affinity) and therefore actually queues.
    relay.fire -> on_finished then only ever fires once that queued delivery
    has already landed on the main thread, so this second connection is
    safely same-thread regardless of its own connection type. Confirmed via
    the same thread check now reporting True."""

    def __init__(self, timeout_s=6.0):
        from PySide6.QtCore import QObject, QThread, Signal

        class Worker(QObject):
            finished = Signal(object, object)  # identifier, label (None, None on timeout)

            def run(self):
                import input_state

                identifier, label = input_state.capture_one(timeout_s=timeout_s)
                self.finished.emit(identifier, label)

        class Relay(QObject):
            fire = Signal(object, object)

        self.thread = QThread()
        self.worker = Worker()
        self.relay = Relay()  # deliberately NOT moved to self.thread - stays on the main thread
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.finished.connect(self.relay.fire)

    def start(self, on_finished):
        # Confirmed live 2026-08-26 (separate finding from the threading bug
        # above): the dialog's own Close button had no guard against being
        # clicked while this worker was still capturing. If the dialog (and
        # this _CaptureWorker instance, held only in the dialog's own
        # self._workers list) gets garbage-collected while self.thread is
        # still running, Qt's QThread destructor is unsafe - "QThread:
        # Destroyed while thread is still running" - a real crash risk, not
        # just a warning, on some Qt builds. Registering self in this
        # module-level list keeps the worker (and its QThread) alive
        # independent of the dialog's own lifetime, so closing the dialog
        # immediately is always safe - the capture just keeps running
        # silently in the background (at most ~6s, its own timeout) and
        # cleans itself up here once thread.finished actually fires, whether
        # or not the dialog that started it is still open.
        _ACTIVE_WORKERS.append(self)
        self.thread.finished.connect(lambda: _ACTIVE_WORKERS.remove(self) if self in _ACTIVE_WORKERS else None)

        self.relay.fire.connect(on_finished)
        self.worker.finished.connect(self.thread.quit)
        self.thread.start()


class KeybindingsDialog:
    def __init__(self):
        from PySide6.QtWidgets import QDialog, QDialogButtonBox, QScrollArea, QVBoxLayout, QWidget

        self.settings = settings_store.load()
        self.bindings = [dict(b, keys=list(b["keys"])) for b in self.settings.get("keybindings", [])]
        self._capturing = None  # (binding_id, key_index) or None
        self._workers = []
        self._key_buttons = {}  # (binding_id, index) -> QPushButton

        self.dialog = QDialog()
        self.dialog.setWindowTitle(t("keybindings.title"))
        self.dialog.resize(560, 520)
        if "--force-topmost" in sys.argv:
            from PySide6.QtCore import Qt
            self.dialog.setWindowFlags(self.dialog.windowFlags() | Qt.WindowStaysOnTopHint)

        outer = QVBoxLayout(self.dialog)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)
        self._body = QWidget()
        scroll.setWidget(self._body)
        self._body_layout = QVBoxLayout(self._body)

        self.status_label = None  # created in _rebuild
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Save).clicked.connect(self._on_save)
        buttons.button(QDialogButtonBox.Close).clicked.connect(self.dialog.reject)
        outer.addWidget(buttons)
        self._buttons = buttons

        self._rebuild()

    # ---- state helpers -----------------------------------------------
    def _signature_counts(self):
        counts = {}
        for b in self.bindings:
            sig = _binding_signature(b)
            counts[sig] = counts.get(sig, 0) + 1
        return counts

    def _has_unset_key(self, binding):
        return any(not k for k in binding["keys"])

    def _validation_message(self):
        counts = self._signature_counts()
        if any(c > 1 for c in counts.values()):
            return t("keybindings.error_duplicate")
        if any(self._has_unset_key(b) for b in self.bindings):
            return t("keybindings.error_unset")
        return ""

    # ---- UI construction -----------------------------------------------
    def _rebuild(self):
        from PySide6.QtWidgets import QLabel

        while self._body_layout.count():
            item = self._body_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        self._key_buttons.clear()

        intro = QLabel(t("keybindings.intro"))
        intro.setWordWrap(True)
        self._body_layout.addWidget(intro)

        for command in COMMAND_ORDER:
            self._build_command_section(command)

        self.status_label = QLabel(self._validation_message())
        self._body_layout.addWidget(self.status_label)
        self._body_layout.addStretch(1)
        self._sync_save_enabled()

    def _build_command_section(self, command):
        from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget

        meta = _command_meta(command)
        section = QWidget()
        layout = QVBoxLayout(section)
        layout.setContentsMargins(0, 12, 0, 0)

        title = QLabel(f"<b>{meta['label']}</b>")
        layout.addWidget(title)
        desc = QLabel(meta["description"])
        desc.setStyleSheet("color: gray;")
        layout.addWidget(desc)

        for binding in [b for b in self.bindings if b["command"] == command]:
            layout.addWidget(self._build_binding_row(binding))

        add_button = QPushButton(t("keybindings.add_binding"))
        add_button.clicked.connect(lambda: self._add_binding(command))
        layout.addWidget(add_button)

        self._body_layout.addWidget(section)

    def _build_binding_row(self, binding):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import (
            QCheckBox, QFrame, QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout,
        )
        import input_state

        frame = QFrame()
        frame.setFrameShape(QFrame.StyledPanel)
        outer = QVBoxLayout(frame)
        row = QHBoxLayout()
        outer.addLayout(row)

        for index, key in enumerate(binding["keys"]):
            label = input_state.describe(key) if key else t("keybindings.press_to_bind")
            btn = QPushButton(label)
            btn.clicked.connect(lambda _checked, bid=binding["id"], i=index: self._start_capture(bid, i))
            self._key_buttons[(binding["id"], index)] = btn
            row.addWidget(btn)
            if len(binding["keys"]) > 1:
                remove_btn = QPushButton("×")
                remove_btn.setFixedWidth(24)
                remove_btn.clicked.connect(lambda _checked, bid=binding["id"], i=index: self._remove_key(bid, i))
                row.addWidget(remove_btn)
            if index < len(binding["keys"]) - 1:
                row.addWidget(QLabel("+"))

        if len(binding["keys"]) < MAX_KEYS_PER_BINDING:
            add_key_btn = QPushButton(t("keybindings.add_key"))
            add_key_btn.clicked.connect(lambda _checked, bid=binding["id"]: self._add_key(bid))
            row.addWidget(add_key_btn)

        meta = _command_meta(binding["command"])
        if meta["supports_long_press"]:
            long_press_box = QCheckBox(t("keybindings.long_press"))
            long_press_box.setChecked(binding["long_press"])
            long_press_box.toggled.connect(lambda checked, bid=binding["id"]: self._set_long_press(bid, checked))
            row.addWidget(long_press_box)

            if binding["long_press"]:
                threshold_label = QLabel(f'{binding["threshold_ms"]}ms')
                slider = QSlider(Qt.Horizontal)
                slider.setMinimum(300)
                slider.setMaximum(3000)
                slider.setSingleStep(50)
                slider.setValue(binding["threshold_ms"])

                def _on_slider(value, bid=binding["id"], lbl=threshold_label):
                    lbl.setText(f"{value}ms")
                    self._set_threshold(bid, value)

                slider.valueChanged.connect(_on_slider)
                row.addWidget(slider)
                row.addWidget(threshold_label)

        delete_btn = QPushButton(t("keybindings.delete"))
        delete_btn.clicked.connect(lambda _checked, bid=binding["id"]: self._delete_binding(bid))
        row.addWidget(delete_btn)

        sig = _binding_signature(binding)
        if self._signature_counts().get(sig, 0) > 1:
            warn = QLabel(t("keybindings.duplicate_warning"))
            warn.setStyleSheet("color: #ff8a80;")
            outer.addWidget(warn)
        if self._has_unset_key(binding):
            warn = QLabel(t("keybindings.unset_warning"))
            warn.setStyleSheet("color: #ff8a80;")
            outer.addWidget(warn)

        return frame

    # ---- mutation handlers ----------------------------------------------
    def _find_binding(self, binding_id):
        return next(b for b in self.bindings if b["id"] == binding_id)

    def _add_binding(self, command):
        self.bindings.append(
            {"id": _new_binding_id(), "command": command, "keys": [""], "long_press": False,
             "threshold_ms": DEFAULT_THRESHOLD_MS}
        )
        self._rebuild()

    def _delete_binding(self, binding_id):
        self.bindings = [b for b in self.bindings if b["id"] != binding_id]
        self._rebuild()

    def _add_key(self, binding_id):
        binding = self._find_binding(binding_id)
        if len(binding["keys"]) < MAX_KEYS_PER_BINDING:
            binding["keys"].append("")
        self._rebuild()

    def _remove_key(self, binding_id, index):
        binding = self._find_binding(binding_id)
        if len(binding["keys"]) > 1:
            del binding["keys"][index]
        self._rebuild()

    def _set_long_press(self, binding_id, checked):
        self._find_binding(binding_id)["long_press"] = checked
        self._rebuild()

    def _set_threshold(self, binding_id, value):
        self._find_binding(binding_id)["threshold_ms"] = value
        # Deliberately no _rebuild() here - the slider is mid-drag, a full
        # rebuild would destroy and recreate the widget the user's mouse is
        # currently on.

    def _start_capture(self, binding_id, index):
        if self._capturing is not None:
            return
        self._capturing = (binding_id, index)
        btn = self._key_buttons.get((binding_id, index))
        if btn:
            btn.setText(t("keybindings.listening"))
        self.status_label.setText(t("keybindings.listening_status"))
        self._sync_save_enabled()  # also disables Close for the duration - see its own comment

        worker = _CaptureWorker()
        self._workers.append(worker)

        def on_finished(identifier, label):
            self._capturing = None
            if identifier:
                self._find_binding(binding_id)["keys"][index] = identifier
                print(f"[keybindings] captured {identifier!r} ({label})")
            else:
                self.status_label.setText(t("keybindings.timeout_status"))
            self._rebuild()

        worker.start(on_finished)

    def _sync_save_enabled(self):
        from PySide6.QtWidgets import QDialogButtonBox

        message = self._validation_message()
        self._buttons.button(QDialogButtonBox.Save).setEnabled(not message and self._capturing is None)
        # _CaptureWorker.start() already makes closing mid-capture safe (the
        # worker outlives the dialog if needed), but disabling Close while
        # actually listening avoids the confusing "I closed this, why is it
        # still listening for a few more seconds" experience - the capture
        # times out on its own quickly (6s) either way.
        self._buttons.button(QDialogButtonBox.Close).setEnabled(self._capturing is None)

    def _on_save(self):
        if self._validation_message():
            return
        self.settings["keybindings"] = self.bindings
        settings_store.save(self.settings)
        self.status_label.setText(t("keybindings.saved_status"))
        print(f"[keybindings] saved {len(self.bindings)} binding(s)")

    def exec(self):
        return self.dialog.exec()


def main():
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    # Confirmed live 2026-08-26: with Qt's default quitOnLastWindowClosed,
    # closing this dialog (the only top-level window in this standalone
    # entry point) while _CaptureWorker was still listening crashed with the
    # same 0xc0000005 in Qt6Widgets.dll as the wrong-thread bug - not that
    # bug recurring, but QApplication tearing down while the worker's
    # QThread was still alive. Confirmed *not* reproducible the same way
    # inside tray_app.py, which already disables this (the app keeps running
    # after Settings/Keybindings closes) - this only matters for this
    # standalone dev-testing entry point.
    app.setQuitOnLastWindowClosed(False)
    dlg = KeybindingsDialog()
    dlg.exec()
    for worker in list(_ACTIVE_WORKERS):
        worker.thread.wait(7000)  # a touch over capture_one()'s own 6s timeout


if __name__ == "__main__":
    main()
