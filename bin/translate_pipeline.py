#!/usr/bin/env python3
import argparse
import importlib.util
import json
import sys
from pathlib import Path


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description="Run OCR and HTTP translation in one Python process.")
    parser.add_argument("image", type=Path, help="Input image, usually last_settled.png.")
    parser.add_argument("--regions-json", type=Path, required=True, help="OCR region JSON.")
    parser.add_argument("--http-url", required=True, help="Translation HTTP endpoint.")
    parser.add_argument("--target-lang", default="Japanese", help="Target translation language.")
    parser.add_argument("--ocr-script", type=Path, default=Path(__file__).with_name("ocr_tesseract.py"))
    parser.add_argument("--translate-script", type=Path, default=Path(__file__).with_name("translate_stub.py"))
    parser.add_argument("--json-output", type=Path, help="Write full OCR/translation JSON here.")
    args = parser.parse_args()

    ocr = load_module(args.ocr_script, "playtranslate_ocr_tesseract")
    translate = load_module(args.translate_script, "playtranslate_translate_stub")

    if not args.image.exists():
        raise FileNotFoundError(args.image)
    if not args.regions_json.exists():
        raise FileNotFoundError(args.regions_json)

    tesseract = ocr.require_command("tesseract")
    regions = ocr.load_regions(args.regions_json)
    if not regions:
        raise ValueError("No OCR regions configured")

    ocr_args = argparse.Namespace(
        image=args.image,
        lang="eng",
        psm=6,
        oem=1,
        no_crop=False,
        crop_x=80,
        crop_y=20,
        crop_width=900,
        crop_height=170,
        resize=100,
    )
    ocr_results = [ocr.ocr_region(ocr_args, tesseract, region, index) for index, region in enumerate(regions)]
    ocr_json = {"regions": ocr_results}

    speaker = translate.pick_speaker(ocr_json)
    text = translate.collect_text(ocr_json)
    if not text:
        raise SystemExit("No useful text region found")

    result = translate.post_http(args.http_url, speaker, text, args.target_lang)
    translation = str(result.get("translation") or "").strip()

    if args.json_output:
        payload = {
            "translation": translation,
            "ocr": ocr_json,
            "http": result,
            "url": args.http_url,
        }
        args.json_output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(translation)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
