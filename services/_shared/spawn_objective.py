"""The A-objective admission contract, shared by daemon and CLI producers."""
import unicodedata


def derived_objective(brief: str, title: object = None) -> str:
    """Bound a pre-field producer's existing text; never invent a task summary."""
    for text in (brief, title):
        if not isinstance(text, str):
            continue
        for line in text.splitlines():
            clean = "".join(char for char in line if unicodedata.category(char) not in {"Cc", "Cf", "Cs"}).strip()
            if clean:
                return clean[:120]
    return "New session"


def objective_error(value: object) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return "objective_required"
    if not isinstance(value, str) or len(value) > 120 or len(value.splitlines()) != 1:
        return "objective_invalid"
    if any(char in value for char in "\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029"):
        return "objective_invalid"
    return None
