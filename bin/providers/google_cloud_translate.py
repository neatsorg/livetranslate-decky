import json
from urllib import error as urlerror
from urllib import request

from .base import ApiKeyError, NetworkError, ProviderError, RateLimitError, TranslationProvider, clean_api_key
from .langcodes import to_code

_ENDPOINT = "https://translation.googleapis.com/language/translate/v2"


class GoogleCloudTranslateProvider(TranslationProvider):
    """Official Google Cloud Translation API (Basic/v2). Not an LLM: ignores
    speaker/profile/context_text and just translates text between
    source_lang and target_lang - no "thinking" overhead, unlike Gemini, so
    should be much lower-latency for straightforward translation.

    Needs a Google Cloud project with the Cloud Translation API enabled and
    an API key (has a free monthly quota, but billing must be set up) -
    unlike the free unofficial `google` provider, which needs no account at
    all.
    """

    name = "google_cloud"

    def __init__(self, api_key="", timeout=20):
        self.api_key = clean_api_key(api_key)
        self.timeout = timeout

    def is_available(self):
        return bool(self.api_key)

    def translate(self, *, speaker, text, target_lang, source_lang, profile, context_text):
        if not self.api_key:
            raise ApiKeyError("Google Cloud Translation API key is not configured")

        body = {"q": text, "target": to_code(target_lang), "format": "text"}
        source_code = to_code(source_lang)
        if source_code and source_code != "auto":
            body["source"] = source_code

        req = request.Request(
            _ENDPOINT,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "X-goog-api-key": self.api_key,
            },
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urlerror.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            # Confirmed live: an invalid key surfaces as a plain 400 here,
            # not 401/403 - Google Cloud's generic REST error shape doesn't
            # give this its own status code, so match on the message text
            # instead (this is also the response for a key whose "API
            # restrictions" don't include Cloud Translation, or one that's
            # just malformed - all worth surfacing as a key problem rather
            # than a generic network failure).
            if exc.code in (401, 403) or "API key not valid" in body_text:
                raise ApiKeyError(
                    f"Google Cloud rejected the configured API key ({exc.code}): {body_text[:300]}"
                ) from exc
            if exc.code == 429:
                raise RateLimitError(
                    f"Google Cloud Translation rate limit or quota exceeded ({exc.code}): {body_text[:300]}"
                ) from exc
            raise NetworkError(f"Google Cloud Translation request failed ({exc.code}): {body_text[:300]}") from exc
        except (urlerror.URLError, OSError) as exc:
            raise NetworkError(f"could not reach Google Cloud Translation: {exc}") from exc

        try:
            return data["data"]["translations"][0]["translatedText"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"unexpected Google Cloud Translation response shape: {data}") from exc
