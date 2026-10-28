"""Unmodified schema + loader from satellite.py@7dd19aad; downgrade oracle.

Extracted source SHA256: 193681505db7241210981993b5eb6c612e43759f336bcf0349d123f6558dca1e
"""
from dataclasses import dataclass, field
import os
import json
import logging
from pathlib import Path
import chat_recovery
WS_MAX_SIZE = 4 * 1024 * 1024  # exact 7dd ceiling
log = logging.getLogger(__name__)
DEFAULT_CHAT_CHECKPOINT_PATH = "~/.local/state/pentacle/satellite_chat.json"

@dataclass
class _StreamTail:
    """Native-source tail state; checkpoints omit transient provenance context."""

    session_name: str
    path: str = ""
    offset: int = 0                     # byte offset of the last COMPLETE line acked
    provider_session_id: str = ""       # identity guard against a foreign transcript
    provider: str = ""                  # inferred from the bound transcript path
    source_pane_pid: str = ""           # ephemeral proof for a Codex push only
    primed: bool = False                # first-bind history-horizon seek applied?
    #: Cross-span provenance context (Codex identity, turn models); reset on rebind.
    provenance_state: dict = field(default_factory=dict)
    binding: dict = field(default_factory=dict)
    observed_size: int = 0
    observed_digest: str = ""
    recovery_range: dict | None = None
    recovery_range_id: str = ''
    recovery_parent: dict = field(default_factory=dict)
    recovery_gap: str = ""
    partial_tail_bytes: int = 0


class Loader:
    def _load_chat_checkpoint(self):
        path = Path(os.path.expanduser(self.config.chat_checkpoint_path or DEFAULT_CHAT_CHECKPOINT_PATH))
        self._chat_checkpoint_path = path
        self._recovering = {}
        self._chat_gap = ''
        self._pass_recovery = []
        self._pass_sources = []
        self._pass_chat_gaps = []
        try:
            if path.stat().st_size > WS_MAX_SIZE:
                raise ValueError('oversized metadata checkpoint')
            data = json.loads(path.read_text())
            if not isinstance(data, dict) or data.get('version') != 1 or data.get('host') != self.config.host:
                raise ValueError('checkpoint version/host mismatch')
            if not isinstance(data.get('sources'), list) or len(data['sources']) > chat_recovery.MAX_SOURCES:
                raise ValueError('invalid source checkpoint list')
            for item in data['sources']:
                tail = _StreamTail(**item)
                if (type(tail.offset) is not int or tail.offset < 0
                        or type(tail.observed_size) is not int or tail.observed_size < tail.offset
                        or not isinstance(tail.binding, dict)
                        or not isinstance(tail.path, str) or not isinstance(tail.session_name, str)):
                    raise ValueError('malformed source checkpoint')
                if tail.binding and (not chat_recovery.source_valid(tail.binding, self.config.host)
                        or not isinstance(tail.binding.get('checkpoint_id'), str)):
                    raise ValueError('malformed original binding')
                fixed = tail.recovery_range
                if (not isinstance(tail.recovery_parent, dict)
                        or (tail.recovery_parent and (set(tail.recovery_parent) != {'range_id', 'end', 'digest'}
                            or not isinstance(tail.recovery_parent['range_id'], str)
                            or chat_recovery.HEX32.fullmatch(tail.recovery_parent['range_id']) is None
                            or not chat_recovery.offset(tail.recovery_parent['end'])
                            or tail.recovery_parent['end'] > tail.offset
                            or not isinstance(tail.recovery_parent['digest'], str)
                            or chat_recovery.HEX64.fullmatch(tail.recovery_parent['digest']) is None))
                        or (fixed is None and tail.recovery_range_id)
                        or (fixed is not None and (not isinstance(tail.recovery_range_id, str)
                            or chat_recovery.HEX32.fullmatch(tail.recovery_range_id) is None))):
                    raise ValueError('malformed recovery range identity')
                if fixed is not None and (not isinstance(fixed, dict) or set(fixed) != {'start', 'end', 'digest'}
                        or not chat_recovery.offset(fixed.get('start')) or not chat_recovery.offset(fixed.get('end'))
                        or not fixed['start'] <= tail.offset <= fixed['end']
                        or not isinstance(fixed.get('digest'), str)
                        or chat_recovery.HEX64.fullmatch(fixed['digest']) is None):
                    raise ValueError('malformed fixed range')
                key = (tail.binding or {}).get('checkpoint_id') or tail.session_name
                self._recovering[key] = tail
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError) as exc:
            # Preserve the unreadable preimage; never cold-seek past its data.
            self._chat_gap = 'checkpoint_corrupt'
            log.error('chat recovery gap: %s (%s)', self._chat_gap, type(exc).__name__)
