"""Settings UI: a plain PySide6 dialog for editing settings_store.py's
persisted JSON. Standalone-runnable for testing (`python settings_dialog.py`)
so this can be validated on its own before wiring it into a tray-icon app
shell - there's no shell yet (see project_playtranslate_windows_port memory,
"still no packaging/tray/UI" was flagged as missing).

Field visibility per provider (api_key / model / url) intentionally toggles
rather than rebuilding the form, since the provider set is small and fixed
(see providers/*.py's own __init__ signatures for which fields each needs).

All user-visible text goes through i18n.t() rather than being hardcoded in
one language - the settings UI defaulted to Japanese for a while (the
developer's own locale), which the user pointed out is a real barrier for
anyone who isn't reading Japanese. i18n.detect_default_language() picks a
sensible default from the OS's own UI language; this dialog's own language
dropdown lets a user override that regardless.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # for providers/
sys.path.insert(0, str(Path(__file__).resolve().parent))  # for settings_store, i18n

if sys.platform == "win32":
    from winlog import setup_stdio

    setup_stdio("settings_dialog.log")

import settings_store
from i18n import t

PROVIDER_FIELDS = {
    "dummy": [],
    "google": [],
    "google_cloud": ["api_key"],
    "deepl": ["api_key"],
    "gemini": ["api_key", "model"],
    "ollama": ["url", "model"],
}

LANGUAGES = [("ja", "日本語"), ("en", "English")]


class SettingsDialog:
    """Not a QDialog subclass on purpose - keeps this importable/testable
    without necessarily instantiating QApplication first (callers that
    already have one, e.g. a future tray app, just reuse it)."""

    def __init__(self, capture_worker=None):
        from PySide6.QtWidgets import (
            QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLineEdit,
            QPushButton, QVBoxLayout,
        )

        # Only used to hand off to RoiCropDialog (see _on_manage_roi()) so
        # its screenshot grabs go through the same dedicated dxcam thread
        # the real capture loop uses, instead of a separate ad-hoc camera
        # from the Qt main thread. None in standalone use (`python
        # settings_dialog.py`) - RoiCropDialog falls back to its own direct
        # dxcam.create() in that case.
        self._capture_worker = capture_worker

        self.settings = settings_store.load()
        import i18n
        i18n.set_language(self.settings.get("ui_language") or i18n.detect_default_language())

        self.dialog = QDialog()
        self.dialog.setWindowTitle(t("settings.title"))
        self.dialog.setMinimumWidth(440)
        if "--force-topmost" in sys.argv:
            # Diagnostic-only: a Task-Scheduler-launched process doesn't
            # have foreground rights, so a normal dialog can open genuinely
            # invisible behind whatever window is currently active (a real
            # double-click launch by the user won't have this problem -
            # this is a test-harness artifact, not a product bug).
            from PySide6.QtCore import Qt
            self.dialog.setWindowFlags(self.dialog.windowFlags() | Qt.WindowStaysOnTopHint)

        layout = QVBoxLayout(self.dialog)
        form = QFormLayout()
        layout.addLayout(form)

        self.language_combo = QComboBox()
        for code, label in LANGUAGES:
            self.language_combo.addItem(label, userData=code)
        self.language_combo.currentIndexChanged.connect(self._on_language_changed)
        form.addRow(t("settings.language_label"), self.language_combo)

        self.provider_combo = QComboBox()
        for key in PROVIDER_FIELDS:
            self.provider_combo.addItem(t(f"provider.{key}"), userData=key)
        form.addRow(t("settings.provider_label"), self.provider_combo)

        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.Password)
        self.api_key_row = form.rowCount()
        form.addRow(t("settings.api_key_label"), self.api_key_edit)

        self.model_edit = QLineEdit()
        self.model_row = form.rowCount()
        form.addRow(t("settings.model_label"), self.model_edit)

        self.url_edit = QLineEdit()
        self.url_row = form.rowCount()
        form.addRow(t("settings.url_label"), self.url_edit)

        self.target_lang_edit = QLineEdit()
        form.addRow(t("settings.target_lang_label"), self.target_lang_edit)

        self.source_lang_edit = QLineEdit()
        form.addRow(t("settings.source_lang_label"), self.source_lang_edit)

        window_title_row = QHBoxLayout()
        self.window_title_combo = QComboBox()
        self.window_title_combo.setEditable(True)
        # Typed text that isn't in the list (e.g. a substring, or a game
        # that isn't running yet) must stay usable as free text - NoInsert
        # keeps it from silently turning into a new permanent dropdown
        # entry the moment focus leaves the field.
        self.window_title_combo.setInsertPolicy(QComboBox.NoInsert)
        self.window_title_combo.lineEdit().setPlaceholderText(t("settings.window_title_placeholder"))
        window_title_row.addWidget(self.window_title_combo)
        window_title_refresh_button = QPushButton(t("settings.refresh_window_list"))
        window_title_refresh_button.clicked.connect(self._refresh_window_title_choices)
        window_title_row.addWidget(window_title_refresh_button)
        form.addRow(t("settings.window_title_label"), window_title_row)

        ocr_row = QHBoxLayout()
        self.ocr_engine_combo = QComboBox()
        self.ocr_engine_combo.addItem(t("settings.ocr_engine_windows_ocr"), userData="windows_ocr")
        ocr_row.addWidget(self.ocr_engine_combo)
        ocr_lang_button = QPushButton(t("settings.manage_ocr_languages"))
        ocr_lang_button.clicked.connect(self._on_manage_ocr_languages)
        ocr_row.addWidget(ocr_lang_button)
        form.addRow(t("settings.ocr_engine_label"), ocr_row)

        self.windows_ocr_lang_edit = QLineEdit()
        self.windows_ocr_lang_edit.setPlaceholderText(t("settings.windows_ocr_lang_placeholder"))
        self.windows_ocr_lang_row = form.rowCount()
        form.addRow(t("settings.windows_ocr_lang_label"), self.windows_ocr_lang_edit)
        self.ocr_engine_combo.currentIndexChanged.connect(self._sync_row_visibility)

        keybindings_button = QPushButton(t("settings.manage_keybindings"))
        keybindings_button.clicked.connect(self._on_manage_keybindings)
        form.addRow(t("settings.keybindings_label"), keybindings_button)

        roi_button = QPushButton(t("settings.manage_roi"))
        roi_button.clicked.connect(self._on_manage_roi)
        form.addRow(t("settings.roi_label"), roi_button)

        self._form = form
        self.provider_combo.currentIndexChanged.connect(self._on_provider_changed)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.dialog.reject)
        layout.addWidget(buttons)

        self._load_into_form()

    def _on_language_changed(self):
        # Takes effect on next open (rebuilding every widget's text live
        # mid-dialog isn't worth the complexity for a settings screen the
        # user isn't staring at continuously) - saved immediately though,
        # not deferred to the dialog's own Save button, so a language
        # switch sticks even if the rest of the form is then cancelled.
        import i18n

        lang = self.language_combo.currentData()
        self.settings["ui_language"] = lang
        settings_store.save(self.settings)
        i18n.set_language(lang)

    def _on_manage_ocr_languages(self):
        from ocr_language_dialog import OcrLanguageDialog

        dlg = OcrLanguageDialog()
        dlg.exec()
        # Missing this call was a real bug (confirmed live 2026-08-29): a
        # successful install writes "ocr_installed_locales" straight to
        # settings.json from inside windows_ocr_lang.install(), same as
        # Keybindings/ROI below - without resyncing self.settings here too,
        # _on_accept()'s settings_store.save(self.settings) later wrote back
        # this dialog's stale pre-install snapshot over that file, silently
        # erasing the just-installed locale from the cache the instant
        # Settings itself was closed with OK.
        self._resync_settings_after_child_dialog()

    def _on_manage_keybindings(self):
        from keybindings_dialog import KeybindingsDialog

        dlg = KeybindingsDialog()
        dlg.exec()
        self._resync_settings_after_child_dialog()

    def _on_manage_roi(self):
        from roi_crop_dialog import RoiCropDialog

        # hide_during_screenshot: so RoiCropDialog can hide this Settings
        # window during its own screenshot grabs - otherwise Settings sits
        # on top of the game window and gets baked into the "clean"
        # screenshot the ROI picker is supposed to show (confirmed live
        # 2026-08-30). capture_worker: see this dialog's own __init__.
        dlg = RoiCropDialog(hide_during_screenshot=[self.dialog], capture_worker=self._capture_worker)
        dlg.exec()
        self._resync_settings_after_child_dialog()

    def _resync_settings_after_child_dialog(self):
        # KeybindingsDialog/RoiCropDialog save straight to settings.json
        # themselves (their own Save buttons, independent of this dialog's
        # OK/Cancel) - self.settings was loaded once at __init__ and never
        # touched since, so without this refresh, _on_accept()'s later
        # settings_store.save(self.settings) would write that stale copy
        # back over whatever the child dialog just saved, silently
        # reverting it. Safe to reload wholesale: every field this dialog's
        # own widgets can edit is written into self.settings explicitly in
        # _on_accept() from the widgets themselves, not carried over from
        # this snapshot, so refreshing it here can't lose an in-progress
        # (not-yet-OK'd) edit sitting in a widget.
        self.settings = settings_store.load()

    def _current_provider_key(self):
        return self.provider_combo.currentData()

    def _on_provider_changed(self):
        # Not just a visibility toggle - the API-key/model/url fields still
        # held whatever the *previous* provider's values were (or blanks),
        # and _on_accept() writes those field contents straight into the
        # newly-selected provider's config. Without reloading them here
        # first, switching e.g. Google Cloud -> Gemini and clicking OK would
        # write Google Cloud's leftover API key text into Gemini's config
        # (or blank out a Gemini key/model that was already saved).
        self._load_provider_fields(self._current_provider_key())
        self._sync_row_visibility()

    def _refresh_window_title_choices(self):
        # Re-enumerates open windows fresh each call rather than once at
        # __init__ - a real workflow is "open Settings, then launch the
        # game, then come back and pick its title", so a snapshot taken
        # before the game even started would be useless without this.
        # Preserves whatever's currently typed/selected (setCurrentText
        # works even for text that isn't among the new items, since the
        # combo is editable) so hitting refresh doesn't clobber an in-
        # progress edit.
        from window_finder import list_window_titles

        current = self.window_title_combo.currentText()
        try:
            titles = list_window_titles()
        except Exception as exc:
            print(f"[settings] window list refresh failed: {exc}")
            titles = []
        self.window_title_combo.clear()
        self.window_title_combo.addItem("")  # empty = capture the whole screen, same as leaving it blank
        self.window_title_combo.addItems(titles)
        self.window_title_combo.setCurrentText(current)

    def _load_provider_fields(self, provider):
        cfg = self.settings.get("provider_config", {}).get(provider, {})
        self.api_key_edit.setText(cfg.get("api_key", ""))
        self.model_edit.setText(cfg.get("model", ""))
        self.url_edit.setText(cfg.get("url", ""))

    def _sync_row_visibility(self):
        fields = PROVIDER_FIELDS.get(self._current_provider_key(), [])
        self._form.setRowVisible(self.api_key_row, "api_key" in fields)
        self._form.setRowVisible(self.model_row, "model" in fields)
        self._form.setRowVisible(self.url_row, "url" in fields)
        self._form.setRowVisible(self.windows_ocr_lang_row, self.ocr_engine_combo.currentData() == "windows_ocr")

    def _load_into_form(self):
        import i18n

        lang_idx = self.language_combo.findData(i18n.current_language())
        self.language_combo.setCurrentIndex(max(lang_idx, 0))

        provider = self.settings.get("provider", "google_cloud")
        idx = self.provider_combo.findData(provider)
        self.provider_combo.setCurrentIndex(max(idx, 0))

        self._load_provider_fields(provider)

        self.target_lang_edit.setText(self.settings.get("target_lang", "ja"))
        self.source_lang_edit.setText(self.settings.get("source_lang", "auto"))
        self._refresh_window_title_choices()
        self.window_title_combo.setCurrentText(self.settings.get("capture", {}).get("window_title", ""))

        ocr_idx = self.ocr_engine_combo.findData(self.settings.get("ocr_engine", "windows_ocr"))
        self.ocr_engine_combo.setCurrentIndex(max(ocr_idx, 0))
        self.windows_ocr_lang_edit.setText(self.settings.get("windows_ocr_language", "en-US"))

        self._sync_row_visibility()

    def _on_accept(self):
        provider = self._current_provider_key()
        self.settings["provider"] = provider
        cfg = self.settings.setdefault("provider_config", {}).setdefault(provider, {})
        if "api_key" in PROVIDER_FIELDS.get(provider, []):
            cfg["api_key"] = self.api_key_edit.text().strip()
        if "model" in PROVIDER_FIELDS.get(provider, []):
            cfg["model"] = self.model_edit.text().strip()
        if "url" in PROVIDER_FIELDS.get(provider, []):
            cfg["url"] = self.url_edit.text().strip()
        self.settings["target_lang"] = self.target_lang_edit.text().strip() or "ja"
        self.settings["source_lang"] = self.source_lang_edit.text().strip() or "auto"
        self.settings.setdefault("capture", {})["window_title"] = self.window_title_combo.currentText().strip()
        self.settings["ocr_engine"] = self.ocr_engine_combo.currentData()
        self.settings["windows_ocr_language"] = self.windows_ocr_lang_edit.text().strip() or "en-US"
        self.settings["ui_language"] = self.language_combo.currentData()
        # keybindings themselves are edited/saved directly by
        # KeybindingsDialog (its own settings_store.save() call) - nothing
        # to carry over from this dialog's own fields.

        settings_store.save(self.settings)
        print(f"saved settings to {settings_store.settings_path()}")
        self.dialog.accept()

    def exec(self):
        return self.dialog.exec()


def main():
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    dlg = SettingsDialog()
    result = dlg.exec()
    print(f"dialog result: {result}")


if __name__ == "__main__":
    main()
