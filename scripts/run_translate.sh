#!/usr/bin/env bash
set -euo pipefail

DATA_DIR="${PLAYTRANSLATE_DATA_DIR:-/home/deck/homebrew/data/PlayTranslate}"
ENGINE_DIR="${PLAYTRANSLATE_ENGINE_DIR:-/home/deck/homebrew/plugins/PlayTranslate/bin}"
OCR_BOX="${PLAYTRANSLATE_OCR_BOX:-playtranslate-ocr}"
TRANSLATE_URL="${PLAYTRANSLATE_TRANSLATE_URL:-}"
if [[ -z "${TRANSLATE_URL}" && -f "${DATA_DIR}/translate_url.txt" ]]; then
  TRANSLATE_URL="$(tr -d '[:space:]' < "${DATA_DIR}/translate_url.txt")"
fi
TRANSLATE_URL="${TRANSLATE_URL:-http://192.168.1.32:8787/translate}"
if [[ "${TRANSLATE_URL}" =~ ^https?://[^/]+/?$ ]]; then
  TRANSLATE_URL="${TRANSLATE_URL%/}/translate"
fi

IMAGE_PATH="${DATA_DIR}/captures/last_settled.png"
REGIONS_JSON="${PLAYTRANSLATE_REGIONS_JSON:-${ENGINE_DIR}/ocr_regions.enigma_of_fear.json}"
OCR_SCRIPT="${ENGINE_DIR}/ocr_tesseract.py"
TRANSLATE_SCRIPT="${ENGINE_DIR}/translate_stub.py"
PIPELINE_SCRIPT="${ENGINE_DIR}/translate_pipeline.py"
OUTPUT_TXT="${DATA_DIR}/last_translation.txt"
OUTPUT_JSON="${DATA_DIR}/last_translation.json"
OUTPUT_ERROR="${DATA_DIR}/last_translation_error.txt"
DEBUG_LOG="${DATA_DIR}/translation-debug.log"

mkdir -p "${DATA_DIR}"

{
  echo "--- PlayTranslate translate $(date '+%Y-%m-%d %H:%M:%S') ---"
  echo "DATA_DIR=${DATA_DIR}"
  echo "ENGINE_DIR=${ENGINE_DIR}"
  echo "OCR_BOX=${OCR_BOX}"
  echo "TRANSLATE_URL=${TRANSLATE_URL}"
  echo "IMAGE_PATH=${IMAGE_PATH}"
} >> "${DEBUG_LOG}"

if [[ ! -f "${IMAGE_PATH}" ]]; then
  echo "image not found: ${IMAGE_PATH}" | tee -a "${DEBUG_LOG}" >&2
  exit 2
fi

if [[ ! -f "${OCR_SCRIPT}" || ! -f "${TRANSLATE_SCRIPT}" || ! -f "${PIPELINE_SCRIPT}" || ! -f "${REGIONS_JSON}" ]]; then
  {
    echo "missing required file"
    echo "OCR_SCRIPT=${OCR_SCRIPT} exists=$([[ -f "${OCR_SCRIPT}" ]] && echo yes || echo no)"
    echo "TRANSLATE_SCRIPT=${TRANSLATE_SCRIPT} exists=$([[ -f "${TRANSLATE_SCRIPT}" ]] && echo yes || echo no)"
    echo "PIPELINE_SCRIPT=${PIPELINE_SCRIPT} exists=$([[ -f "${PIPELINE_SCRIPT}" ]] && echo yes || echo no)"
    echo "REGIONS_JSON=${REGIONS_JSON} exists=$([[ -f "${REGIONS_JSON}" ]] && echo yes || echo no)"
  } | tee -a "${DEBUG_LOG}" >&2
  exit 3
fi

DISTROBOX_START=$(date +%s.%N)
TRANSLATION="$(
  distrobox enter "${OCR_BOX}" -- python3 "${PIPELINE_SCRIPT}" \
    "${IMAGE_PATH}" \
    --regions-json "${REGIONS_JSON}" \
    --http-url "${TRANSLATE_URL}" \
    --json-output "${OUTPUT_JSON}" \
    2>>"${DEBUG_LOG}"
)"
DISTROBOX_END=$(date +%s.%N)
DISTROBOX_ELAPSED=$(awk -v a="${DISTROBOX_START}" -v b="${DISTROBOX_END}" 'BEGIN { printf "%.3f", b - a }')

printf '%s\n' "${TRANSLATION}" > "${OUTPUT_TXT}"
rm -f "${OUTPUT_ERROR}"

{
  echo "wrote ${OUTPUT_TXT}"
  echo "translation=${TRANSLATION}"
  echo "timing.distrobox_enter_to_exit_s=${DISTROBOX_ELAPSED}"
} >> "${DEBUG_LOG}"

printf '%s\n' "${TRANSLATION}"
