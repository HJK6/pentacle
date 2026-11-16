"""No-device acceptance for the 10-04 activation gaps (one bounded candidate).

G1 (tone listener_busy): a spoken question's own listening tone must play through the
1s post-question recognition self-fence instead of being refused as listener_busy.
G2 (rules schema): a voice-rules.json persisted before the answer-window/silent keys
existed must still load (the new additive keys backfill from defaults), not be rejected.
"""
import copy
import json
from .test_speaker_service import service, opened, line
from voice_rules import Rules, DEFAULTS, validate


# --- G1: listening tone plays through the question's own recognition fence ---------------

def test_listening_tone_plays_through_question_self_fence(service):
    cid = opened(service)
    played = []
    orig = service.clips.play
    service.clips.play = lambda group, phrases, deadline, on_start=None: (
        played.append(group) or orig(group, phrases, deadline, on_start=on_start))
    result = line(service, cid, expects_answer=True)
    assert result['outcome'] == 'spoken'
    # The window still opens...
    window = service.answer_window(cid)
    assert window and window['ready'] is True
    # ...and the listening tone actually PLAYS (it is not refused by the question's 1s self-fence).
    assert 'listening_tone' in played, 'listening tone was refused (listener_busy) instead of playing'
    # The opened telemetry records that the tone played, for activation-proof auditability.
    opened_ev = [e for e in service.events if e.get('subsystem') == 'voice.answer_window'
                 and e.get('event') == 'opened']
    assert opened_ev and opened_ev[-1].get('tone_played') is True


def test_tone_off_opens_window_and_plays_no_tone(service, tmp_path, monkeypatch):
    # With listening_tone disabled, no tone plays and the opened event records tone_played False.
    policy = copy.deepcopy(DEFAULTS)
    policy['replies']['listening_tone'] = False
    path = tmp_path / 'rules.json'
    path.write_text(json.dumps(policy))
    monkeypatch.setenv('MIC_VOICE_RULES_FILE', str(path))
    service.rules = Rules()
    cid = opened(service)
    played = []
    orig = service.clips.play
    service.clips.play = lambda group, phrases, deadline, on_start=None: (
        played.append(group) or orig(group, phrases, deadline, on_start=on_start))
    assert line(service, cid, expects_answer=True)['outcome'] == 'spoken'
    assert 'listening_tone' not in played
    opened_ev = [e for e in service.events if e.get('subsystem') == 'voice.answer_window'
                 and e.get('event') == 'opened']
    assert opened_ev and opened_ev[-1].get('tone_played') is False


# --- G2: rules file predating the new keys still loads (additive backfill) ----------------

def _preexisting_policy():
    policy = copy.deepcopy(DEFAULTS)
    # A rules file persisted before this spec: no answer-window/tone/question keys, no tone clip.
    for key in ('answer_window_seconds', 'listening_tone', 'questions_per_conversation'):
        policy['replies'].pop(key, None)
    policy['clips'].pop('listening_tone', None)
    return policy


def test_preexisting_rules_file_loads_with_backfilled_defaults(tmp_path):
    path = tmp_path / 'voice-rules.json'
    path.write_text(json.dumps(_preexisting_policy()))
    rules = Rules(path)
    # The old-schema file loads cleanly rather than being rejected as 'Invalid replies section'.
    assert rules.error is None
    snap = rules.snapshot()
    assert snap['replies']['answer_window_seconds'] == DEFAULTS['replies']['answer_window_seconds']
    assert snap['replies']['listening_tone'] is DEFAULTS['replies']['listening_tone']
    assert snap['replies']['questions_per_conversation'] == DEFAULTS['replies']['questions_per_conversation']
    assert snap['clips']['listening_tone'] == DEFAULTS['clips']['listening_tone']


def test_backfill_preserves_operator_values_and_still_rejects_unknown_keys(tmp_path):
    # Operator-set values for the new keys are preserved (setdefault does not override).
    policy = _preexisting_policy()
    policy['replies']['answer_window_seconds'] = 45
    policy['replies']['listening_tone'] = False
    policy['replies']['questions_per_conversation'] = 7
    assert validate(policy)['replies']['answer_window_seconds'] == 45
    assert validate(policy)['replies']['listening_tone'] is False
    # Strictness is preserved: an unknown reply key is still rejected.
    bad = _preexisting_policy()
    bad['replies']['surprise_key'] = 1
    try:
        validate(bad)
        assert False, 'unknown reply key should be rejected'
    except ValueError:
        pass
