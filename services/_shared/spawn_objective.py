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


def resolve_objective(
    objective: object, *, objective_supported: object, parent_stream_id: object,
    brief: str = "", title: object = None, objective_source: object = None,
) -> tuple[object, str, str | None]:
    """Derive absent objectives and bound explicit objectives to the roster limit."""
    blank = objective is None or (isinstance(objective, str) and not objective.strip())
    if blank:
        return derived_objective(brief, title), "derived", None
    if isinstance(objective, str):
        objective = objective[:120]
    error = None
    if not isinstance(objective, str) or len(objective.splitlines()) != 1 or any(
        char in objective for char in "\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029"
    ):
        error = "objective_invalid"
    parent_absent = not (str(parent_stream_id).strip() if parent_stream_id is not None else "")
    if parent_absent and error is None and objective_source == "derived":
        return objective, "derived", None
    return objective, "explicit", error
