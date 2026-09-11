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


def objective_required_for(objective_supported: object, parent_stream_id: object) -> bool:
    """The lineage-aware objective predicate.

    An objective is consumed only where it is projected: the parent's sub-agent
    roster. So it is required only for a parented child spawn produced by a
    protocol-aware caller. Top-level, `--top-level`, handoff, and
    scheduled-without-parent spawns (parent None) set their own goal and never
    require one; producers predating the objective protocol
    (`objective_supported` falsy \u2014 e.g. the desktop client) always derive.
    """
    parent = str(parent_stream_id).strip() if parent_stream_id is not None else ""
    return bool(objective_supported) and bool(parent)


def resolve_objective(
    objective: object, *, objective_supported: object, parent_stream_id: object,
    brief: str = "", title: object = None,
) -> tuple[object, str, str | None]:
    """Resolve `(objective, objective_source, error)` under the lineage rule.

    Strict (`objective_required_for`) \u2192 the objective is required and
    shape-validated; `error` is `objective_required`/`objective_invalid` on a
    blank/absent/malformed value. Not strict \u2192 a blank or absent objective is
    derived from the brief/title (`objective_source='derived'`), while an
    explicitly supplied objective is shape-validated and kept (`'explicit'`).
    A re-validation site that carries an already-resolved objective and no brief
    consumes only `error`: a non-strict blank yields no error (tolerated), a
    strict or malformed one still does.
    """
    strict = objective_required_for(objective_supported, parent_stream_id)
    blank_or_absent = objective is None or (isinstance(objective, str) and not objective.strip())
    if not strict and blank_or_absent:
        return derived_objective(brief, title), "derived", None
    return objective, "explicit", objective_error(objective)
