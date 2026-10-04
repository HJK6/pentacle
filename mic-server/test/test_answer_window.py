"""No-device acceptance for the Bart answer window on the speaker service."""
import json
from .test_speaker_service import service, opened, line
from voice_rules import Rules, DEFAULTS
import copy


def rules_file(tmp_path, monkeypatch, service, **reply_changes):
    policy = copy.deepcopy(DEFAULTS)
    policy['replies'].update(reply_changes)
    path = tmp_path / 'rules.json'
    path.write_text(json.dumps(policy))
    monkeypatch.setenv('MIC_VOICE_RULES_FILE', str(path))
    service.rules = Rules()
    return path


def test_question_returns_line_id_and_opens_window_with_tone(service):
    cid = opened(service)
    before = len(service.speaker.renderer.calls)
    result = line(service, cid, expects_answer=True)
    assert result['outcome'] == 'spoken'
    assert isinstance(result.get('line_id'), str) and len(result['line_id']) == 32
    window = service.answer_window(cid)
    assert window and window['line_id'] == result['line_id'] and window['ready'] is True
    # The listening tone is played (default on): a fresh clip render is not needed, but the
    # tone clip is a distinct spoken clip; status surfaces the waiting state.
    assert service.status()['answer_window'] == dict(waiting=True, conversation_id=cid,
                                                      line_id=result['line_id'], ready=True, expires_in=20)


def test_listening_tone_off_opens_window_without_rendering_tone(service, tmp_path, monkeypatch):
    rules_file(tmp_path, monkeypatch, service, listening_tone=False)
    cid = opened(service)
    before = len(service.speaker.renderer.calls)
    result = line(service, cid, expects_answer=True)
    # The question line itself renders; no extra tone render follows it.
    assert result['outcome'] == 'spoken'
    assert service.answer_window(cid) is not None
    assert service.speaker.renderer.calls[before:] == ['It is ready.', 'The detail is in chat.']


def test_final_and_expects_answer_refused_renders_nothing(service):
    cid = opened(service)
    count = len(service.speaker.renderer.calls)
    result = line(service, cid, final=True, expects_answer=True)
    assert result['outcome'] == 'refused' and result['reason'] == 'final_and_expects_answer'
    assert len(service.speaker.renderer.calls) == count
    assert service.answer_window(cid) is None


def _three_lines_then_question(service):
    cid = opened(service)
    for _ in range(3):
        assert line(service, cid)['outcome'] == 'spoken'
        service.test_clock[0] += 4
    question = line(service, cid, expects_answer=True)
    assert question['outcome'] == 'spoken'
    return cid, question['line_id']


def test_delivered_answer_resets_allowance(service):
    cid, line_id = _three_lines_then_question(service)
    # Fourth line reached the four-line limit; its window defers closure.
    assert service.answer_window(cid)['line_id'] == line_id
    assert service.answer_delivered(cid, line_id) is True
    service.test_clock[0] += 4
    # A line beyond the previous four-line limit is accepted after the delivered answer.
    assert line(service, cid)['outcome'] == 'spoken'


def test_no_answer_after_limit_refuses_next_line(service):
    cid, line_id = _three_lines_then_question(service)
    # No answer: let the window lapse past its length. Nothing is delivered.
    service.test_clock[0] += 25
    assert service.answer_window(cid) is None
    assert line(service, cid)['reason'] == 'exhausted_conversation'


def test_window_lapses_quietly_and_conversation_stays_open_below_limit(service):
    cid = opened(service)
    question = line(service, cid, expects_answer=True)
    assert question['outcome'] == 'spoken'
    count = len(service.speaker.renderer.calls)
    service.test_clock[0] += 25
    # The window closes with nothing delivered and nothing played.
    assert service.answer_window(cid) is None
    assert len(service.speaker.renderer.calls) == count
    # Below the allowance limit the conversation stays open for a wake-word turn.
    service.test_clock[0] += 4
    assert line(service, cid)['outcome'] == 'spoken'


def test_suppressed_question_opens_no_window(service):
    cid = opened(service)
    service.set_silent(True, 'web')
    count = len(service.speaker.renderer.calls)
    result = line(service, cid, expects_answer=True)
    assert result['outcome'] == 'suppressed' and result['reason'] == 'silent_mode'
    assert 'line_id' in result
    assert service.answer_window(cid) is None
    assert service.status()['answer_window'] == dict(waiting=False)
    assert len(service.speaker.renderer.calls) == count


def test_fourth_question_refused_until_rule_raised(service, tmp_path, monkeypatch):
    rules_file(tmp_path, monkeypatch, service, questions_per_conversation=3, lines_per_conversation=100)
    cid = opened(service)
    for i in range(3):
        assert line(service, cid, expects_answer=True)['outcome'] == 'spoken'
        service.test_clock[0] += 4
    refused = line(service, cid, expects_answer=True)
    assert refused['outcome'] == 'refused' and refused['reason'] == 'questions_per_conversation'
    # Raise the ceiling with no code change; the fourth question is then accepted.
    path = tmp_path / 'rules.json'
    policy = copy.deepcopy(DEFAULTS)
    policy['replies'].update(questions_per_conversation=5, lines_per_conversation=100)
    path.write_text(json.dumps(policy))
    assert service.reload()
    assert line(service, cid, expects_answer=True)['outcome'] == 'spoken'


def test_one_window_at_a_time_reported(service):
    cid = opened(service)
    assert service.has_open_window() is False
    line(service, cid, expects_answer=True)
    assert service.has_open_window() is True
    service.answer_delivered(cid, service.conversations[cid]['last_line_id'])
    assert service.has_open_window() is False
