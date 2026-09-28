"""Approved spawn-only dialogue contract: grounded fields and bounded replies."""
import pytest
from local_actions import validate_decision


@pytest.fixture(autouse=True)
def configured_synthetic_hosts(monkeypatch):
    import local_actions
    monkeypatch.setattr(local_actions, 'HOSTS', {'samplehost', 'otherhost', 'thirdhost'})
    monkeypatch.setenv('MIC_LOCAL_ACTIONS_DEFAULT_HOST', 'samplehost')


def proposal(**values):
    return dict(route='spawn_agent', task='', model='', effort='', host='', assets=[], reply='', **{}) | values


def test_missing_model_keeps_task_and_asks():
    result = validate_decision(proposal(task='Review tests.'), 'Spawn an agent to Review tests.')
    assert result['route'] == 'clarify'
    assert result['spawn']['fields']['task'] == 'Review tests.'
    assert result['spawn']['missing'] == ['model']


def test_only_model_is_required():
    result = validate_decision(proposal(route='clarify', reply='What task?'), 'Spawn me an agent')
    assert result['route'] == 'clarify'
    assert set(result['spawn']['missing']) == {'model'}
    assert 'model' in result['reply'].lower() and 'task' not in result['reply'].lower()


def test_named_model_uses_guidance_default():
    result = validate_decision(proposal(task='Review tests.', model='astra'), 'Spawn an Astra agent to Review tests.')
    assert result['route'] == 'spawn_agent'
    assert (result['model'], result['effort'], result['host']) == ('gpt-6-astra', 'high', 'samplehost')


def test_invalid_option_keeps_task_for_repair():
    result = validate_decision(proposal(task='Review tests.', model='unicorn'), 'Spawn a unicorn agent to Review tests.')
    assert result['route'] == 'clarify' and result['spawn']['missing'] == ['model']
    assert result['spawn']['fields']['task'] == 'Review tests.'


def test_direct_answer_requires_open_window_and_over(monkeypatch):
    from .test_wake_voice import wake_listener, hear
    monkeypatch.setenv('MIC_LOCAL_ACTIONS', 'true')
    monkeypatch.setenv('MIC_LOCAL_ACTIONS_WAKE', 'shared')
    _, listener = wake_listener(monkeypatch)
    listener.running = True
    actions = listener.voice_actions
    actions.classifier = lambda text: validate_decision(proposal(), text)
    actions.speaker = lambda *args: {'stopped': True}
    clock = [100.]
    monkeypatch.setattr('time.monotonic', lambda: clock[0])
    hear(listener, 'Hey Bart spawn an agent')
    hear(listener, 'over')
    item = actions.queue.get_nowait()
    actions._process(item)
    clock[0] += 2
    assert actions.snapshot()['pending']['state'] == 'waiting'
    hear(listener, 'Astra')
    assert listener.capture_origin == 'followup'
    assert actions.queue.empty()
    hear(listener, 'over')
    answer = actions.queue.get_nowait()
    assert answer['id'] == item['id'] and answer['text'] == 'Astra'


def answer_decision(text, spawn, **fields):
    import io, json
    from local_actions import classify_followup
    raw = proposal(**fields)
    def opener(*args, **kwargs):
        return io.BytesIO(json.dumps({'message': {'content': json.dumps(raw)}}).encode())
    return classify_followup(text, spawn, opener=opener)


@pytest.fixture
def dialogue(monkeypatch):
    from .test_wake_voice import wake_listener, hear
    monkeypatch.setenv('MIC_LOCAL_ACTIONS', 'true')
    monkeypatch.setenv('MIC_LOCAL_ACTIONS_WAKE', 'shared')
    _, listener = wake_listener(monkeypatch)
    listener.running = True
    clock = [100.]
    monkeypatch.setattr('time.monotonic', lambda: clock[0])
    actions = listener.voice_actions
    actions.speaker = lambda *args: {'stopped': True}
    def open_request(text='Spawn an agent', **fields):
        actions.classifier = lambda actual: validate_decision(proposal(**fields), actual)
        hear(listener, 'Hey Bart '+text); hear(listener, 'over')
        item = actions.queue.get_nowait(); actions._process(item)
        clock[0] += 2
        return item
    def answer(text, **fields):
        actions.followup_classifier = lambda actual, pending: answer_decision(actual, pending, **fields)
        hear(listener, text); hear(listener, 'over')
        item = actions.queue.get_nowait(); actions._process(item)
        return item
    return listener, actions, clock, open_request, answer, hear


def test_followup_spawns_once_with_original_id_exact_task_and_default_effort(dialogue):
    listener, actions, clock, start, answer, hear = dialogue
    original = start()
    task = "Review tests; print 'Done!'"
    answer('Astra to '+task, model='astra', task=task)
    claim = listener.wake.claim(actions_version=2)
    assert claim['id'] == original['id']
    assert claim['text'] == original['text']
    assert claim['action']['task'] == task
    assert claim['action']['model'] == 'gpt-6-astra' and claim['action']['effort'] == 'high'
    assert actions.snapshot()['pending'] is None
    hear(listener, task); hear(listener, 'over')
    assert listener.wake.claim(actions_version=2) is None and actions.queue.empty()


def test_two_answers_fill_only_missing_fields(dialogue):
    listener, actions, clock, start, answer, hear = dialogue
    start('Spawn an agent with ultra effort', effort='ultra')
    answer('Sol', model='sol')
    assert actions.pending['spawn']['missing'] == ['effort']
    assert actions.pending['answers'] == 1
    clock[0] += 2
    answer('High', effort='high')
    claim = listener.wake.claim(actions_version=2)
    assert claim['action']['model'] == 'gpt-6-sol' and claim['action']['effort'] == 'high'


def test_invalid_option_repair_preserves_task(dialogue):
    listener, actions, clock, start, answer, hear = dialogue
    start('Spawn a unicorn agent to Review tests.', model='unicorn', task='Review tests.')
    answer('Fable', model='fable')
    claim = listener.wake.claim(actions_version=2)
    assert claim['action']['task'] == 'Review tests.' and claim['action']['model'] == 'claude-fable-5-1'


def test_pending_invalid_effort_cannot_disappear_when_answer_only_supplies_model():
    initial = validate_decision(proposal(task='Review tests.', effort='ultra'), 'Spawn an agent with ultra effort to Review tests.')
    result = answer_decision('Sol', initial['spawn'], model='sol')
    assert result['spawn']['missing'] == ['effort']
    complete = answer_decision('High', result['spawn'], effort='high')
    assert complete['effort'] == 'high'


@pytest.mark.parametrize('operation', ['timeout', 'cancel', 'off', 'fresh_wake'])
def test_pending_partial_answer_cleared_on_boundaries(dialogue, operation):
    listener, actions, clock, start, answer, hear = dialogue
    start(); hear(listener, 'partial task')
    if operation == 'timeout':
        clock[0] += 61
        assert actions.snapshot()['pending'] is None
    elif operation == 'cancel': hear(listener, 'cancel')
    elif operation == 'off': listener.stop()
    else: hear(listener, 'Hey Bart what is Bitcoin worth?')
    assert actions.pending is None
    if operation == 'fresh_wake':
        assert listener.captured_texts == ['what is Bitcoin worth?']
    else:
        hear(listener, 'over')
        assert actions.queue.empty() and not listener.wake.pending


def test_answer_in_flight_does_not_admit_second_answer(dialogue):
    listener, actions, clock, start, answer, hear = dialogue
    start()
    hear(listener, 'Astra'); hear(listener, 'over')
    item = actions.queue.get_nowait()
    assert actions.pending['state'] == 'in_flight'
    hear(listener, 'Review something else.'); hear(listener, 'over')
    assert actions.queue.empty() and listener.state == 'LISTENING'
    actions.followup_classifier = lambda text, pending: answer_decision(text, pending, model='astra')
    actions._process(item)
    assert len(listener.wake.pending) == 1


def test_fresh_wake_invalidates_inference_without_changing_mic_generation(dialogue):
    listener, actions, clock, start, answer, hear = dialogue
    start(); generation = listener.wake.generation
    def classifier(text, pending):
        hear(listener, 'Hey Bart what is Bitcoin worth?')
        return answer_decision(text, pending, model='astra')
    actions.followup_classifier = classifier
    hear(listener, 'Astra'); hear(listener, 'over')
    actions._process(actions.queue.get_nowait())
    assert not listener.wake.pending and listener.wake.generation == generation
    assert listener.captured_texts == ['what is Bitcoin worth?']


def test_deadline_applies_to_answer_admission_not_inference(dialogue):
    listener, actions, clock, start, answer, hear = dialogue
    start()
    hear(listener, 'Astra'); hear(listener, 'over')
    item = actions.queue.get_nowait(); clock[0] += 80
    actions.followup_classifier = lambda text, pending: answer_decision(text, pending, model='astra')
    actions._process(item)
    assert len(listener.wake.pending) == 1


def test_unrelated_is_silent_counts_and_never_refreshes_timer(dialogue):
    listener, actions, clock, start, answer, hear = dialogue
    start(); deadline = actions.pending['deadline']
    actions.speaker = lambda *args: pytest.fail('Unrelated room speech must stay silent')
    answer('The pizza is here.', route='bart')
    assert actions.pending['deadline'] == deadline and actions.pending['answers'] == 1
    answer('The pizza is still here.', route='bart')
    assert actions.pending is None and not listener.wake.pending


def test_failure_and_deferred_speech_never_open_window(dialogue):
    listener, actions, clock, start, answer, hear = dialogue
    def fail(*args): raise TimeoutError('lost receipt')
    actions.speaker = fail
    with pytest.raises(TimeoutError): start()
    assert actions.pending is None


def test_literal_grounding_rejects_rewritten_task_or_new_model(dialogue):
    listener, actions, clock, start, answer, hear = dialogue
    start()
    with pytest.raises(ValueError): answer_decision('Review tests.', actions.pending['spawn'], task='Delete files.')
    result = answer_decision('Review tests.', actions.pending['spawn'], task='Review tests.', model='sol')
    assert result['route'] == 'clarify' and result['spawn']['missing'] == ['model']


def test_invalid_answers_never_extend_deadline_and_cap_does_not_rearm(dialogue):
    listener, actions, clock, start, answer, hear = dialogue
    start('Spawn an agent to Review tests.', task='Review tests.')
    deadline = actions.pending['deadline']
    answer('Unicorn', model='unicorn')
    assert actions.pending['deadline'] == deadline and actions.pending['answers'] == 1
    clock[0] += 2
    answer('Unicorn', model='unicorn')
    assert actions.pending is None and not listener.wake.pending
    assert actions.last['response'] == 'Please start again with the full request.'


def test_model_and_task_in_one_answer_grounded_from_same_utterance(dialogue):
    listener, actions, clock, start, answer, hear = dialogue
    start('Spawn an agent')
    answer('Use Sol to Review tests.', model='sol', task='Review tests.')
    claim = listener.wake.claim(actions_version=2)
    assert claim['action']['model'] == 'gpt-6-sol'
    assert actions.last['field_sources']['task']['text'] == 'Use Sol to Review tests.'


def test_late_asr_over_cannot_admit_expired_answer(dialogue):
    import numpy as np
    listener, actions, clock, start, answer, hear = dialogue
    start(); hear(listener, 'Review tests.')
    def transcribe(audio):
        clock[0] += 61
        return 'over'
    listener._transcribe = transcribe
    listener._handle_utterance(np.ones(8000))
    assert actions.pending is None and actions.queue.empty() and not listener.wake.pending


def test_busy_question_never_opens_pending_window(dialogue, monkeypatch):
    listener, actions, clock, start, answer, hear = dialogue
    monkeypatch.setattr('time.sleep', lambda seconds: clock.__setitem__(0, clock[0]+seconds))
    decision = validate_decision(proposal(), 'Spawn an agent')
    listener.state = 'CAPTURING'; listener.capture_origin = 'manual'
    item = dict(id='busy',generation=listener.wake.generation,revision=actions.revision,text='Spawn an Astra agent')
    actions._ask(item, decision)
    assert actions.pending is None and actions.last['state'] == 'error'


def test_off_during_question_playback_never_commits_pending(dialogue):
    listener, actions, clock, start, answer, hear = dialogue
    actions.speaker = lambda *args: listener.stop()
    start()
    assert actions.pending is None


def test_manual_copy_cannot_take_over_answer_capture(dialogue):
    listener, actions, clock, start, answer, hear = dialogue
    start(); hear(listener, 'Review tests.')
    with pytest.raises(ValueError): listener._execute_command('start_copy')
    assert listener.capture_origin == 'followup'


@pytest.mark.parametrize('seconds', ['0', '181', 'nan', 'infinity', 'oops'])
def test_invalid_window_configuration_fails_startup(monkeypatch, seconds):
    from .test_local_actions import fake_actions
    monkeypatch.setenv('MIC_LOCAL_FOLLOWUP_SECONDS', seconds)
    with pytest.raises(ValueError): fake_actions(monkeypatch)


def test_old_typed_client_cannot_consume_new_followup_contract(dialogue):
    listener, actions, clock, start, answer, hear = dialogue
    start(); answer('Astra', model='astra')
    assert listener.wake.claim(actions_version=1) is None
    assert len(listener.wake.pending) == 1
    assert listener.wake.claim(actions_version=2)['action']['version'] == 2


@pytest.mark.parametrize('model,effort', [('astra','high'),('sol','high'),('terra','xhigh'),('luna','max'),('fable','high'),('opus','high'),('sonnet','high')])
def test_named_model_alone_is_sufficient_with_guidance_defaults(model, effort):
    result = validate_decision(proposal(model=model), f'Spawn a {model} agent')
    assert result['route'] == 'spawn_agent' and result['task'] == ''
    assert result['host'] == 'samplehost' and result['effort'] == effort
    assert result['effort_source'] == 'model_guidance_default'


def test_explicit_effort_overrides_guidance_default():
    result = validate_decision(proposal(model='astra',effort='medium'), 'Spawn an Astra agent with medium effort')
    assert result['effort'] == 'medium' and result['effort_source'] == 'explicit'


def test_missing_model_reply_alone_opens_idle_agent(dialogue):
    listener, actions, clock, start, answer, hear = dialogue
    start()
    assert actions.pending['spawn']['missing'] == ['model']
    answer('Astra', model='astra')
    claim=listener.wake.claim(actions_version=2)
    assert claim['action']['task'] == '' and claim['action']['model'] == 'gpt-6-astra'
    assert claim['action']['effort'] == 'high' and actions.pending is None


def test_fresh_wake_preserves_earlier_independent_request(dialogue):
    listener, actions, clock, start, answer, hear = dialogue
    actions.classifier = lambda text: {'route':'bart','text':text}
    hear(listener, 'Hey Bart first message'); hear(listener, 'over')
    first = actions.queue.get_nowait()
    hear(listener, 'Hey Bart second message'); hear(listener, 'over')
    second = actions.queue.get_nowait()
    actions._process(first); actions._process(second)
    assert [listener.wake.claim()['text'], listener.wake.claim()['text']] == ['first message', 'second message']


@pytest.mark.parametrize('returned', [False, True])
def test_followup_explicit_host_and_effort_override_only_defaults(returned):
    pending = validate_decision(proposal(), 'Spawn an agent')['spawn']
    result = answer_decision('Astra on Otherhost with medium effort', pending, model='astra',
                            host='otherhost' if returned else '', effort='medium' if returned else '')
    assert result['host'] == 'otherhost' and result['effort'] == 'medium'
    assert result['effort_source'] == 'explicit'


def test_followup_cannot_silently_replace_explicit_host():
    pending = validate_decision(proposal(host='samplehost'), 'Spawn an agent on Samplehost')['spawn']
    with pytest.raises(ValueError): answer_decision('Astra on Otherhost', pending, model='astra')


def test_recognition_stamp_never_acquires_dialogue_lock(dialogue):
    from unittest.mock import Mock
    listener, actions, clock, start, answer, hear = dialogue
    actions.expire = Mock(side_effect=AssertionError('Callback must not expire dialogue'))
    listener.recognition_stamp()
    actions.expire.assert_not_called()


def test_expired_nonprogress_question_is_not_spoken(dialogue):
    listener, actions, clock, start, answer, hear = dialogue
    start(); hear(listener, 'Unicorn'); hear(listener, 'over')
    item = actions.queue.get_nowait(); clock[0] += 70
    actions.speaker = lambda *args: pytest.fail('Expired question must not be spoken')
    actions.followup_classifier = lambda text,pending: answer_decision(text,pending,model='unicorn')
    actions._process(item)
    assert actions.pending is None and actions.last['state'] == 'expired'


def test_fresh_wake_cancels_question_being_prepared(dialogue):
    listener, actions, clock, start, answer, hear = dialogue
    def say(item, text):
        hear(listener, 'Hey Bart new message')
        return True
    actions._say = say
    start()
    assert actions.pending is None and listener.captured_texts == ['new message']


def test_model_echo_of_unspoken_default_is_not_an_explicit_option():
    pending=validate_decision(proposal(), 'Spawn an agent')['spawn']
    result=answer_decision('Astra', pending, model='astra',host='samplehost',effort='high')
    assert result['host']=='samplehost' and result['effort']=='high'
    assert result['effort_source']=='model_guidance_default'
    assert 'host' not in result['sources'] and 'effort' not in result['sources']


def test_unspoken_model_cannot_supply_required_model():
    result=validate_decision(proposal(model='astra'), 'Spawn an agent')
    assert result['route']=='clarify' and result['spawn']['missing']==['model']


def test_spoken_options_win_over_model_echo_defaults():
    result=validate_decision(proposal(task='review tests',model='astra',host='samplehost',effort='high'),
                             'Start a Sol agent on Otherhost with medium effort to review tests')
    assert (result['model'],result['host'],result['effort'])==('gpt-6-sol','otherhost','medium')


@pytest.mark.parametrize('text', ['Use an Astra high.', 'Spawn me an Astra high. Spawn and Astra High.'])
def test_option_echo_in_task_does_not_discard_valid_answer(dialogue, text):
    listener, actions, clock, start, answer, hear = dialogue
    original = start()
    answer(text, model='astra', effort='high', task='high')
    claim = listener.wake.claim(actions_version=2)
    assert claim and claim['id'] == original['id']
    assert claim['action']['model'] == 'gpt-6-astra'
    assert claim['action']['effort'] == 'high'
    assert claim['action']['task'] == ''
    assert claim['action']['effort_source'] == 'explicit'


def test_answer_ready_precedes_first_speech_but_not_echo_tail(dialogue):
    listener, actions, clock, start, answer, hear = dialogue
    start()
    clock[0] -= 2
    assert actions.snapshot()['pending']['ready'] is False
    clock[0] += 1.01
    assert listener.state == 'LISTENING'
    assert actions.snapshot()['pending']['ready'] is True
    clock[0] += 61
    assert actions.snapshot()['pending'] is None


@pytest.mark.parametrize('text', ['Astra high to high', 'Astra high for high', 'Astra high task high'])
def test_option_echo_does_not_erase_introduced_task(dialogue, text):
    listener, actions, clock, start, answer, hear = dialogue
    start()
    answer(text, model='astra', effort='high', task='high')
    assert listener.wake.claim(actions_version=2) is None
    assert actions.snapshot()['pending']['state'] == 'waiting'
    assert actions.snapshot()['last']['validation_error']


def test_literal_task_containing_effort_word_is_preserved(dialogue):
    listener, actions, clock, start, answer, hear = dialogue
    start()
    answer('Use Astra high to Review high priority tests.', model='astra', effort='high', task='Review high priority tests.')
    claim = listener.wake.claim(actions_version=2)
    assert claim['action']['task'] == 'Review high priority tests.'


def test_followup_answer_decoder_does_not_receive_wake_hint(monkeypatch):
    import numpy as np
    from types import SimpleNamespace
    from .test_wake_voice import wake_listener
    _, listener = wake_listener(monkeypatch)
    listener.voice_actions.pending = {'state':'waiting'}
    calls=[]
    def transcribe(path, **kwargs):
        calls.append(kwargs)
        return [SimpleNamespace(text='Astra high')], None
    listener.whisper_model=SimpleNamespace(transcribe=transcribe)
    assert listener._transcribe(np.ones(8000)) == 'Astra high'
    assert 'hotwords' not in calls[0]
    assert calls[0]['beam_size'] == 1
