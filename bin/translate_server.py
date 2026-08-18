#!/usr/bin/env python3
import argparse
import json
import os
import threading
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from providers import ApiKeyError, NetworkError, ProviderError, RateLimitError, create_provider
from providers.prompt import sanitize_speaker


class TranslationCache:
    """In-memory LRU cache for exact (backend, langs, speaker, text)
    matches. Mirrors playtranslate-android's own 50-entry translationCache -
    confirmed there to meaningfully cut repeat-translation latency (e.g. a
    static UI label re-appearing after a scene change), and matters even
    more here since LLM backends (Ollama/Gemini) can take 1-2+ seconds per
    call versus ~0.1s for a plain MT API. Thread-safe: ThreadingHTTPServer
    can run several /translate requests concurrently (the dynamic engine's
    discovery phase translates newly-found blocks in parallel).
    """

    def __init__(self, max_entries=50):
        self._max_entries = max_entries
        self._store = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            if key not in self._store:
                return None
            self._store.move_to_end(key)
            return self._store[key]

    def put(self, key, value):
        with self._lock:
            self._store[key] = value
            self._store.move_to_end(key)
            if len(self._store) > self._max_entries:
                self._store.popitem(last=False)


def load_profile(path):
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_context(path, limit):
    if not path:
        return ""
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if limit > 0 and len(text) > limit:
        return text[:limit].rstrip()
    return text


class TranslateHandler(BaseHTTPRequestHandler):
    server_version = "PlayTranslateHTTP/0.2"

    def send_json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self.send_json(
                200,
                {
                    "ok": True,
                    "backend": self.server.provider.name,
                    "model": getattr(self.server.provider, "model", None),
                    "context": bool(self.server.context_text),
                    "profile": bool(self.server.profile),
                },
            )
            return
        self.send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/translate":
            self.send_json(404, {"error": "not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            raw_speaker = str(payload.get("speaker") or "").strip()
            speaker = sanitize_speaker(raw_speaker, self.server.profile, self.server.context_text)
            text = str(payload.get("text") or "").strip()
            target_lang = str(payload.get("target_lang") or "Japanese").strip()
            source_lang = str(payload.get("source_lang") or "English").strip()
            if not text:
                self.send_json(400, {"error": "text is required"})
                return

            provider = self.server.provider
            cache_key = (provider.name, source_lang, target_lang, speaker, text)
            translation = self.server.cache.get(cache_key)
            cached = translation is not None
            if not cached:
                translation = provider.translate(
                    speaker=speaker,
                    text=text,
                    target_lang=target_lang,
                    source_lang=source_lang,
                    profile=self.server.profile,
                    context_text=self.server.context_text,
                )
                self.server.cache.put(cache_key, translation)

            self.send_json(
                200,
                {
                    "translation": translation,
                    "backend": provider.name,
                    "model": getattr(provider, "model", None),
                    "speaker": speaker,
                    "raw_speaker": raw_speaker,
                    "text": text,
                    "target_lang": target_lang,
                    "cached": cached,
                },
            )
        except ApiKeyError as exc:
            self.send_json(401, {"error": str(exc), "error_type": "api_key_error"})
        except RateLimitError as exc:
            self.send_json(429, {"error": str(exc), "error_type": "rate_limit_error"})
        except NetworkError as exc:
            self.send_json(502, {"error": str(exc), "error_type": "network_error"})
        except ProviderError as exc:
            self.send_json(500, {"error": str(exc), "error_type": "provider_error"})
        except Exception as exc:
            self.send_json(500, {"error": str(exc)})

    def log_message(self, fmt, *args):
        print(f"{self.client_address[0]} - {fmt % args}")


def main():
    parser = argparse.ArgumentParser(description="Minimal PlayTranslate translation HTTP server.")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host.")
    parser.add_argument("--port", type=int, default=8787, help="Bind port.")
    parser.add_argument(
        "--backend",
        choices=["dummy", "ollama", "gemini", "google", "google_cloud", "deepl"],
        default="dummy",
        help="Translation backend.",
    )
    parser.add_argument("--model", default=None, help="Model name (Ollama/Gemini); defaults to a sensible model per backend.")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434", help="Ollama base URL.")
    parser.add_argument(
        "--api-key",
        # Prefer the env var over a CLI flag for anything that spawns us
        # (see main.py's _ensure_translate_server): a secret on the command
        # line is visible to any other process on the box via `ps`/
        # /proc/<pid>/cmdline, an env var isn't. The flag stays for
        # convenient manual/local testing.
        default=os.environ.get("PLAYTRANSLATE_ENGINE_API_KEY", ""),
        help=(
            "API key for the selected backend (Gemini/DeepL/Google Cloud). "
            "Prefer PLAYTRANSLATE_ENGINE_API_KEY instead when scripting this."
        ),
    )
    parser.add_argument("--profile", type=Path, help="Game profile JSON used as translation context.")
    parser.add_argument("--context", type=Path, help="Plain text game notes used as translation context.")
    parser.add_argument("--context-limit", type=int, default=3000, help="Maximum characters read from --context. Use 0 for no limit.")
    args = parser.parse_args()

    if args.backend == "ollama":
        provider = create_provider("ollama", url=args.ollama_url, model=args.model or "translategemma")
    elif args.backend == "gemini":
        provider = create_provider("gemini", api_key=args.api_key, model=args.model or "gemini-3.6-flash")
    elif args.backend == "deepl":
        provider = create_provider("deepl", api_key=args.api_key)
    elif args.backend == "google_cloud":
        provider = create_provider("google_cloud", api_key=args.api_key)
    else:
        provider = create_provider(args.backend)

    server = ThreadingHTTPServer((args.host, args.port), TranslateHandler)
    server.provider = provider
    server.cache = TranslationCache()
    server.profile = load_profile(args.profile)
    server.context_text = load_context(args.context, args.context_limit)
    print(
        f"Listening on http://{args.host}:{args.port} "
        f"backend={provider.name} model={getattr(provider, 'model', None)} "
        f"profile={bool(server.profile)} context={bool(server.context_text)}"
    )
    server.serve_forever()


if __name__ == "__main__":
    raise SystemExit(main())
