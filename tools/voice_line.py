"""Deterministic spoken-line limits. Semantic quality is reviewed separately."""
import re


def check_line(text):
    if not isinstance(text, str) or not text.strip():
        return "empty_text"
    if len(text) > 300:
        return "characters"
    if len(text.split()) > 40:
        return "words"
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
    # A decimal point inside a number is not a sentence boundary.
    sentence_text = re.sub(r"(?<=\d)\.(?=\d)", "", text)
    sentences = [part for part in re.split(r"[.!?]+", sentence_text) if part.strip()]
    if len(sentences) > 2:
        return "sentences"
    numbers = re.findall(r"(?<!\w)[+-]?\d+(?:[.,]\d+)*(?:%?)(?!\w)", text)
    number_words = re.findall(r"\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million|billion)\b", text, re.I)
    if len(numbers) + len(number_words) > 3:
        return "numbers"
    if any(re.search(r"\.\d{2,}", number) for number in numbers):
        return "unrounded_number"
    return None
