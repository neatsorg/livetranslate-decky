from .base import ApiKeyError, NetworkError, ProviderError, RateLimitError, TranslationProvider
from .deepl import DeepLProvider
from .dummy import DummyProvider
from .gemini import GeminiProvider
from .google_cloud_translate import GoogleCloudTranslateProvider
from .google_translate import GoogleTranslateProvider
from .ollama import OllamaProvider

_PROVIDER_CLASSES = {
    DummyProvider.name: DummyProvider,
    OllamaProvider.name: OllamaProvider,
    GeminiProvider.name: GeminiProvider,
    GoogleTranslateProvider.name: GoogleTranslateProvider,
    GoogleCloudTranslateProvider.name: GoogleCloudTranslateProvider,
    DeepLProvider.name: DeepLProvider,
}


def create_provider(name, **config):
    try:
        provider_cls = _PROVIDER_CLASSES[name]
    except KeyError:
        raise ValueError(f"unknown backend: {name}") from None
    return provider_cls(**config)


__all__ = [
    "ApiKeyError",
    "DeepLProvider",
    "DummyProvider",
    "GeminiProvider",
    "GoogleCloudTranslateProvider",
    "GoogleTranslateProvider",
    "NetworkError",
    "OllamaProvider",
    "ProviderError",
    "RateLimitError",
    "TranslationProvider",
    "create_provider",
]
