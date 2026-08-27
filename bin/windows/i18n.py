"""Minimal i18n for the settings UI - a flat key->string catalog per
language, not a full gettext/babel setup (the string count is small and
nothing here needs plural forms or ICU formatting).

detect_default_language() reads the OS UI language via
GetUserDefaultUILanguage - what language the user actually wants UI text
in, independent of e.g. keyboard layout or locale-for-non-Unicode-programs -
so a non-Japanese Windows install defaults to English without the user
having to find a language toggle first. current_language() still lets the
settings UI override this explicitly (persisted as settings["ui_language"]).
"""
import ctypes

STRINGS = {
    "ja": {
        "app_name": "LiveTranslator-kun",
        "settings.title": "LiveTranslator-kun 設定",
        "settings.language_label": "表示言語",
        "settings.provider_label": "翻訳エンジン",
        "settings.api_key_label": "APIキー",
        "settings.model_label": "モデル",
        "settings.url_label": "URL",
        "settings.target_lang_label": "翻訳先言語",
        "settings.source_lang_label": "翻訳元言語 (auto可)",
        "settings.window_title_label": "対象ウィンドウ名 (部分一致)",
        "settings.window_title_placeholder": "空欄なら画面全体をキャプチャ",
        "settings.refresh_window_list": "一覧を更新",
        "settings.ocr_engine_label": "OCRエンジン",
        "settings.ocr_engine_screenai": "Chrome Screen AI (高精度)",
        "settings.ocr_engine_windows_ocr": "Windows標準OCR (高速)",
        "settings.manage_ocr_languages": "OCR言語を管理...",
        "settings.windows_ocr_lang_label": "Windows OCRの言語",
        "settings.windows_ocr_lang_placeholder": "例: en-US, ja-JP",
        "settings.keybindings_label": "キーバインド",
        "settings.manage_keybindings": "キーバインドを設定...",
        "settings.roi_label": "キャプチャ範囲",
        "settings.manage_roi": "キャプチャ範囲(固定ROI)を設定...",
        "settings.saved": "保存しました",
        "provider.dummy": "Dummy (テスト用、翻訳しない)",
        "provider.google": "Google翻訳 (無料・登録不要)",
        "provider.google_cloud": "Google Cloud Translation API",
        "provider.deepl": "DeepL API",
        "provider.gemini": "Google Gemini",
        "provider.ollama": "Ollama (ローカル/LAN)",

        "tray.settings": "設定...",
        "tray.start_capture": "キャプチャ開始",
        "tray.pause_capture": "一時停止",
        "tray.quit": "終了",
        "tray.reload_failed": "設定の再読み込みに失敗しました: {error}",
        "tray.not_configured": "翻訳エンジンまたはOCRエンジンが未設定です。「設定...」から設定してください。",
        "toast.refreshed": "更新しました",
        "toast.paused": "一時停止しました",
        "toast.resumed": "再開しました",

        "keybindings.title": "キーバインド設定",
        "keybindings.intro": "キー枠をクリックしてから、キーボード/マウス/ゲームパッドの実際のボタンを押してください。",
        "keybindings.command.refresh": "Refresh",
        "keybindings.command.refresh_desc": "画面を再検出し直します。",
        "keybindings.command.pause_resume": "Pause / Resume",
        "keybindings.command.pause_resume_desc": "翻訳ループの一時停止/再開を切り替えます。",
        "keybindings.press_to_bind": "Press to bind",
        "keybindings.listening": "Listening...",
        "keybindings.add_key": "+ Add key",
        "keybindings.add_binding": "+ Add binding",
        "keybindings.delete": "Delete",
        "keybindings.long_press": "長押し",
        "keybindings.duplicate_warning": "重複しています",
        "keybindings.unset_warning": "キーが未設定です",
        "keybindings.error_duplicate": "同じキー(と長押し設定)の組み合わせが重複しています。",
        "keybindings.error_unset": "未設定のキーがあります。",
        "keybindings.listening_status": "キーボード/マウス/ゲームパッドの何かを押してください...",
        "keybindings.timeout_status": "入力が検出されませんでした（タイムアウト）",
        "keybindings.saved_status": "保存しました",

        "ocr_lang.title": "OCR言語コンポーネント",
        "ocr_lang.intro": "Windows標準OCRで使う言語を追加インストールできます。インストールには管理者の承認が必要です。",
        "ocr_lang.installed": "インストール済み",
        "ocr_lang.not_installed": "未インストール",
        "ocr_lang.install_button": "インストール",
        "ocr_lang.installing_status": "インストール中… (UACの確認画面が出たら許可してください)",
        "ocr_lang.install_failed_prefix": "失敗: {message}",
        "locale.en-US": "英語 (US)",
        "locale.ja-JP": "日本語",
        "locale.ko-KR": "韓国語",
        "locale.zh-CN": "中国語 (簡体)",
        "locale.zh-TW": "中国語 (繁体)",
        "locale.fr-FR": "フランス語",
        "locale.de-DE": "ドイツ語",
        "locale.es-ES": "スペイン語",
        "ocr_lang.uac_declined": "管理者権限の昇格が許可されませんでした: {error}",
        "ocr_lang.timeout": "{timeout}秒待っても完了しませんでした（ネットワークが遅い可能性があります）",
        "ocr_lang.exit_error": "インストールコマンドがエラー終了しました (exit code {code})",
        "ocr_lang.install_success": "インストールが完了しました",
        "ocr_lang.install_unconfirmed": "コマンドは完了しましたが、インストール状態を確認できませんでした",

        "roi.title": "キャプチャ範囲 (固定ROI)",
        "roi.intro": "画面全体を検出する代わりに、字幕欄など特定の1領域だけを追跡したいときに使います。",
        "roi.no_screenshot": "スクリーンショットを取得してください",
        "roi.retake": "スクリーンショットを再取得",
        "roi.left": "左端 (Left)",
        "roi.top": "上端 (Top)",
        "roi.width": "幅 (Width)",
        "roi.height": "高さ (Height)",
        "roi.save": "保存",
        "roi.save_and_enable": "保存して有効化",
        "roi.disable": "固定ROIを無効化",
        "roi.close": "閉じる",
        "roi.screenshot_failed": "スクリーンショット取得失敗: {error}",
        "roi.saved_enabled": "有効化して保存しました",
        "roi.saved": "保存しました",
        "roi.disabled": "固定ROIを無効化しました（画面全体の検出に戻ります）",
    },
    "en": {
        "app_name": "LiveTranslator-kun",
        "settings.title": "LiveTranslator-kun Settings",
        "settings.language_label": "Display language",
        "settings.provider_label": "Translation engine",
        "settings.api_key_label": "API key",
        "settings.model_label": "Model",
        "settings.url_label": "URL",
        "settings.target_lang_label": "Target language",
        "settings.source_lang_label": "Source language (auto OK)",
        "settings.window_title_label": "Target window title (substring)",
        "settings.window_title_placeholder": "Leave blank to capture the whole screen",
        "settings.refresh_window_list": "Refresh list",
        "settings.ocr_engine_label": "OCR engine",
        "settings.ocr_engine_screenai": "Chrome Screen AI (higher accuracy)",
        "settings.ocr_engine_windows_ocr": "Windows built-in OCR (faster)",
        "settings.manage_ocr_languages": "Manage OCR languages...",
        "settings.windows_ocr_lang_label": "Windows OCR language",
        "settings.windows_ocr_lang_placeholder": "e.g. en-US, ja-JP",
        "settings.keybindings_label": "Keybindings",
        "settings.manage_keybindings": "Configure keybindings...",
        "settings.roi_label": "Capture region",
        "settings.manage_roi": "Configure capture region (fixed ROI)...",
        "settings.saved": "Saved",
        "provider.dummy": "Dummy (testing only, no translation)",
        "provider.google": "Google Translate (free, no account)",
        "provider.google_cloud": "Google Cloud Translation API",
        "provider.deepl": "DeepL API",
        "provider.gemini": "Google Gemini",
        "provider.ollama": "Ollama (local/LAN)",

        "tray.settings": "Settings...",
        "tray.start_capture": "Start Capture",
        "tray.pause_capture": "Pause",
        "tray.quit": "Exit",
        "tray.reload_failed": "Failed to reload settings: {error}",
        "tray.not_configured": "The translation engine or OCR engine isn't configured yet. Open \"Settings...\" to set it up.",
        "toast.refreshed": "Refreshed",
        "toast.paused": "Paused",
        "toast.resumed": "Resumed",

        "keybindings.title": "Keybinding Settings",
        "keybindings.intro": "Click a key slot, then press the actual keyboard, mouse, or gamepad button you want to bind.",
        "keybindings.command.refresh": "Refresh",
        "keybindings.command.refresh_desc": "Re-runs detection on the current screen.",
        "keybindings.command.pause_resume": "Pause / Resume",
        "keybindings.command.pause_resume_desc": "Toggles the translation loop's paused state.",
        "keybindings.press_to_bind": "Press to bind",
        "keybindings.listening": "Listening...",
        "keybindings.add_key": "+ Add key",
        "keybindings.add_binding": "+ Add binding",
        "keybindings.delete": "Delete",
        "keybindings.long_press": "Long press",
        "keybindings.duplicate_warning": "Duplicate",
        "keybindings.unset_warning": "Key not set",
        "keybindings.error_duplicate": "Two bindings share the same keys (and long-press setting).",
        "keybindings.error_unset": "One or more key slots aren't bound yet.",
        "keybindings.listening_status": "Press something on your keyboard, mouse, or gamepad...",
        "keybindings.timeout_status": "No input detected (timed out)",
        "keybindings.saved_status": "Saved",

        "ocr_lang.title": "OCR Language Components",
        "ocr_lang.intro": "Install additional languages for Windows' built-in OCR. Installing requires administrator approval.",
        "ocr_lang.installed": "Installed",
        "ocr_lang.not_installed": "Not installed",
        "ocr_lang.install_button": "Install",
        "ocr_lang.installing_status": "Installing... (approve the UAC prompt if one appears)",
        "ocr_lang.install_failed_prefix": "Failed: {message}",
        "locale.en-US": "English (US)",
        "locale.ja-JP": "Japanese",
        "locale.ko-KR": "Korean",
        "locale.zh-CN": "Chinese (Simplified)",
        "locale.zh-TW": "Chinese (Traditional)",
        "locale.fr-FR": "French",
        "locale.de-DE": "German",
        "locale.es-ES": "Spanish",
        "ocr_lang.uac_declined": "The administrator elevation prompt was not approved: {error}",
        "ocr_lang.timeout": "Didn't finish after {timeout}s (the network may be slow)",
        "ocr_lang.exit_error": "The install command exited with an error (exit code {code})",
        "ocr_lang.install_success": "Installed successfully",
        "ocr_lang.install_unconfirmed": "The command finished, but the installed state couldn't be confirmed",

        "roi.title": "Capture Region (Fixed ROI)",
        "roi.intro": "Use this to track just one fixed region (e.g. a subtitle box) instead of scanning the whole screen.",
        "roi.no_screenshot": "Take a screenshot to preview",
        "roi.retake": "Retake screenshot",
        "roi.left": "Left",
        "roi.top": "Top",
        "roi.width": "Width",
        "roi.height": "Height",
        "roi.save": "Save",
        "roi.save_and_enable": "Save && enable",
        "roi.disable": "Disable fixed ROI",
        "roi.close": "Close",
        "roi.screenshot_failed": "Screenshot failed: {error}",
        "roi.saved_enabled": "Saved and enabled",
        "roi.saved": "Saved",
        "roi.disabled": "Fixed ROI disabled (back to whole-screen detection)",
    },
}

_LANGID_JAPANESE_PRIMARY = 0x11  # PRIMARYLANGID(LANG_JAPANESE)

_current_language = None


def detect_default_language():
    try:
        langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        if (langid & 0x3FF) == _LANGID_JAPANESE_PRIMARY:
            return "ja"
    except Exception:
        pass
    return "en"


def set_language(lang):
    global _current_language
    _current_language = lang if lang in STRINGS else "en"


def current_language():
    global _current_language
    if _current_language is None:
        try:
            import settings_store

            _current_language = settings_store.load().get("ui_language") or detect_default_language()
        except Exception:
            _current_language = detect_default_language()
    return _current_language


def t(key, **kwargs):
    lang = current_language()
    catalog = STRINGS.get(lang, STRINGS["en"])
    text = catalog.get(key, STRINGS["en"].get(key, key))
    return text.format(**kwargs) if kwargs else text
