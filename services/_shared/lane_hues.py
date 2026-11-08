"""Ordered lane hue keys from the one shipped palette; no title inference."""
import json
import logging
from functools import lru_cache
from pathlib import Path

TABLE = json.loads((Path(__file__).resolve().parents[2] / 'pentacle-chat-core/lane_hues.json').read_text())
LANE_HUE_KEYS = tuple(item['key'] for item in TABLE['palette'])
assert TABLE['version'] == 1 and len(LANE_HUE_KEYS) == len(set(LANE_HUE_KEYS)) == 16

@lru_cache(maxsize=128)
def _diagnose(value):
    logging.getLogger(__name__).warning('work_lane_hue_invalid: projecting null (%s)', value)

def resolve_hue(value):
    if value is None or isinstance(value, str) and value in LANE_HUE_KEYS:
        return value
    _diagnose(str(value))
    return None
