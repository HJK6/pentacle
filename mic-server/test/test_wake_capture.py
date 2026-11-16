from wake_capture import WakeCaptures


def test_capacity_claim_once_and_snapshot_omits_text():
    queue = WakeCaptures(enabled=True)
    for n in range(8):
        assert queue.complete(f'synthetic message {n}', capture_id=str(n))
    assert not queue.can_start()
    assert not queue.complete('overflow')
    assert queue.claim()['id'] == '0'
    assert queue.can_start()
    assert 'text' not in queue.snapshot()['last_claim']
    assert queue.review()['text'] == 'synthetic message 0'
    assert queue.claim()['id'] == '1'


def test_action_version_does_not_discard_pending_item():
    queue = WakeCaptures()
    assert queue.complete('synthetic action', action={'version': 2}, capture_id='action')
    assert queue.claim(actions_version=1) is None
    assert queue.snapshot()['pending_count'] == 1
    assert queue.claim(actions_version=2)['id'] == 'action'
    queue.note_action_outcome('action', 'ok', {'synthetic': True})
    assert queue.review()['action_outcome'] == 'ok'
    old_generation = queue.generation
    queue.complete('pending')
    queue.invalidate()
    assert queue.generation != old_generation
    assert queue.claim() is None
