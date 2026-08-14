#!/usr/bin/env python3
import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    t_process_start = time.monotonic()

    parser = argparse.ArgumentParser(description="Run OCR and HTTP translation in one Python process.")
    parser.add_argument("image", type=Path, help="Input image, usually last_settled.png.")
    parser.add_argument("--regions-json", type=Path, required=True, help="OCR region JSON.")
    parser.add_argument("--http-url", required=True, help="Translation HTTP endpoint.")
    parser.add_argument("--target-lang", default="Japanese", help="Target translation language.")
    parser.add_argument("--ocr-script", type=Path, default=Path(__file__).with_name("ocr_tesseract.py"))
    parser.add_argument("--translate-script", type=Path, default=Path(__file__).with_name("translate_stub.py"))
    parser.add_argument("--json-output", type=Path, help="Write full OCR/translation JSON here.")
    args = parser.parse_args()

    t_modules_start = time.monotonic()
    ocr = load_module(args.ocr_script, "playtranslate_ocr_tesseract")
    translate = load_module(args.translate_script, "playtranslate_translate_stub")
    t_modules_end = time.monotonic()

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
    t_ocr_start = time.monotonic()
    region_timings = []
    ocr_results = []
    for index, region in enumerate(regions):
        t_region_start = time.monotonic()
        ocr_results.append(ocr.ocr_region(ocr_args, tesseract, region, index))
        region_timings.append(
            {"name": region.get("name") or f"region_{index}", "seconds": round(time.monotonic() - t_region_start, 3)}
        )
    t_ocr_end = time.monotonic()
    ocr_json = {"regions": ocr_results}

    speaker = translate.pick_speaker(ocr_json)
    text = translate.strip_leading_speaker(translate.collect_text(ocr_json), speaker)
    if not text:
        raise SystemExit("No useful text region found")

    t_http_start = time.monotonic()
    result = translate.post_http(args.http_url, speaker, text, args.target_lang)
    t_http_end = time.monotonic()
    translation = str(result.get("translation") or "").strip()

    timing = {
        "process_start_to_args_parsed_s": round(t_modules_start - t_process_start, 3),
        "module_load_s": round(t_modules_end - t_modules_start, 3),
        "ocr_total_s": round(t_ocr_end - t_ocr_start, 3),
        "ocr_regions": region_timings,
        "http_translate_s": round(t_http_end - t_http_start, 3),
        "process_total_s": round(time.monotonic() - t_process_start, 3),
    }

    if args.json_output:
        payload = {
            "translation": translation,
            "ocr": ocr_json,
            "http": result,
            "url": args.http_url,
            "timing": timing,
        }
        args.json_output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(translation)
    print(f"[timing] {json.dumps(timing, ensure_ascii=False)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
