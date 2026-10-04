"""Evaluation-set rule: an expects_answer line must be a single direct question."""
import pytest
from tools.voice_line import check_expects_answer, check_line
from tools import voice_eval


def test_check_expects_answer_accepts_single_direct_question():
    assert check_expects_answer('Should I proceed with the deploy?') is None
    assert check_expects_answer('Ready?') is None


def test_check_expects_answer_rejects_statement_and_multi_question():
    assert check_expects_answer('I will proceed with the deploy.') == 'not_single_question'
    assert check_expects_answer('Proceeding now.') == 'not_single_question'
    # A multi-question line is caught by the ordinary checker first.
    assert check_expects_answer('Should I deploy? Or wait?') == 'questions'
    # A question mark that is not the end of a direct question is rejected.
    assert check_expects_answer('Is it ready, and the detail is in chat.') == 'not_single_question'


def _run(events, monkeypatch):
    monkeypatch.setattr(voice_eval, 'submit_line', lambda *a, **k: {'outcome': 'spoken'})
    cases = [{'id': 'a', 'conversation_id': 'c', 'category': 'short'}]
    transcripts = [{'id': 'a', 'chat': 'reply-in-chat', 'events': events}]
    return voice_eval.evaluate(cases, transcripts, endpoint='http://127.0.0.1:7780')


def test_eval_passes_when_every_question_is_single(monkeypatch):
    result = _run([{'type': 'speech', 'text': 'Should I proceed?', 'final': False, 'expects_answer': True}], monkeypatch)
    assert result['expects_answer_ok'] is True and result['expects_answer_failures'] == []


def test_eval_fails_on_a_statement_flagged_expects_answer(monkeypatch):
    result = _run([{'type': 'speech', 'text': 'I am proceeding now.', 'final': False, 'expects_answer': True}], monkeypatch)
    assert result['expects_answer_ok'] is False
    assert result['expects_answer_failures'][0]['reason'] == 'not_single_question'


def test_eval_fails_on_a_multi_question_flagged_expects_answer(monkeypatch):
    result = _run([{'type': 'speech', 'text': 'Should I deploy? Or wait?', 'final': False, 'expects_answer': True}], monkeypatch)
    assert result['expects_answer_ok'] is False
    assert result['expects_answer_failures'][0]['reason'] == 'questions'
