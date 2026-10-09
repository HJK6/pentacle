"""Real 150-second ASR deadline, including restart and an interrupted client."""

import asyncio
from datetime import datetime
import json
import threading
import time

from error_alerts_ui_fixture import produce_voice
from transcribe import Transcriber


class ProductPredicateFailure(AssertionError):
    pass


def require(predicate, message):
    if not predicate:
        raise ProductPredicateFailure(message)


class BlockedPoster:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.calls = 0

    def __call__(self, url, *, data, content_type, timeout):
        self.calls += 1
        self.timeout = timeout
        self.entered.set()
        try:
            # Fault at the external IO boundary, not a changed product timer.
            if not self.release.wait(180):
                raise RuntimeError("fixture ASR release missing")
            return (
                200,
                json.dumps({"text": "disposable transcript", "duration_s": 1}).encode(),
            )
        finally:
            self.finished.set()


async def transcribe_deadline(h):
    poster = BlockedPoster()
    pending = None
    before = len(h.provider.pastes)
    try:
        made = await produce_voice(h)
        oid = made["operation_id"]
        op = await h.store.voice_get("operator:" + h.credential_id, oid)
        intent = {
            "version": 1,
            "operation_id": oid,
            "origin_stream_id": op["origin_stream_id"],
            "origin_generation": op["origin_generation"],
            "operation_kind": op["operation_kind"],
            "client_build": op["client_build"],
        }
        h.server._transcriber = Transcriber(
            h.blobs, mic_api="http://127.0.0.1:1", http_post=poster
        )
        async with h.client() as client:
            pending = asyncio.create_task(
                client.rpc(
                    "transcribe_blob",
                    request_id="transcribe-" + oid,
                    voice_operation=intent,
                    blob_sha=op["data"]["blob_sha"],
                    mime="audio/mp4",
                    timeout=180,
                )
            )
            assert await asyncio.to_thread(
                poster.entered.wait, 5
            ), "actual backend IO boundary not reached"
            op = await h.store.voice_get("operator:" + h.credential_id, oid)
            started_at = op["data"]["transcribe_started"]
            started_epoch = datetime.fromisoformat(
                started_at.replace("Z", "+00:00")
            ).timestamp()
            await asyncio.sleep(1)
        await asyncio.gather(pending, return_exceptions=True)
        pending = None
        await h.restart()
        reopened = await h.store.voice_get("operator:" + h.credential_id, oid)
        require(
            reopened["data"]["transcribe_started"] == started_at,
            "restart changed original ASR deadline",
        )
        require(
            reopened["origin_generation"] == intent["origin_generation"],
            "restart changed immutable origin",
        )
        elapsed = time.time() - started_epoch
        await asyncio.sleep(max(0, 150 + 7 - elapsed))
        async with h.client() as client:
            result = await client.rpc(
                "error.list", request_id="asr-deadline-review", id=oid
            )
        items = result.get("items", [])
        require(len(items) == 1, "ASR missing milestone did not yield one alert")
        alert = items[0]
        require(
            alert["code"] == "transcribe_milestone_missing",
            "wrong ASR missing-milestone code",
        )
        require(
            alert["condition"] == "unknown",
            "elapsed time cannot prove a transcription failure",
        )
        require(
            alert["delivery"]["state"] == "delivered" and alert["delivery"]["proof_at"],
            "ASR alert lacks provider USER proof",
        )
        require(poster.calls == 1, "restart or deadline automatically replayed ASR")
        observed = [text for text in h.provider.pastes[before:] if oid in text]
        require(
            len(observed) == 1,
            "ASR alert did not produce exactly one observed submission",
        )
        return {
            "classification": "PASS",
            "scope": "D05 wall-clock timer/restart/disconnect subcell only",
            "elapsed_s": time.time() - started_epoch,
            "original_started_at": started_at,
            "backend_calls": poster.calls,
            "backend_timeout_parameter_s": poster.timeout,
            "condition": alert["condition"],
            "notice_id": alert["delivery"]["notice_id"],
            "proof_at": alert["delivery"]["proof_at"],
            "provider_submissions": len(observed),
        }
    finally:
        poster.release.set()
        if poster.entered.is_set():
            assert await asyncio.to_thread(
                poster.finished.wait, 5
            ), "owned ASR worker did not join"
        if pending:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
