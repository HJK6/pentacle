import mic_server


def base_payload(**overrides):
    payload = {
        "selected_device": "Example USB Microphone",
        "preferred_device_name": "Example USB Microphone",
        "preferred_present": True,
        "disallowed_device": False,
        "stream_open": True,
        "stream_status": None,
    }
    payload.update(overrides)
    return payload


def test_health_classifier_ok():
    health = mic_server.AudioHealthState()
    health.update(base_payload(), now=100.0)
    audio = health.update({"peak": 0.2, "rms": 0.05, "stream_status": None}, now=100.1)

    assert audio["health_state"] == "ok"
    assert audio["callback_age_ms"] == 0
    assert audio["flatline_seconds"] == 0.0


def test_health_classifier_muted_or_tcc_silence():
    health = mic_server.AudioHealthState()
    health.update(base_payload(), now=100.0)
    health.update({"peak": 0.0, "rms": 0.0}, now=100.0)
    audio = health.snapshot(now=102.0)

    assert audio["health_state"] == "muted_or_tcc_silence"
    assert audio["flatline_seconds"] >= 2.0


def test_health_classifier_preferred_missing():
    health = mic_server.AudioHealthState()
    audio = health.update(base_payload(
        selected_device=None,
        preferred_present=False,
        stream_open=False,
    ), now=100.0)

    assert audio["health_state"] == "preferred_missing"


def test_health_classifier_wrong_device():
    health = mic_server.AudioHealthState()
    audio = health.update(base_payload(
        selected_device="Synthetic Loopback Microphone",
        preferred_device_name=None,
        preferred_present=False,
        disallowed_device=True,
    ), now=100.0)

    assert audio["health_state"] == "wrong_device"


def test_health_classifier_stream_errors():
    health = mic_server.AudioHealthState()
    audio = health.update(base_payload(stream_status="input overflow"), now=100.0)

    assert audio["health_state"] == "stream_error"


def test_health_classifier_stale_callback():
    health = mic_server.AudioHealthState()
    health.update(base_payload(), now=100.0)
    health.update({"peak": 0.2, "rms": 0.05}, now=100.0)
    audio = health.snapshot(now=104.0)

    assert audio["health_state"] == "stream_error"
    assert audio["callback_age_ms"] >= 4000


def test_audio_last_error_requires_sustained_degraded_and_clears_on_ok():
    health = mic_server.AudioHealthState()
    health.update(base_payload(preferred_present=False, stream_open=False), now=100.0)

    assert health.last_error_payload(now=109.9) is None
    error = health.last_error_payload(now=110.0)
    assert error["health_state"] == "preferred_missing"

    health.update(base_payload(), now=111.0)
    health.update({"peak": 0.2, "rms": 0.05}, now=111.0)
    assert health.last_error_payload(now=111.0) is None
