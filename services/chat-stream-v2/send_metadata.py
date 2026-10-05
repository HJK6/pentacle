"""Ordinary-send metadata whitelist shared by ingress and receipt projection."""

from typing import Any


def normalize_send_meta(raw: object) -> dict[str, Any]:
    """Whitelist the additive ``meta`` a send may carry (voice-input lane).

    Only ``meta.voice.duration_s`` is persisted so a producer cannot smuggle
    arbitrary durable state onto the USER event. Returns ``{}`` for anything
    that is not a well-formed voice envelope."""
    if not isinstance(raw, dict):
        return {}
    voice = raw.get("voice")
    if not isinstance(voice, dict):
        return {}
    duration = voice.get("duration_s")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        return {}
    if duration < 0:
        return {}
    return {"voice": {"duration_s": round(float(duration), 3)}}
