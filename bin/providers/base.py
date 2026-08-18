import re
from abc import ABC, abstractmethod

_ZERO_WIDTH_RE = re.compile("[\u200b\u200c\u200d\ufeff]")


def clean_api_key(value):
    """Strip whitespace and zero-width characters clipboard managers
    sometimes carry along on paste - a wrong-looking-but-actually-mistyped
    key is a common real-world source of "API key not valid" errors."""
    if not value:
        return value
    return _ZERO_WIDTH_RE.sub("", value).strip()


class ProviderError(Exception):
    """Base class for translation provider failures."""


class ApiKeyError(ProviderError):
    """The configured API key is missing or was rejected by the provider."""


class RateLimitError(ProviderError):
    """The provider is throttling requests or out of quota."""


class NetworkError(ProviderError):
    """The provider could not be reached, or the request timed out."""


class TranslationProvider(ABC):
    name = "unknown"

    def is_available(self):
        return True

    @abstractmethod
    def translate(self, *, speaker, text, target_lang, source_lang, profile, context_text):
        """Translate `text` and return the translated string.

        LLM-style providers (Ollama, Gemini) are expected to build a prompt
        from speaker/profile/context_text internally; plain MT API providers
        (Google, DeepL) may ignore those and translate text/target_lang/
        source_lang directly.
        """
