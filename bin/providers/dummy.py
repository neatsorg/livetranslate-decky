from .base import TranslationProvider


class DummyProvider(TranslationProvider):
    name = "dummy"

    def translate(self, *, speaker, text, target_lang, source_lang, profile, context_text):
        prefix = f"[{target_lang} draft]"
        speaker_line = f"{speaker}: " if speaker else ""
        return f"{prefix} {speaker_line}{text}"
