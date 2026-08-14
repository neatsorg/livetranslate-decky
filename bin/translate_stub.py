#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path
from urllib import request


def load_ocr(path):
    if path == Path("-"):
        return json.load(sys.stdin)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def useful_regions(data, role):
    return [
        region
        for region in data.get("regions", [])
        if region.get("role") == role and region.get("useful") and region.get("text", "").strip()
    ]


def pick_speaker(data):
    speakers = useful_regions(data, "speaker")
    if not speakers:
        return ""
    return max(speakers, key=lambda r: len(r.get("text", "").strip())).get("text", "").strip()


def collect_text(data):
    texts = useful_regions(data, "text")
    if texts:
        return "\n".join(region.get("text", "").strip() for region in texts).strip()

    contexts = useful_regions(data, "context")
    return "\n".join(region.get("text", "").strip() for region in contexts).strip()


def _normalize_for_match(value):
    return "".join(ch for ch in value.upper() if ch.isalnum())


def strip_leading_speaker(text, speaker):
    """Drop a leading line that just duplicates the speaker name.

    A body-text OCR region that is miscalibrated (or a game whose layout
    puts the name tag right above the dialogue) can capture the speaker
    name as the first line of the body text. Region tuning should be the
    real fix, but this is a cheap, engine-agnostic safety net for whatever
    slips through, on this game or any other.
    """
    if not text or not speaker:
        return text
    lines = text.split("\n")
    first, rest = lines[0].strip(), lines[1:]
    if _normalize_for_match(first) == _normalize_for_match(speaker):
        return "\n".join(rest).strip()
    return text


def build_prompt(speaker, text, target_lang):
    context_line = f"Speaker: {speaker}\n" if speaker else ""
    return (
        "You are translating game dialogue from OCR text. "
        "Correct obvious OCR errors silently when the intended text is clear. "
        "Preserve the character voice and avoid adding explanations.\n\n"
        f"Translate into {target_lang}.\n\n"
        f"{context_line}Text:\n{text}\n"
    )


def post_http(url, speaker, text, target_lang):
    payload = json.dumps(
        {
            "speaker": speaker,
            "text": text,
            "target_lang": target_lang,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    req = request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def main():
    parser = argparse.ArgumentParser(description="Prepare OCR output for translation.")
    parser.add_argument("ocr_json", type=Path, help="OCR JSON file, or '-' for stdin.")
    parser.add_argument("--target-lang", default="Japanese", help="Target translation language.")
    parser.add_argument("--http-url", help="POST speaker/text to a translation HTTP endpoint.")
    parser.add_argument("--json", action="store_true", help="Print structured JSON instead of a prompt.")
    args = parser.parse_args()

    data = load_ocr(args.ocr_json)
    speaker = pick_speaker(data)
    text = strip_leading_speaker(collect_text(data), speaker)
    if not text:
        raise SystemExit("No useful text region found")

    prompt = build_prompt(speaker, text, args.target_lang)
    if args.http_url:
        result = post_http(args.http_url, speaker, text, args.target_lang)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(result.get("translation", ""))
        return 0

    if args.json:
        print(json.dumps({"speaker": speaker, "text": text, "target_lang": args.target_lang, "prompt": prompt}, ensure_ascii=False, indent=2))
    else:
        print(prompt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
