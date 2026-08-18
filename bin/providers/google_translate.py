import json
from urllib import error as urlerror
from urllib import parse, request

from .base import NetworkError, RateLimitError, TranslationProvider
from .langcodes import to_code

_ENDPOINT = "https://translate.googleapis.com/translate_a/single"


class GoogleTranslateProvider(TranslationProvider):
    """Uses Google's unofficial, keyless translate_a/single endpoint.

    Not an LLM: ignores speaker/profile/context_text and just translates
    text between source_lang and target_lang. Needs no API key, which makes
    it the lowest-friction option for users without Ollama.
    """

    name = "google"

    def __init__(self, timeout=20):
        self.timeout = timeout

    def translate(self, *, speaker, text, target_lang, source_lang, profile, context_text):
        params = {
            "client": "gtx",
            "sl": to_code(source_lang),
            "tl": to_code(target_lang),
            "dt": "t",
            "q": text,
        }
        url = _ENDPOINT + "?" + parse.urlencode(params)
        req = request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urlerror.HTTPError as exc:
            if exc.code == 429:
                raise RateLimitError(f"Google Translate rate limit exceeded ({exc.code})") from exc
            raise NetworkError(f"Google Translate request failed ({exc.code})") from exc
        except (urlerror.URLError, OSError) as exc:
            raise NetworkError(f"could not reach Google Translate: {exc}") from exc

        try:
            segments = data[0]
            translated = "".join(segment[0] for segment in segments if segment and segment[0])
        except (IndexError, TypeError) as exc:
            raise NetworkError(f"unexpected Google Translate response shape: {data}") from exc
        return translated.strip()
