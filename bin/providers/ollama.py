import json
from urllib import error as urlerror
from urllib import request

from .base import NetworkError, TranslationProvider
from .prompt import build_prompt, sanitize_translation


class OllamaProvider(TranslationProvider):
    name = "ollama"

    def __init__(self, url="http://127.0.0.1:11434", model="translategemma", timeout=120):
        self.url = url
        self.model = model
        self.timeout = timeout

    def translate(self, *, speaker, text, target_lang, source_lang, profile, context_text):
        prompt = build_prompt(speaker, text, target_lang, profile, context_text, source_lang=source_lang)
        payload = json.dumps(
            {
                "model": self.model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": 0.1,
                    "num_predict": 250,
                },
                "keep_alive": -1,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        req = request.Request(
            self.url.rstrip("/") + "/api/generate",
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urlerror.URLError, OSError) as exc:
            raise NetworkError(f"could not reach Ollama at {self.url}: {exc}") from exc
        return sanitize_translation(str(data.get("response") or ""))
