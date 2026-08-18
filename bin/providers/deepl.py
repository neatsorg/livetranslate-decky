import json
from urllib import error as urlerror
from urllib import request

from .base import ApiKeyError, NetworkError, ProviderError, RateLimitError, TranslationProvider, clean_api_key
from .langcodes import to_code

# Target language requires a region for English; DeepL rejects a bare "EN"
# as a *target* (source is fine as plain "EN").
_TARGET_ALIASES = {"EN": "EN-US"}


class DeepLProvider(TranslationProvider):
    """Official DeepL REST API. Not an LLM: ignores speaker/profile/
    context_text and just translates text between source_lang and
    target_lang.
    """

    name = "deepl"

    def __init__(self, api_key="", timeout=20):
        self.api_key = clean_api_key(api_key)
        self.timeout = timeout

    def is_available(self):
        return bool(self.api_key)

    def _host(self):
        return "api-free.deepl.com" if self.api_key.endswith(":fx") else "api.deepl.com"

    def translate(self, *, speaker, text, target_lang, source_lang, profile, context_text):
        if not self.api_key:
            raise ApiKeyError("DeepL API key is not configured")

        target_code = to_code(target_lang).upper()
        target_code = _TARGET_ALIASES.get(target_code, target_code)
        source_code = to_code(source_lang).upper()

        body = {"text": [text], "target_lang": target_code}
        if source_code and source_code != "AUTO":
            body["source_lang"] = source_code

        req = request.Request(
            f"https://{self._host()}/v2/translate",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"DeepL-Auth-Key {self.api_key}",
            },
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urlerror.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            if exc.code == 403:
                raise ApiKeyError(f"DeepL rejected the configured API key ({exc.code}): {body_text[:300]}") from exc
            if exc.code == 456:
                raise RateLimitError(f"DeepL monthly quota exceeded (456): {body_text[:300]}") from exc
            if exc.code == 429:
                raise RateLimitError(f"DeepL rate limit exceeded ({exc.code}): {body_text[:300]}") from exc
            raise NetworkError(f"DeepL request failed ({exc.code}): {body_text[:300]}") from exc
        except (urlerror.URLError, OSError) as exc:
            raise NetworkError(f"could not reach DeepL: {exc}") from exc

        try:
            return data["translations"][0]["text"].strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"unexpected DeepL response shape: {data}") from exc
