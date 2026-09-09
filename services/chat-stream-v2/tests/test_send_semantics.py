"""Contract coverage for daemon-v2 send semantics."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from comms import Comms
from sessions import Sessions, VerbError
from spawnctl import SpawnCtl
from store import Store


HOST = "localhost"
REMOTE = "hostb"
NAME = "send-target"
PNG = b"\x89PNG\r\n\x1a\nfixture-png"
SHA = hashlib.sha256(PNG).hexdigest()


def _submitted(text: str) -> str:
    return f"\u23fa {text}\n\u276f \n"


class FakeTmux:
    def __init__(
        self,
        *,
        submit_on_paste: bool = True,
        submit_on_retry: bool = False,
        fail_phase: str | None = None,
        fail_enter: bool = False,
    ) -> None:
        self.submit_on_paste = submit_on_paste
        self.submit_on_retry = submit_on_retry
        self.fail_phase = fail_phase
        self.fail_enter = fail_enter
        self.paste_error = None
        self.screen = "\u276f \n"
        self.pastes: list[str] = []
        self.enters = 0
        self.escapes = 0
        self.staged: list[tuple[str, bytes]] = []

    async def capture(self, _name: str) -> str:
        return self.screen

    async def paste(self, _name: str, text: str) -> None:
        if self.paste_error is not None:
            raise self.paste_error
        self.pastes.append(text)
        if self.fail_phase:
            raise VerbError("paste_failed", "simulated paste failure", phase=self.fail_phase)
        if self.submit_on_paste:
            self.screen = f"{self.screen.rstrip()}\n{_submitted(text)}"

    async def send_enter(self, _name: str) -> None:
        self.enters += 1
        if self.fail_enter:
            raise VerbError("paste_failed", "simulated Enter failure", phase="enter_failed")
        if self.submit_on_retry and self.pastes:
            self.screen = _submitted(self.pastes[-1])

    async def run(self, *args: str, **_kwargs) -> tuple[int, str]:
        if args[:1] == ("send-keys",) and "Escape" in args:
            self.escapes += 1
        return 0, ""

    async def stage_text(self, path: str, data: bytes) -> None:
        self.staged.append((path, data))


class FakeBlobStore:
    def __init__(self, blobs: dict[str, bytes] | None = None, error: Exception | None = None) -> None:
        self.blobs = blobs or {SHA: PNG}
        self.error = error
        self.reads: list[str] = []

    async def read_verified(self, sha: str, *, max_bytes: int) -> bytes:
        self.reads.append(sha)
        if self.error is not None:
            raise self.error
        data = self.blobs[sha]
        if len(data) > max_bytes:
            raise ValueError("blob exceeds attachment limit")
        if hashlib.sha256(data).hexdigest() != sha:
            raise ValueError("blob digest mismatch")
        return data


class FakeHosts:
    local_host = HOST

    def __init__(self, tmux: FakeTmux, *, remote_tmux: FakeTmux | None = None, fail_stage: bool = False) -> None:
        self.local_tmux = tmux
        self.remote_tmux = remote_tmux or tmux
        self.fail_stage = fail_stage

    def is_local(self, host: str) -> bool:
        return host == HOST

    def tmux_for(self, host: str) -> FakeTmux:
        return self.local_tmux if host == HOST else self.remote_tmux

    async def ensure_reachable(self, _host: str, _what: str) -> None:
        return None

    async def run_command(self, _host: str, *_args: str, **_kwargs) -> tuple[int, str]:
        if self.fail_stage:
            return 1, "staging unavailable"
        return 0, "/tmp/public-fixture/.cache/pentacle-stream/attachments\n"


def _new_comms(
    tmux: FakeTmux,
    tmp_path: Path,
    *,
    blob_store: FakeBlobStore | None = None,
    hosts: FakeHosts | None = None,
) -> tuple[Comms, Store, Sessions]:
    store = Store(":memory:")
    store.start()
    sessions = Sessions(store, tmux=tmux, local_host=HOST)
    comms = Comms(
        store,
        sessions,
        SpawnCtl(store, sessions, tmux=tmux),
        hosts=hosts,
        blob_store=blob_store or FakeBlobStore(),
        attachment_root=tmp_path / "attachments",
    )
    return comms, store, sessions


async def _open(comms: Comms, sessions: Sessions, host: str = HOST) -> None:
    await sessions.open(host, NAME, provider="claude")


def _run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("bootstrap_state", ["queued", "starting", "failed", "ready", None, "started"])
def test_claude_first_send_requires_bootstrap_ready(tmp_path: Path, bootstrap_state: str | None) -> None:
    """Admission is addressability, not permission to paste into a booting CLI."""
    async def go() -> None:
        tmux = FakeTmux()
        comms, store, sessions = _new_comms(tmux, tmp_path)
        try:
            await sessions.open(HOST, NAME, provider="claude", bootstrap_state=bootstrap_state)
            blocked = bootstrap_state in {"queued", "starting", "failed"}
            try:
                result = await comms.send({
                    "stream_id": f"{HOST}:{NAME}", "text": "synthetic first turn",
                    "request_id": "send-bootstrap-discriminator", "optimistic_id": "opt-bootstrap",
                })
            except VerbError as exc:
                assert blocked
                assert exc.code == "bootstrap_not_ready"
                assert exc.extra["phase"] == "not_started"
                assert exc.extra["action_committed"] is False
            else:
                assert not blocked
                assert result["submission_confirmed"] is True
            assert len(tmux.pastes) == (0 if blocked else 1)
            assert tmux.enters == 0
            if blocked:
                receipt = await store.get_send_receipt(f"{HOST}:{NAME}", "send-bootstrap-discriminator")
                assert receipt["delivery"] == "not_landed"
                assert receipt["attempts"] == 0
                assert receipt["reason"] == "bootstrap_not_ready"
        finally:
            store.stop()

    _run(go())


def test_claude_starting_blocks_before_attachment_materialization(tmp_path: Path) -> None:
    async def go() -> None:
        tmux, blobs = FakeTmux(), FakeBlobStore()
        comms, store, sessions = _new_comms(tmux, tmp_path, blob_store=blobs)
        try:
            await sessions.open(HOST, NAME, provider="claude", bootstrap_state="starting",
                                initial_prompt_delivery="not_requested")
            with pytest.raises(VerbError, match="not ready"):
                await comms.send({"stream_id": f"{HOST}:{NAME}", "attachments": [
                    {"key": SHA, "mime": "image/png"}], "request_id": "send-starting-image"})
            assert blobs.reads == []
            assert tmux.pastes == [] and tmux.staged == [] and tmux.enters == 0
            assert not (tmp_path / "attachments").exists()
        finally:
            store.stop()
    _run(go())


def test_claude_readiness_rechecked_after_materialization(tmp_path: Path) -> None:
    async def go() -> None:
        tmux = FakeTmux()
        comms, store, sessions = _new_comms(tmux, tmp_path)
        materialize = comms._materialize_send_plan
        async def change_state(plan):
            result = await materialize(plan)
            await store.update_session(HOST, NAME, bootstrap_state="starting")
            return result
        comms._materialize_send_plan = change_state
        try:
            await sessions.open(HOST, NAME, provider="claude", bootstrap_state="ready")
            result = await comms.send({"stream_id": f"{HOST}:{NAME}", "text": "race",
                                       "request_id": "send-ready-race"})
            assert result["delivery"] == "not_landed"
            assert result["reason"] == "bootstrap_not_ready"
            assert tmux.pastes == [] and tmux.enters == 0
            comms._materialize_send_plan = materialize
            await store.update_session(HOST, NAME, bootstrap_state="ready")
            result = await comms.send({"stream_id": f"{HOST}:{NAME}", "text": "ready now",
                                       "request_id": "send-ready-transition"})
            assert result["submission_confirmed"] is True
            assert tmux.pastes == ["ready now"] and tmux.enters == 0
        finally:
            store.stop()
    _run(go())


@pytest.mark.parametrize("cancel_send", [False, True])
@pytest.mark.parametrize("writer", ["publish", "adopt_delivered", "adopt_indeterminate"])
def test_claude_bootstrap_publication_serializes_with_input(tmp_path: Path, cancel_send: bool, writer: str) -> None:
    """The real bootstrap publisher cannot invalidate an admitted in-flight input."""
    async def go() -> None:
        captured, release_capture = asyncio.Event(), asyncio.Event()
        order: list[str] = []
        class PausedTmux(FakeTmux):
            async def capture(self, name: str) -> str:
                if not captured.is_set():
                    captured.set()
                    await release_capture.wait()
                return await super().capture(name)
            async def paste(self, name: str, text: str) -> None:
                row = await store.fetch_session(HOST, NAME)
                assert row["bootstrap_state"] == "ready"
                order.append("paste")
                await super().paste(name, text)
        tmux = PausedTmux()
        comms, store, sessions = _new_comms(tmux, tmp_path)
        send_task = publish_task = acquire_waiter = None
        try:
            # The public contract cancel fence makes _adopt_interrupted_spawn fail closed
            # without a matching nonce-bearing reservation for its request_id.
            # Seed it (adopt variants only) and open the row under the SAME fence
            # so open() accepts the reserved name; the publish variant reserves
            # nothing and its send/lock timing is untouched.
            adopt_fence = "synthetic-adoption" if writer != "publish" else None
            if adopt_fence:
                await store.reserve_stream_id(
                    HOST, NAME, ttl_s=600, request_id=adopt_fence, nonce="synthetic-nonce",
                )
            await sessions.open(
                HOST, NAME, provider="claude", bootstrap_state="ready", fence=adopt_fence,
            )
            send_task = asyncio.create_task(comms.send({
                "stream_id": f"{HOST}:{NAME}", "text": "serialized input",
                "request_id": "send-publish-race",
            }))
            await asyncio.wait_for(captured.wait(), 2)
            async def publish() -> None:
                if writer == "publish":
                    await comms.spawnctl._publish_spawn_state(HOST, NAME, "starting")
                else:
                    async def no_reset(*_args, **_kwargs):
                        return False
                    async def settled(*_args, **_kwargs):
                        return ("delivered" if writer == "adopt_delivered" else "indeterminate",
                                "synthetic_evidence", "synthetic_reason")
                    comms.spawnctl._reset_blocked_spawn_recorded = no_reset
                    comms.spawnctl._settle_adoption = settled
                    await comms.spawnctl._adopt_interrupted_spawn(
                        HOST, NAME, "synthetic-adoption", {"open_fields": {"objective": "Exercise adoption and input serialization"}, "brief": "initial brief"}, tmux,
                    )
                order.append("publication")
            lock = sessions._lifecycle_lock(HOST, NAME)
            acquire_attempted = asyncio.Event()
            acquire = lock.acquire
            async def observe_acquire():
                acquire_attempted.set()
                return await acquire()
            lock.acquire = observe_acquire
            publish_task = asyncio.create_task(publish())
            # This is the existing authority lock, not a second input-only lock.
            assert lock.locked()
            acquire_waiter = asyncio.create_task(acquire_attempted.wait())
            await asyncio.wait({acquire_waiter, publish_task}, timeout=2,
                               return_when=asyncio.FIRST_COMPLETED)
            assert acquire_attempted.is_set(), "writer bypassed the lifecycle mutex"
            assert not publish_task.done()
            if cancel_send:
                send_task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await send_task
            else:
                release_capture.set()
                result = await asyncio.wait_for(send_task, 2)
                assert result["submission_confirmed"] is True
            await asyncio.wait_for(publish_task, 2)
            assert order == (["publication"] if cancel_send else ["paste", "publication"])
            assert (await store.fetch_session(HOST, NAME))["bootstrap_state"] == (
                "started" if writer == "adopt_delivered" else "starting")
            assert not sessions._lifecycle_lock(HOST, NAME).locked()
        finally:
            release_capture.set()
            for task in (send_task, publish_task, acquire_waiter):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(task for task in (send_task, publish_task, acquire_waiter) if task), return_exceptions=True)
            store.stop()
    _run(go())


def test_claude_published_starting_wins_before_send_and_emits_unlocked(tmp_path: Path) -> None:
    async def go() -> None:
        tmux = FakeTmux()
        comms, store, sessions = _new_comms(tmux, tmp_path)
        emitted: list[bool] = []
        class Emitter:
            async def emit_if_changed(self, *, immediate: bool) -> None:
                assert not sessions._lifecycle_lock(HOST, NAME).locked()
                emitted.append(immediate)
        try:
            await sessions.open(HOST, NAME, provider="claude", bootstrap_state="ready")
            sessions._inventory_emitter = Emitter()
            await comms.spawnctl._publish_spawn_state(HOST, NAME, "starting")
            with pytest.raises(VerbError, match="not ready"):
                await comms.send({"stream_id": f"{HOST}:{NAME}", "text": "not admitted"})
            assert emitted == [True]
            assert tmux.pastes == [] and tmux.enters == 0
        finally:
            store.stop()
    _run(go())


def test_photo_only_accept_materialize_echo(tmp_path: Path) -> None:
    async def go() -> tuple[dict, FakeTmux, FakeBlobStore, Store]:
        tmux = FakeTmux()
        blobs = FakeBlobStore()
        stale = tmp_path / "attachments" / f"{SHA}.png"
        stale.parent.mkdir()
        stale.write_bytes(b"stale materialization")
        comms, store, sessions = _new_comms(tmux, tmp_path, blob_store=blobs)
        await _open(comms, sessions)
        result = await comms.send({
            "stream_id": f"{HOST}:{NAME}", "attachments": [{"key": SHA, "mime": "image/png"}],
            "optimistic_id": "opt-photo", "msg_id": "m-photo", "request_id": "send-photo-only",
        })
        return result, tmux, blobs, store

    result, tmux, blobs, store = _run(go())
    try:
        assert result["delivery"] == "landed"
        assert result["submission_confirmed"] is True
        assert result["attempt"] == 1
        assert result["receipt_id"]
        projected = _run(store.get_send_receipt(f"{HOST}:{NAME}", "send-photo-only"))
        assert projected is not None and projected["state"] == "landed"
        assert projected["content_kind"] == "attachment_only"
        assert projected["attachment_count"] == 1
        assert projected["receipt_id"] == result["receipt_id"]
        assert blobs.reads == [SHA]
        assert len(tmux.pastes) == 1
        path = tmp_path / "attachments" / f"{SHA}.png"
        assert path.read_bytes() == PNG
        assert tmux.pastes[0] == f"Image at {path}"
    finally:
        store.stop()


def test_caption_image_accept_materialize_echo(tmp_path: Path) -> None:
    async def go() -> tuple[dict, FakeTmux, Store]:
        tmux = FakeTmux()
        comms, store, sessions = _new_comms(tmux, tmp_path)
        await _open(comms, sessions)
        result = await comms.send({
            "stream_id": f"{HOST}:{NAME}", "message": "  see this  ",
            "attachments": [{"key": SHA, "mime": "image/png", "bytes": len(PNG)}],
            "optimistic_id": "opt-caption", "msg_id": "m-caption",
        })
        return result, tmux, store

    result, tmux, store = _run(go())
    try:
        assert result["delivery"] == "landed"
        path = tmp_path / "attachments" / f"{SHA}.png"
        assert tmux.pastes == [f"Look at the image file at {path}, then respond to the user's message: see this"]
    finally:
        store.stop()


def test_remote_attachment_materializes_on_agent_host(tmp_path: Path) -> None:
    async def go() -> tuple[dict, FakeTmux, Store]:
        local_tmux = FakeTmux()
        remote_tmux = FakeTmux()
        hosts = FakeHosts(local_tmux, remote_tmux=remote_tmux)
        comms, store, sessions = _new_comms(
            local_tmux, tmp_path, hosts=hosts,
        )
        await _open(comms, sessions, REMOTE)
        result = await comms.send({
            "stream_id": f"{REMOTE}:{NAME}", "attachments": [{"key": SHA, "mime": "image/png"}],
        })
        return result, remote_tmux, store

    result, tmux, store = _run(go())
    try:
        assert result["host"] == REMOTE
        assert len(tmux.staged) == 1
        path, data = tmux.staged[0]
        assert path == f"/tmp/public-fixture/.cache/pentacle-stream/attachments/{SHA}.png"
        assert data == PNG
        assert tmux.pastes == [f"Image at {path}"]
    finally:
        store.stop()


VALID = {"key": SHA, "mime": "image/png"}


@pytest.mark.parametrize(
    ("attachments", "expected"),
    [
        ({}, "attachment_invalid"),
        ([{}], "attachment_invalid"),
        ([{**VALID, "key": "g" * 64}], "attachment_invalid"),
        ([{**VALID, "mime": "image/gif"}], "attachment_invalid"),
        ([{**VALID, "width": True}], "attachment_invalid"),
        ([{**VALID, "height": -1}], "attachment_invalid"),
        ([{**VALID, "bytes": 25 * 1024 * 1024 + 1}], "attachment_invalid"),
        ([VALID] * 6, "attachment_invalid"),
    ],
)
def test_attachment_rejections_are_pre_paste(tmp_path: Path, attachments, expected: str) -> None:
    async def go() -> tuple[VerbError | None, FakeTmux, FakeBlobStore, Store]:
        tmux = FakeTmux()
        blobs = FakeBlobStore()
        comms, store, sessions = _new_comms(tmux, tmp_path, blob_store=blobs)
        await _open(comms, sessions)
        try:
            await comms.send({"stream_id": f"{HOST}:{NAME}", "message": "caption", "attachments": attachments})
        except VerbError as exc:
            return exc, tmux, blobs, store
        return None, tmux, blobs, store

    error, tmux, blobs, store = _run(go())
    try:
        assert error is not None and error.code == expected
        assert tmux.pastes == []
        assert blobs.reads == []
    finally:
        store.stop()


def test_attachment_fetch_failure_is_durable_not_landed(tmp_path: Path) -> None:
    async def go() -> tuple[dict, FakeTmux, FakeBlobStore, Store]:
        tmux = FakeTmux()
        blobs = FakeBlobStore(error=ValueError("missing blob"))
        comms, store, sessions = _new_comms(tmux, tmp_path, blob_store=blobs)
        await _open(comms, sessions)
        result = await comms.send({
            "stream_id": f"{HOST}:{NAME}", "message": "caption", "attachments": [VALID],
            "request_id": "send-fetch-failure",
        })
        return result, tmux, blobs, store

    result, tmux, blobs, store = _run(go())
    try:
        assert result["delivery"] == "not_landed"
        assert result["receipt_id"]
        projected = _run(store.get_send_receipt(f"{HOST}:{NAME}", "send-fetch-failure"))
        assert projected is not None and projected["state"] == "not_landed"
        assert projected["reason"] == "attachment_fetch_failed"
        assert blobs.reads == [SHA]
        assert tmux.pastes == []
    finally:
        store.stop()


def test_remote_attachment_staging_failure_is_durable_not_landed(tmp_path: Path) -> None:
    async def go() -> tuple[dict, FakeTmux, Store]:
        local_tmux = FakeTmux()
        remote_tmux = FakeTmux()
        hosts = FakeHosts(local_tmux, remote_tmux=remote_tmux, fail_stage=True)
        comms, store, sessions = _new_comms(local_tmux, tmp_path, hosts=hosts)
        await _open(comms, sessions, REMOTE)
        result = await comms.send({
            "stream_id": f"{REMOTE}:{NAME}", "attachments": [VALID],
            "request_id": "send-stage-failure",
        })
        return result, remote_tmux, store

    result, tmux, store = _run(go())
    try:
        assert result["delivery"] == "not_landed"
        projected = _run(store.get_send_receipt(f"{REMOTE}:{NAME}", "send-stage-failure"))
        assert projected is not None and projected["state"] == "not_landed"
        assert projected["reason"] == "attachment_stage_failed"
        assert tmux.pastes == []
    finally:
        store.stop()


@pytest.mark.parametrize(
    ("phase", "expected_state", "expected_delivery"),
    [
        ("not_started", "not_landed", "not_landed"),
        ("body_maybe_pasted", "accepted", "committed_pending_proof"),
        ("enter_failed", "accepted", "committed_pending_proof"),
    ],
)
def test_paste_failure_phase_appends_durable_receipt(
    tmp_path: Path, phase: str, expected_state: str, expected_delivery: str,
) -> None:
    async def go() -> tuple[dict, Store]:
        tmux = FakeTmux(fail_phase=phase)
        comms, store, sessions = _new_comms(tmux, tmp_path)
        await _open(comms, sessions)
        result = await comms.send({
            "stream_id": f"{HOST}:{NAME}", "message": "will fail", "optimistic_id": "opt-fail",
            "request_id": f"send-paste-{phase}",
        })
        return result, store

    result, store = _run(go())
    try:
        assert result["delivery"] == expected_delivery
        projected = _run(store.get_send_receipt(f"{HOST}:{NAME}", f"send-paste-{phase}"))
        assert projected is not None and projected["state"] == expected_state
    finally:
        store.stop()


def test_text_only_contract_matrix(tmp_path: Path) -> None:
    async def go() -> tuple[list[dict], FakeTmux, Store]:
        tmux = FakeTmux()
        comms, store, sessions = _new_comms(tmux, tmp_path)
        await _open(comms, sessions)
        replies = [
            await comms.send({"stream_id": f"{HOST}:{NAME}", "message": "preferred", "text": "ignored"}),
            await comms.send({"stream_id": f"{HOST}:{NAME}", "text": "text alias"}),
            await comms.send({"stream_id": f"{HOST}:{NAME}", "message": "\x1b[31mclean\x1b[0m", "sanitize": True}),
            await comms.send({"stream_id": f"{HOST}:{NAME}", "message": "urgent", "urgent": True}),
        ]
        return replies, tmux, store

    replies, tmux, store = _run(go())
    try:
        assert all(reply["delivery"] == "landed" for reply in replies)
        assert tmux.pastes == ["preferred", "text alias", "clean", "urgent"]
        assert tmux.escapes == 1
    finally:
        store.stop()


def test_sanitize_does_not_hide_invalid_attachment_container(tmp_path: Path) -> None:
    tmux = FakeTmux()
    comms, store, sessions = _new_comms(tmux, tmp_path)
    try:
        _run(sessions.open(HOST, NAME, provider="codex"))
        with pytest.raises(VerbError) as raised:
            _run(
                comms.send(
                    {
                        "stream_id": f"{HOST}:{NAME}",
                        "message": "\x1b[31m",
                        "sanitize": True,
                        "attachments": {},
                    }
                )
            )
        assert raised.value.code == "attachment_invalid"
        assert not tmux.pastes
    finally:
        store.stop()


def test_whitespace_only_text_rejects_before_paste(tmp_path: Path) -> None:
    async def go() -> tuple[VerbError | None, FakeTmux, Store]:
        tmux = FakeTmux()
        comms, store, sessions = _new_comms(tmux, tmp_path)
        await _open(comms, sessions)
        try:
            await comms.send({"stream_id": f"{HOST}:{NAME}", "message": "  \n\t"})
        except VerbError as exc:
            return exc, tmux, store
        return None, tmux, store

    error, tmux, store = _run(go())
    try:
        assert error is not None and error.code == "bad_request"
        assert tmux.pastes == []
    finally:
        store.stop()


def test_stale_history_does_not_confirm_identical_active_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import comms as comms_module

    monkeypatch.setattr(comms_module, "SUBMISSION_EVIDENCE_TIMEOUT_S", 0.01)
    monkeypatch.setattr(comms_module, "SUBMISSION_EVIDENCE_POLL_S", 0.001)
    tmux = FakeTmux(submit_on_paste=False)
    tmux.screen = "⏺ repeat\n❯ repeat\n"
    comms, store, sessions = _new_comms(tmux, tmp_path)
    try:
        _run(sessions.open(HOST, NAME, provider="codex"))
        result = _run(comms.send({"stream_id": f"{HOST}:{NAME}", "message": "repeat"}))
        assert result["delivery"] == "not_landed"
        assert result["submission_confirmed"] is False
        assert result["reason"] == "active_draft"
        assert result["attempts"] == 2
        assert tmux.pastes == ["repeat"]
        assert tmux.enters == 1
    finally:
        store.stop()


def test_enter_retry_never_repastes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import comms

    monkeypatch.setattr(comms, "SUBMISSION_EVIDENCE_TIMEOUT_S", 0.01)
    monkeypatch.setattr(comms, "SUBMISSION_EVIDENCE_POLL_S", 0.001)

    async def go() -> tuple[dict, FakeTmux, Store]:
        tmux = FakeTmux(submit_on_paste=False, submit_on_retry=True)
        tmux.screen = "⏵⏵ bypass permissions on\n❯ retry me"
        comms_obj, store, sessions = _new_comms(tmux, tmp_path)
        await _open(comms_obj, sessions)
        result = await comms_obj.send({"stream_id": f"{HOST}:{NAME}", "message": "retry me"})
        return result, tmux, store

    result, tmux, store = _run(go())
    try:
        assert result["delivery"] == "landed"
        assert result["submission_confirmed"] is True
        assert result["attempt"] == 2
        assert tmux.pastes == ["retry me"]
        assert tmux.enters == 1
    finally:
        store.stop()


def test_unexpected_injection_failure_stays_durably_pending(tmp_path: Path) -> None:
    tmux = FakeTmux()
    tmux.paste_error = RuntimeError("tmux disconnected")
    comms, store, sessions = _new_comms(tmux, tmp_path)

    try:
        asyncio.run(_open(comms, sessions))
        result = asyncio.run(comms.send({
            "stream_id": f"{HOST}:{NAME}", "text": "hello", "request_id": "send-paste-exception",
        }))
        assert result["delivery"] == "committed_pending_proof"
        assert result["action_status"] == "committed"
        assert result["confirmation_status"] == "pending"
        assert result["do_not_resubmit"] is True
        assert "DO NOT RESUBMIT" in result["retry_guidance"]
        projected = asyncio.run(store.get_send_receipt(f"{HOST}:{NAME}", "send-paste-exception"))
        assert projected is not None and projected["state"] == "accepted"
    finally:
        store.stop()


def test_unconfirmed_never_landed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import comms

    monkeypatch.setattr(comms, "SUBMISSION_EVIDENCE_TIMEOUT_S", 0.01)
    monkeypatch.setattr(comms, "SUBMISSION_EVIDENCE_POLL_S", 0.001)

    async def go() -> tuple[dict, FakeTmux, Store]:
        tmux = FakeTmux(submit_on_paste=False, submit_on_retry=False)
        tmux.screen = (
            "OpenAI Codex\n─────────\n› never landed\n"
            "  gpt-5-codex high"
        )
        comms_obj, store, sessions = _new_comms(tmux, tmp_path)
        await sessions.open(HOST, NAME, provider="codex")
        result = await comms_obj.send({"stream_id": f"{HOST}:{NAME}", "message": "never landed"})
        return result, tmux, store

    result, tmux, store = _run(go())
    try:
        assert result["delivery"] == "not_landed"
        assert result["submission_confirmed"] is False
        assert result["reason"] == "active_draft"
        assert result["attempts"] == 2
        assert tmux.pastes == ["never landed"]
        assert tmux.enters == 1
    finally:
        store.stop()
