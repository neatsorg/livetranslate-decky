#!/usr/bin/env python3
import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import request


def dummy_translate(speaker, text, target_lang):
    prefix = f"[{target_lang} draft]"
    speaker_line = f"{speaker}: " if speaker else ""
    return f"{prefix} {speaker_line}{text}"


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


def context_speaker_aliases(context_text):
    aliases = set()
    for line in context_text.splitlines():
        stripped = line.strip()
        if not stripped or ":" not in stripped:
            continue
        head = stripped.split(":", 1)[0]
        for alias in head.split("/"):
            alias = alias.strip()
            if re.fullmatch(r"[A-Za-z][A-Za-z .'-]{1,40}", alias):
                aliases.add(alias.upper())
    return aliases


def profile_speaker_aliases(profile):
    aliases = set()
    for name, description in (profile.get("characters") or {}).items():
        aliases.add(str(name).strip().upper())
        if isinstance(description, dict):
            alias_for = description.get("alias_for")
            if alias_for:
                aliases.add(str(alias_for).strip().upper())
            display_name = description.get("name")
            if display_name:
                aliases.add(str(display_name).strip().upper())
    return aliases


def sanitize_speaker(speaker, profile, context_text):
    speaker = re.sub(r"\s+", " ", speaker.strip().upper())
    if not speaker:
        return ""
    aliases = profile_speaker_aliases(profile) | context_speaker_aliases(context_text)
    if aliases and speaker not in aliases:
        return ""
    if "/" in speaker or len(speaker) > 32:
        return ""
    if not re.fullmatch(r"[A-Z][A-Z .'-]{1,31}", speaker):
        return ""
    return speaker


def profile_context(profile, speaker):
    if not profile:
        return ""

    lines = []
    title = profile.get("title")
    if title:
        lines.append(f"Title: {title}")
    context = profile.get("context")
    if context:
        lines.append(f"Context: {context}")

    policy = profile.get("translation_policy") or []
    if policy:
        lines.append("Policy: " + " / ".join(str(item) for item in policy))

    terms = profile.get("terms") or {}
    if terms:
        lines.append("Terms: " + "; ".join(f"{key}: {value}" for key, value in terms.items()))

    characters = profile.get("characters") or {}
    current = characters.get(speaker) if speaker else None
    if isinstance(current, dict) and "alias_for" in current:
        current = characters.get(current["alias_for"], current)
    if speaker and current:
        lines.append(f"Current speaker: {speaker} - {current}")
    elif characters:
        lines.append("Characters: " + " / ".join(f"{name}: {description}" for name, description in characters.items()))

    return "\n".join(lines)


def build_context(profile, context_text, speaker):
    blocks = []
    profile_text = profile_context(profile, speaker)
    if profile_text:
        blocks.append(profile_text)
    if context_text:
        blocks.append(context_text)
    return "\n\n".join(blocks).strip()


def build_prompt(speaker, text, target_lang, profile, context_text):
    context = build_context(profile, context_text, speaker)
    context_block = f"\n作品メモ:\n{context}\n" if context else ""
    speaker_line = f"話者: {speaker}\n" if speaker else ""
    return (
        "あなたはプロのゲームローカライズ翻訳者です。\n"
        "英語のゲーム会話を自然な日本語に翻訳してください。\n"
        "出力は日本語訳のみ。解説や注釈は禁止。\n"
        "会話文の冒頭に話者の名前を書いたり、カギカッコで囲んだりする必要はありません。出力は訳文のみです。\n"
        "最優先するのは原文の意味です。主語・目的語・時制・肯定や否定・因果関係を変えないでください。\n"
        "話者の性別、年齢、立場、性格に応じた、自然な日本語の一人称と口調で翻訳してください。\n"
        "話者の性別・年齢・立場がわからない場合には、一人称を省略した日本語訳文にしてかまいません。\n"
        "作品メモは、話者の一人称と口調を決定する目的で使ってください。\n"
        "作品メモは、話者の一人称と口調を決定する以外の目的で使わないでください。たとえば作品メモを理由に、原文にない内容・行動・説明を追加したり、原文の意味を言い換えすぎたりしないでください。\n"
        "短い文や、最後の文や、カンマの後ろの部分などを、省略することを禁止します。原文に含まれる各文を、すべて訳してください。\n"
        "原文が省略記号で終わっていない限り、文章を「…」や「……」で終わらせることを禁止します。\n"
        "OCR由来の明らかな誤りは自然に補正してください。\n"
        "ただし、欠けている文や読めない語を勝手に補完しすぎないでください。\n"
        "英語の間投詞やスラングは自然な日本語に置き換えてください。\n"
        "固有名詞は、作品メモに日本語名がある場合はそれを使い、なければ英字のまま残してください。\n"
        "原文と関係ない語句が混ざっていると判断した場合は、翻訳に含めないでください。\n"
        f"{context_block}\n"
        f"{speaker_line}"
        f"原文:\n{text}\n\n"
        f"{target_lang}訳:\n"
    )


def sanitize_translation(text):
    text = text.strip()
    text = re.sub(r"^```(?:[a-zA-Z0-9_-]+)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    prefixes = (
        "日本語訳:",
        "Japanese translation:",
        "Translation:",
        "訳:",
    )
    for prefix in prefixes:
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    return text.strip()


def ollama_generate(url, model, prompt):
    payload = json.dumps(
        {
            "model": model,
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
        url.rstrip("/") + "/api/generate",
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with request.urlopen(req, timeout=120) as response:
        data = json.loads(response.read().decode("utf-8"))
    return sanitize_translation(str(data.get("response") or ""))


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
                    "backend": self.server.backend,
                    "model": self.server.model,
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
            if not text:
                self.send_json(400, {"error": "text is required"})
                return

            backend = self.server.backend
            prompt = build_prompt(speaker, text, target_lang, self.server.profile, self.server.context_text)
            if backend == "ollama":
                translation = ollama_generate(self.server.ollama_url, self.server.model, prompt)
            elif backend == "dummy":
                translation = dummy_translate(speaker, text, target_lang)
            else:
                self.send_json(500, {"error": f"unknown backend: {backend}"})
                return

            self.send_json(
                200,
                {
                    "translation": translation,
                    "backend": backend,
                    "model": self.server.model if backend == "ollama" else None,
                    "speaker": speaker,
                    "raw_speaker": raw_speaker,
                    "text": text,
                    "target_lang": target_lang,
                    "prompt": prompt,
                },
            )
        except Exception as exc:
            self.send_json(500, {"error": str(exc)})

    def log_message(self, fmt, *args):
        print(f"{self.client_address[0]} - {fmt % args}")


def main():
    parser = argparse.ArgumentParser(description="Minimal PlayTranslate translation HTTP server.")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host.")
    parser.add_argument("--port", type=int, default=8787, help="Bind port.")
    parser.add_argument("--backend", choices=["dummy", "ollama"], default="dummy", help="Translation backend.")
    parser.add_argument("--model", default="translategemma", help="Ollama model name.")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434", help="Ollama base URL.")
    parser.add_argument("--profile", type=Path, help="Game profile JSON used as translation context.")
    parser.add_argument("--context", type=Path, help="Plain text game notes used as translation context.")
    parser.add_argument("--context-limit", type=int, default=3000, help="Maximum characters read from --context. Use 0 for no limit.")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), TranslateHandler)
    server.backend = args.backend
    server.model = args.model
    server.ollama_url = args.ollama_url
    server.profile = load_profile(args.profile)
    server.context_text = load_context(args.context, args.context_limit)
    print(
        f"Listening on http://{args.host}:{args.port} "
        f"backend={args.backend} model={args.model} "
        f"profile={bool(server.profile)} context={bool(server.context_text)}"
    )
    server.serve_forever()


if __name__ == "__main__":
    raise SystemExit(main())
