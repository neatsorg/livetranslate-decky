#!/usr/bin/env python3
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image


def require_command(name):
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"{name} was not found in PATH")
    return path


def normalize_text(text):
    return "\n".join(line.strip() for line in text.splitlines() if line.strip()).strip()


def is_useful_text(text):
    compact = "".join(ch for ch in text if ch.isalnum())
    return len(compact) >= 2


def alpha_count(text):
    return sum(1 for ch in text if ch.isalpha())


def cleanup_line_best_alpha(text):
    candidates = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        letters = alpha_count(line)
        alnum = sum(1 for ch in line if ch.isalnum())
        if letters < 2:
            continue
        score = letters * 3 + alnum - len(line)
        candidates.append((score, letters, len(line), line))
    if not candidates:
        return ""
    candidates.sort(reverse=True)
    return candidates[0][3]


def cleanup_text(text, region):
    cleanup = region.get("cleanup")
    if isinstance(cleanup, str):
        mode = cleanup
    elif isinstance(cleanup, dict):
        mode = cleanup.get("mode", "none")
    else:
        mode = "none"

    if mode in ("none", ""):
        return text
    if mode == "line_best_alpha":
        return cleanup_line_best_alpha(text)
    raise ValueError(f"Unknown cleanup mode: {mode}")


def region_number(region, key, source_size, default):
    percent_key = f"{key}_pct"
    normalized_key = f"{key}_rel"
    if percent_key in region:
        return int(round(float(region[percent_key]) * source_size / 100.0))
    if normalized_key in region:
        return int(round(float(region[normalized_key]) * source_size))
    return int(region.get(key, default))


def crop_image(source, region):
    left = max(region_number(region, "x", source.width, 0), 0)
    top = max(region_number(region, "y", source.height, 0), 0)
    width = region_number(region, "width", source.width, source.width - left)
    height = region_number(region, "height", source.height, source.height - top)
    right = min(left + width, source.width)
    bottom = min(top + height, source.height)
    if right <= left or bottom <= top:
        raise ValueError(
            f"OCR region is outside image bounds: "
            f"x={left} y={top} width={width} height={height} "
            f"image={source.width}x{source.height}"
        )
    return source.crop((left, top, right, bottom))


def binarize_image(image, threshold):
    gray = image.convert("L")
    return gray.point(lambda p: 0 if p >= threshold else 255).convert("RGB")


def prepare_image(image_path, region, suffix):
    resize = int(region.get("resize", 100))
    no_crop = bool(region.get("no_crop", False))
    threshold = region.get("white_text_threshold")
    if no_crop and resize == 100 and threshold is None:
        return image_path

    output = Path(tempfile.gettempdir()) / f"playtranslate_ocr_{suffix}.png"
    with Image.open(image_path) as image:
        image = image.convert("RGB")
        if not no_crop:
            image = crop_image(image, region)
        if resize != 100:
            width = max(int(image.width * resize / 100), 1)
            height = max(int(image.height * resize / 100), 1)
            image = image.resize((width, height), Image.Resampling.LANCZOS)
        if threshold is not None:
            image = binarize_image(image, int(threshold))
        image.save(output)
    return output


def run_tesseract(tesseract, image, lang, psm, oem):
    command = [
        tesseract,
        str(image),
        "stdout",
        "-l",
        lang,
        "--psm",
        str(psm),
        "--oem",
        str(oem),
    ]
    result = subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return normalize_text(result.stdout)


def load_regions(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    return data.get("ocr_regions") or data.get("regions") or []


def default_region(args):
    return {
        "name": "text",
        "role": "text",
        "x": args.crop_x,
        "y": args.crop_y,
        "width": args.crop_width,
        "height": args.crop_height,
        "resize": args.resize,
        "psm": args.psm,
        "oem": args.oem,
        "lang": args.lang,
        "no_crop": args.no_crop,
    }


def ocr_region(args, tesseract, region, index):
    name = region.get("name") or f"region_{index}"
    role = region.get("role", "text")
    safe_name = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in name)
    try:
        ocr_image = prepare_image(args.image, region, f"{index}_{safe_name}")
        text = run_tesseract(
            tesseract,
            ocr_image,
            region.get("lang", args.lang),
            int(region.get("psm", args.psm)),
            int(region.get("oem", args.oem)),
        )
        cleaned_text = cleanup_text(text, region)
        error = None
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        text = ""
        cleaned_text = ""
        error = str(exc)
    return {
        "name": name,
        "role": role,
        "text": cleaned_text,
        "raw_text": text,
        "useful": is_useful_text(cleaned_text),
        **({"error": error} if error else {}),
    }


def print_plain_result(results):
    speakers = [r["text"] for r in results if r["role"] == "speaker" and r["useful"]]
    texts = [r["text"] for r in results if r["role"] == "text" and r["useful"]]
    contexts = [r["text"] for r in results if r["role"] == "context" and r["useful"]]

    if speakers:
        print(f"Speaker: {speakers[0]}")
    if texts:
        print("\n\n".join(texts))
    elif contexts:
        print("\n\n".join(contexts))


def main():
    parser = argparse.ArgumentParser(description="OCR a settled PlayTranslate crop with Pillow and Tesseract.")
    parser.add_argument("image", type=Path, help="Input image, usually last_settled.png.")
    parser.add_argument("--lang", default="eng", help="Tesseract language.")
    parser.add_argument("--psm", type=int, default=6, help="Tesseract page segmentation mode.")
    parser.add_argument("--oem", type=int, default=1, help="Tesseract OCR engine mode.")
    parser.add_argument("--no-crop", action="store_true", help="Do not crop before OCR.")
    parser.add_argument("--crop-x", type=int, default=80, help="OCR crop X offset.")
    parser.add_argument("--crop-y", type=int, default=20, help="OCR crop Y offset.")
    parser.add_argument("--crop-width", type=int, default=900, help="OCR crop width.")
    parser.add_argument("--crop-height", type=int, default=170, help="OCR crop height.")
    parser.add_argument("--resize", type=int, default=100, help="Resize percentage after crop.")
    parser.add_argument("--regions-json", type=Path, help="JSON file containing multiple OCR regions.")
    parser.add_argument("--json", action="store_true", help="Print OCR results as JSON.")
    args = parser.parse_args()

    if not args.image.exists():
        raise FileNotFoundError(args.image)

    tesseract = require_command("tesseract")
    regions = load_regions(args.regions_json) if args.regions_json else [default_region(args)]
    if not regions:
        raise ValueError("No OCR regions configured")

    results = [ocr_region(args, tesseract, region, index) for index, region in enumerate(regions)]
    if args.json:
        print(json.dumps({"regions": results}, ensure_ascii=False, indent=2))
    else:
        print_plain_result(results)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"ocr failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
