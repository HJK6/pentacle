"""Operator-owned speech policy. Invalid reloads retain the last valid policy."""
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import threading

DEFAULTS = {
    'replies': {'characters_per_line': 300, 'lines_per_conversation': 4,
                'minimum_gap_seconds': 3, 'conversation_ceiling_seconds': 43200,
                'kickoff_deadline_seconds': 15},
    'clips': {'acknowledgement': ['On it.', 'Looking into it.', 'One moment.', 'Let me check.', 'Got it.', 'Working on it.'],
              'late_kickoff': ['Still working on it.'], 'fallback': ["I've replied in chat."],
              'silent_on': ['Silent mode is on.'], 'silent_off': ['Silent mode is off.'],
              'meeting_on': ['Meeting mode is on.'], 'meeting_off': ['Meeting mode is off.']},
    'origins': ['room_mic'],
    'modes': {'silent': {'on_phrases': ['silent mode on', 'be quiet', 'stop talking'],
                         'off_phrases': ['silent mode off', 'you can talk now', 'speak again'],
                         'suppresses': ['clips', 'lines']},
              'meeting': {'on_phrases': ['start meeting', 'start recording meeting'],
                          'off_phrases': ['stop meeting', 'end meeting'], 'suppresses': []}},
    'announcements': [], 'actions': ['chat'],
    'labels': {'room_mic': 'Room mic', 'mobile_dictation': 'Mobile dictation',
               'silent': 'Silent mode', 'meeting': 'Meeting mode'},
}


def validate(value):
    if not isinstance(value, dict) or set(value) != set(DEFAULTS):
        raise ValueError('Rules sections must match the shipped schema')
    replies = value['replies']
    if not isinstance(replies, dict) or set(replies) != set(DEFAULTS['replies']):
        raise ValueError('Invalid replies section')
    bounds = {'characters_per_line': (1, 800), 'lines_per_conversation': (1, 100),
              'minimum_gap_seconds': (0, 3600), 'conversation_ceiling_seconds': (1, 43200),
              'kickoff_deadline_seconds': (.01, 3600)}
    for key, (low, high) in bounds.items():
        n = replies[key]
        if type(n) not in (int, float) or not math.isfinite(n) or not low <= n <= high:
            raise ValueError('Invalid reply limit: '+key)
        if key in ('characters_per_line', 'lines_per_conversation') and type(n) is not int:
            raise ValueError('Reply count limits must be integers')
    def strings(items, *, allow_empty=True):
        if not isinstance(items, list) or (not allow_empty and not items) or any(not isinstance(s, str) or not s.strip() or len(s)>300 for s in items):
            raise ValueError('Expected a list of nonempty strings')
        if len(set(items)) != len(items):
            raise ValueError('Duplicate policy values')
    for key in ('origins', 'announcements', 'actions'):
        strings(value[key])
    clips = value['clips']
    if not isinstance(clips, dict) or set(clips) != set(DEFAULTS['clips']):
        raise ValueError('Invalid clips section')
    for phrases in clips.values():
        strings(phrases, allow_empty=False)
    if not 6 <= len(clips['acknowledgement']) <= 8:
        raise ValueError('Acknowledgement requires six to eight phrases')
    modes = value['modes']
    if not isinstance(modes, dict) or set(modes) != {'silent', 'meeting'}:
        raise ValueError('Invalid modes section')
    for mode in modes.values():
        if not isinstance(mode, dict) or set(mode) != {'on_phrases', 'off_phrases', 'suppresses'}:
            raise ValueError('Invalid mode controls')
        for phrases in mode.values():
            strings(phrases)
        if set(mode['suppresses']) - {'clips', 'lines'}:
            raise ValueError('Unknown mode suppression')
    labels = value['labels']
    if not isinstance(labels, dict) or set(labels) != set(DEFAULTS['labels']) or any(not isinstance(s,str) or not s.strip() for s in labels.values()):
        raise ValueError('Invalid labels section')
    return copy.deepcopy(value)


class Rules:
    def __init__(self, path=None):
        self.path = Path(path or os.environ['MIC_VOICE_RULES_FILE']) if path or os.environ.get('MIC_VOICE_RULES_FILE') else None
        self.lock = threading.RLock()
        self.value = copy.deepcopy(DEFAULTS)
        self.error = None
        self.version = self._version(self.value)
        self.reload()

    @staticmethod
    def _version(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:16]

    def reload(self, validate_extra=None):
        with self.lock:
            try:
                value = validate(json.loads(self.path.read_text())) if self.path else copy.deepcopy(DEFAULTS)
                if validate_extra:
                    validate_extra(value)
                self.value = value
                self.version = self._version(value)
                self.error = None
                return True
            except (ValueError, OSError, TypeError, KeyError) as exc:
                self.error = str(exc)
                return False

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.value)

    def status(self):
        with self.lock:
            return dict(version=self.version, error=self.error, modes=copy.deepcopy(self.value['modes']))
