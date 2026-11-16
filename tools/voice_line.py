"""Deterministic spoken-line limits. Semantic quality is reviewed separately."""
import re


DEFAULT_LIMITS = dict(sentences_per_line=2, words_per_line=40, characters_per_line=300)


def counts(text):
    sentence_text = re.sub(r"(?<=\d)\.(?=\d)", "", text)
    return dict(characters_per_line=len(text), words_per_line=len(text.split()),
                sentences_per_line=len([s for s in re.split(r"[.!?]+", sentence_text) if s.strip()]))


def limit_refusal(text, limits):
    if not isinstance(text, str) or not text.strip():
        return None
    for key, measured in counts(text).items():
        if measured > limits[key]:
            return dict(outcome="refused", reason=key.split("_")[0], limit_name=key, limit=limits[key], measured=measured)
    return None


def check_expects_answer(text, limits=None):
    """A line flagged expects_answer must be a single direct question.

    Returns None when the line passes the ordinary checker and is exactly one
    question ending in "?"; otherwise a reason. A statement (no "?") or a line
    holding more than one question fails, so the evaluation run rejects it.
    """
    reason = check_line(text, limits)
    if reason:
        return reason
    if not isinstance(text, str):
        return "empty_text"
    if text.count("?") != 1 or not text.rstrip().endswith("?"):
        return "not_single_question"
    return None


def check_line(text, limits=None):
    if not isinstance(text, str) or not text.strip():
        return "empty_text"
    refusal = limit_refusal(text, limits or DEFAULT_LIMITS)
    if refusal:
        return refusal["reason"]
    if re.search(r"[\r\n\x00-\x1f\x7f]", text):
        return "markup"
    if re.search(r"[`*_#\[\]{}<>]|(?:^|\s)[-•]\s|\$\(", text):
        return "markup_or_code"
    if re.search(r"(?:https?://|www\.|\b\w+://|[\w.+-]+@[\w.-]+\.[a-z]{2,}|\b(?:[a-z0-9-]+\.)+[a-z]{2,63}\b)", text, re.I):
        return "link"
    if re.search(r"[/\\]|\b\w+:\w+|\b[a-z]+[A-Z]\w*|\b(?:0x[0-9a-f]+|[0-9a-f]{8,})\b|\b\w*\d+\w*[a-z]\w*\b|\b[a-z]\w*\d+\w*\b", text):
        return "identifier_or_path"
    if text.count('?') > 1:
        return "questions"
    numbers = re.findall(r"(?<!\w)[+-]?\d+(?:[.,]\d+)*(?:%?)(?!\w)", text)
    number_words = re.findall(r"\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million|billion)\b", text, re.I)
    if len(numbers) + len(number_words) > 3:
        return "numbers"
    if any(re.search(r"\.\d{2,}", number) for number in numbers):
        return "unrounded_number"
    return None
