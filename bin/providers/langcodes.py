_NAME_TO_CODE = {
    "auto": "auto",
    "english": "en",
    "japanese": "ja",
    "korean": "ko",
    "chinese": "zh",
    "spanish": "es",
    "french": "fr",
    "german": "de",
}


def to_code(lang):
    """Best-effort mapping from a human language name (as used elsewhere in
    this codebase, e.g. "Japanese") to an ISO 639-1-ish code for MT APIs.

    Already-a-code input ("ja", "en") passes through unchanged. Only
    English/Japanese are exercised today; the table is easy to extend later
    without touching call sites.
    """
    lang = (lang or "").strip()
    if not lang:
        return "auto"
    lowered = lang.lower()
    return _NAME_TO_CODE.get(lowered, lowered)
