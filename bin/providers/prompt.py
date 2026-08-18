import re


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


_JAPANESE_NAMES = {"japanese", "ja", "jp", "日本語"}


def _is_japanese(lang):
    return lang.strip().lower() in _JAPANESE_NAMES


def build_prompt(speaker, text, target_lang, profile, context_text, source_lang="English"):
    """Build the LLM prompt used by LLM-style providers (Ollama, Gemini).

    The hand-tuned Japanese instructions below (character-voice first-person
    pronoun selection etc.) are Japanese-specific and don't generalize to
    other target languages, so they stay as a dedicated branch. Any other
    target_lang falls back to a language-neutral English instruction template
    with equivalent intent (preserve meaning, don't drop sentences, don't
    hallucinate, respect glossary terms).
    """
    context = build_context(profile, context_text, speaker)

    if _is_japanese(target_lang):
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

    context_block = f"\nNotes:\n{context}\n" if context else ""
    speaker_line = f"Speaker: {speaker}\n" if speaker else ""
    return (
        "You are a professional game localization translator.\n"
        f"Translate the following {source_lang} game dialogue into natural {target_lang}.\n"
        "Output only the translation - no explanations or notes.\n"
        "Do not prefix the line with the speaker's name or wrap it in quotes.\n"
        "Preserve the original meaning above all else: keep the subject, object, tense, and polarity unchanged.\n"
        "Match the speaker's voice and tone to their apparent gender, age, and role when it is clear from context; keep it neutral otherwise.\n"
        "Use the notes only to inform tone and terminology, never to add content, actions, or explanations absent from the source text.\n"
        "Do not omit any sentence, including short trailing clauses - translate every sentence in the source.\n"
        "Do not end the translation with an ellipsis unless the source itself ends with one.\n"
        "Silently correct obvious OCR errors, but do not invent missing or unreadable text.\n"
        "Keep proper nouns as given unless the notes provide a localized name.\n"
        "Ignore any text that appears unrelated to the source dialogue.\n"
        f"{context_block}\n"
        f"{speaker_line}"
        f"Source ({source_lang}):\n{text}\n\n"
        f"{target_lang} translation:\n"
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
