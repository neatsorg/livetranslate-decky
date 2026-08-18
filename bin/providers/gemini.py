import json
from urllib import error as urlerror
from urllib import request

from .base import ApiKeyError, NetworkError, ProviderError, RateLimitError, TranslationProvider, clean_api_key
from .prompt import build_prompt, sanitize_translation

_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class GeminiProvider(TranslationProvider):
    name = "gemini"

    # gemini-2.5-flash was retired for new users (confirmed live via a 404
    # from the API itself, which named gemini-3.6-flash as the successor -
    # Google's flash lineup moves fast, so this will likely need bumping
    # again; the QAM's "Gemini Model" field lets a user override it without
    # a redeploy either way.
    def __init__(self, api_key="", model="gemini-3.6-flash", timeout=60):
        self.api_key = clean_api_key(api_key)
        self.model = model
        self.timeout = timeout

    def is_available(self):
        return bool(self.api_key)

    def translate(self, *, speaker, text, target_lang, source_lang, profile, context_text):
        if not self.api_key:
            raise ApiKeyError("Gemini API key is not configured")

        prompt = build_prompt(speaker, text, target_lang, profile, context_text, source_lang=source_lang)
        payload = json.dumps(
            {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.1,
                    # Confirmed live: gemini-3.6-flash defaults to spending
                    # its output budget on internal "thinking" tokens before
                    # (or instead of) the actual answer, sometimes consuming
                    # the whole budget and returning empty content; calls
                    # also take 20-50s regardless. Tried disabling thinking
                    # via generationConfig.thinkingConfig.thinkingBudget=0
                    # (the mechanism from the Gemini 2.5 Flash-era API), but
                    # this model rejects it outright with a 400
                    # INVALID_ARGUMENT - its schema evidently differs and
                    # postdates what's confirmable here, so rather than keep
                    # guessing at undocumented fields, just give thinking
                    # (plus the actual answer) enough room to complete
                    # instead of trying to disable it.
                    "maxOutputTokens": 2000,
                },
            },
            ensure_ascii=False,
        ).encode("utf-8")
        req = request.Request(
            _ENDPOINT.format(model=self.model),
            data=payload,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "x-goog-api-key": self.api_key,
            },
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urlerror.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code in (401, 403):
                raise ApiKeyError(f"Gemini rejected the configured API key ({exc.code})") from exc
            if exc.code == 429:
                raise RateLimitError(f"Gemini rate limit or quota exceeded ({exc.code}): {body[:300]}") from exc
            raise NetworkError(f"Gemini request failed ({exc.code}): {body[:300]}") from exc
        except (urlerror.URLError, OSError) as exc:
            raise NetworkError(f"could not reach Gemini: {exc}") from exc

        candidates = data.get("candidates") or []
        if not candidates:
            block_reason = (data.get("promptFeedback") or {}).get("blockReason")
            if block_reason:
                raise ProviderError(f"Gemini blocked the request: {block_reason}")
            raise ProviderError(f"Gemini returned no candidates: {data}")

        parts = (candidates[0].get("content") or {}).get("parts")
        if not parts:
            usage = data.get("usageMetadata") or {}
            if usage.get("thoughtsTokenCount"):
                raise ProviderError(
                    "Gemini returned no text - its whole output budget "
                    f"({usage.get('thoughtsTokenCount')} tokens) went to internal "
                    "thinking instead. Raising maxOutputTokens further may help."
                )
            raise ProviderError(f"unexpected Gemini response shape: {data}")

        text_out = "".join(part.get("text", "") for part in parts)
        return sanitize_translation(text_out)
