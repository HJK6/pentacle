#!/usr/bin/env python3
"""Error Alerts acceptance runner: imports the explicit product root only."""
from __future__ import annotations
import argparse, asyncio, base64, hashlib, importlib.metadata, json, os, pathlib, shutil, subprocess, sys, tempfile, time, traceback, uuid


def source_identity(root):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=root, text=True).strip()

    if pathlib.Path(git("rev-parse", "--show-toplevel")).resolve() != root:
        raise ValueError("product_root_must_be_checkout_root")
    if git("status", "--porcelain", "--untracked-files=all"):
        raise ValueError("product_root_must_be_clean")
    return {
        "product_sha": git("rev-parse", "HEAD"),
        "product_tree": git("rev-parse", "HEAD^{tree}"),
        "origin": git("remote", "get-url", "origin"),
        "python_executable": str(pathlib.Path(sys.executable).resolve()),
        "python_sha256": hashlib.sha256(
            pathlib.Path(sys.executable).read_bytes()
        ).hexdigest(),
        "python_packages": dict(
            sorted(
                (d.metadata["Name"], d.version)
                for d in importlib.metadata.distributions()
            )
        ),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--product-root", type=pathlib.Path, required=True)
    p.add_argument("--out", type=pathlib.Path, required=True)
    p.add_argument(
        "--mode",
        choices=["baseline-red", "candidate", "installed-consumer"],
        required=True,
    )
    p.add_argument("--consumer-manifest", type=pathlib.Path)
    p.add_argument(
        "--only-d01",
        action="store_true",
        help="Bounded diagnostic; never a complete candidate gate",
    )
    a = p.parse_args()
    root = a.product_root.resolve()
    service = root / "services/chat-stream-v2"
    harness = pathlib.Path(__file__).resolve().parents[1] / "tests"
    if a.mode == "installed-consumer":
        a.out.mkdir(parents=True, exist_ok=True)
        result = {
            "mode": a.mode,
            "passed": False,
            "classification": "BLOCKED",
            "reason": (
                "FD installed-client artifact and executable serializer census required"
                if not a.consumer_manifest
                else "Installed-consumer runner cells not yet implemented"
            ),
            "owned_listeners": 0,
        }
        (a.out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))
        return 1
    for k in list(os.environ):
        if k.startswith(("PENTACLE_", "AGENT_ORCH_", "OPENAI_", "ANTHROPIC_", "AWS_")):
            os.environ.pop(k, None)
    os.environ["PENTACLE_HOST_ID"] = "fixture"
    os.environ["PENTACLE_ERROR_ALERTS_MODE"] = "on"
    os.environ.pop("TMUX", None)
    os.environ.pop("TMUX_TMPDIR", None)
    sys.path[:0] = [
        str(service),
        str(root / "services"),
        str(root / "services/_shared"),
        str(root / "services/agent-orch"),
        str(harness),
    ]
    a.out.mkdir(parents=True, exist_ok=True)
    try:
        identity = source_identity(root)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        result = {
            "mode": a.mode,
            "passed": False,
            "classification": "HARNESS_ERROR",
            "preflight": str(exc),
            "owned_listeners": 0,
            "owned_processes": 0,
        }
        (a.out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))
        return 1
    with tempfile.TemporaryDirectory(prefix="error-alerts-gate-") as tmp:
        os.environ["PENTACLE_CONFIG_ROOT"] = tmp + "/config"
        os.environ["PENTACLE_DATA_ROOT"] = tmp + "/data"
        os.environ["PENTACLE_OPERATOR_CREDENTIALS"] = tmp + "/unused-credentials.json"
        result = asyncio.run(run(a, root, pathlib.Path(tmp)))
        result["identity"] = identity
    (a.out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
    return 0 if result["passed"] else 1


async def read_only_projection(h):
    """The FD query: typed facts and their linked outbox delivery, read-only."""
    service = getattr(h.server, "error_alerts", None)
    if service is None:
        return []  # baseline product: no typed facts or projection exist
    notices = {r["notice_id"]: r for r in await service.notice_rows()}
    out = []
    for fact in await h.notify._db.call("error_rows"):
        ctx = fact["error_context"]
        for nid in ctx["notice_ids"] or [None]:
            d = service.delivery(notices.get(nid), notices, ctx)
            out.append({
                "notification_id": fact["notification_id"],
                "family": ctx["family"],
                "code": ctx["code"],
                "condition": ctx["condition"],
                "operation_id": ctx.get("operation_id") or ctx.get("episode_id"),
                "delivery": d,
            })
    return out


async def run(a, root, tmp):
    from error_alerts_fixture import ErrorAlertsHarness
    import server, store

    for module in (server, store):
        assert pathlib.Path(module.__file__).resolve().is_relative_to(root)
    result = {
        "mode": a.mode,
        "product_root": str(root),
        "product_sha": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip(),
        "python": sys.version,
        "cells": {},
        "passed": False,
    }
    result["harness_sha256"] = hashlib.sha256(
        pathlib.Path(__file__).read_bytes()
        + (
            pathlib.Path(__file__).resolve().parents[1]
            / "tests/error_alerts_fixture.py"
        ).read_bytes()
    ).hexdigest()
    result["harness_files"] = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (pathlib.Path(__file__).resolve().parents[1] / "tests").glob(
            "error_alerts_*.py"
        )
    }
    # The final RED/GREEN pair uses byte-identical production transport;
    # only the imported product root changes.
    h = ErrorAlertsHarness(tmp, real_transport=True)
    try:
        await h.start()
        runtime = {
            "owner_pid": os.getpid(),
            "product_sha": result["product_sha"],
            "host": "127.0.0.1",
            "port": h.port,
            "fixture_root": str(tmp),
            "sessions_db": str(tmp / "sessions.db"),
            "notifications_db": str(tmp / "notifications.db"),
            "provider_pid": h.provider.pid,
            "tmux_socket": h.provider.socket,
            "disposable": True,
        }
        (a.out / "runtime.json").write_text(json.dumps(runtime, indent=2) + "\n")
        content = b"fixture voice bytes"
        op = str(uuid.uuid4())
        rid = "fixture-upload"
        intent = {
            "version": 1,
            "operation_id": op,
            "origin_stream_id": "fixture:v2-test",
            "origin_generation": h.origin["session_generation"],
            "operation_kind": "voice_chat",
            "client_build": "fixture-v1",
        }
        async with h.client() as client:
            # Baseline records future intent attempt; successful generic upload is the actual control.
            init = await client.rpc(
                "upload_blob_init",
                request_id=rid,
                purpose="generic",
                size_hint_bytes=len(content),
                voice_operation=intent,
            )
            if init.get("type") != "upload_blob.init.ok":
                result["intent_refusal"] = init
                init = await client.rpc(
                    "upload_blob_init",
                    request_id=rid,
                    purpose="generic",
                    size_hint_bytes=len(content),
                )
            assert init["type"] == "upload_blob.init.ok", init
            done = await client.rpc(
                "upload_blob_chunk",
                request_id=rid,
                data_b64=base64.b64encode(content).decode(),
                final=True,
            )
            assert done["type"] == "upload_blob.ok", done
            assert done["blob_sha"] == hashlib.sha256(content).hexdigest()
            result["upload_control"] = {"committed": True, "sha": done["blob_sha"]}
        started = time.monotonic()
        await asyncio.sleep(67)
        result["timer_elapsed_s"] = time.monotonic() - started
        if hasattr(h.server, "error_alerts") and h.server.error_alerts is not None:
            rows = await h.notify._db.call("error_rows")
        else:
            rows = await h.notify._db.call("list_notifications", limit=100)
        typed = [r for r in rows if r.get("error_context")]
        result["cells"]["D01"] = {
            "classification": "PRODUCT_FAIL" if not typed else "PASS",
            "predicate": "committed_voice_upload_interruption_creates_canonical_alert",
            "actual_alerts": len(typed),
            "provider_submissions": len(h.provider.pastes),
        }
        result["review"] = await read_only_projection(h)
        result["first_failed_predicate"] = (
            None
            if typed
            else "D01: no canonical alert after committed upload and 67 seconds without transcription"
        )
        if a.mode == "baseline-red":
            result["passed"] = (
                not typed
                and result["upload_control"]["committed"]
                and not h.provider.pastes
            )
        else:
            matches = [r for r in result["review"] if r["operation_id"] == op]
            d01_pass = (
                len(matches) == 1
                and matches[0]["delivery"]["state"] == "delivered"
                and bool(matches[0]["delivery"]["proof_at"])
                and len(h.provider.pastes) == 1
            )
            result["cells"]["D01"].update(
                classification="PASS" if d01_pass else "PRODUCT_FAIL",
                production_transport=True,
                proved_alerts=sum(
                    r["delivery"]["state"] == "delivered" for r in matches
                ),
            )
            if not d01_pass:
                result["first_failed_predicate"] = (
                    "D01: canonical alert and generation-bound provider USER proof required"
                )
            if d01_pass and not a.only_d01:
                from error_alerts_restart_scenario import restart_boundaries
                from error_alerts_security_scenario import (
                    authentication,
                    negative_reports,
                )

                result["cells"]["D02"] = await restart_boundaries(h)
                from error_alerts_live_cells import (
                    installed_transcribe_failure,
                    isolated,
                    rebind_follows_generation,
                )

                async def strict_frames(h2):
                    return {"classification": "EVIDENCE", "strict_frames": await negative_reports(h2)}

                # H2/H3: fresh isolated fixtures so no cell's rate-limit history
                # holds or folds another's (limits and clocks unchanged).
                result["cells"].update(await isolated("h2", tmp, a.out, [
                    ("A01", authentication), ("A02", strict_frames),
                    ("I01", installed_transcribe_failure)]))
                result["cells"].update(await isolated("h3", tmp, a.out, [
                    ("R01", rebind_follows_generation)]))
            # Narrowed delivery-core gate (amendment v4 §6). Every required
            # cell must PASS; a reduced run (--only-d01) never passes.
            required = ("D01", "D02", "A01", "I01", "R01")
            result["required_cells"] = list(required)
            result["passed"] = not a.only_d01 and all(
                result["cells"].get(c, {}).get("classification") == "PASS"
                for c in required
            )
    except Exception as exc:
        result["classification"] = (
            "PRODUCT_FAIL"
            if type(exc).__name__ == "ProductPredicateFailure"
            else "HARNESS_ERROR"
        )
        result["exception"] = type(exc).__name__
        result["traceback"] = traceback.format_exc()
    finally:
        # Retain only synthetic evidence, never credential files. A failed
        # proof must be traceable after owned processes and scratch DBs close.
        trace = {"provider_identity": getattr(h.provider, "identity", None)}
        try:
            for source in [
                tmp / "provider-input.jsonl",
                *tmp.glob(".codex/sessions/*.jsonl"),
            ]:
                if source.exists():
                    target = a.out / (
                        "provider-input.jsonl"
                        if source.name == "provider-input.jsonl"
                        else "provider-user-" + source.name
                    )
                    shutil.copyfile(source, target)
            if h.store:
                trace["session"] = await h.store.fetch_session("fixture", "v2-test")
                trace["events"] = await h.store.fetch_session_event_tail(
                    "fixture:v2-test", limit=100
                )
                trace["tell_deliveries"] = await h.store.list_tell_deliveries(
                    "fixture:v2-test", limit=100
                )
                # R01 rebind target, when that cell ran.
                trace["rebind_target"] = {
                    "session": await h.store.fetch_session("fixture", "v2-next"),
                    "events": await h.store.fetch_session_event_tail(
                        "fixture:v2-next", limit=100
                    ),
                    "tell_deliveries": await h.store.list_tell_deliveries(
                        "fixture:v2-next", limit=100
                    ),
                }
            if getattr(h, "ingest", None):
                trace["ingest"] = {
                    sid: {
                        k: getattr(state, k)
                        for k in (
                            "path",
                            "offset",
                            "generation",
                            "session_id",
                            "failures",
                        )
                    }
                    for sid, state in h.ingest._streams.items()
                }
            if getattr(h.server, "error_alerts", None):
                trace["notices"] = await h.server.error_alerts.notice_rows()
            (a.out / "proof-trace.json").write_text(json.dumps(trace, indent=2) + "\n")
        except Exception:
            result["evidence_retention_error"] = traceback.format_exc()
        try:
            await h.stop()
        except Exception:
            result["classification"] = "CLEANUP_FAIL"
            result["passed"] = False
        result["cleanup"] = h.cleanup
        # Joined stores are closed here; retain their raw disposable bytes,
        # including any remaining SQLite sidecars, before TemporaryDirectory.
        for pattern in ("sessions.db*", "notifications.db*"):
            for source in tmp.glob(pattern):
                shutil.copyfile(source, a.out / source.name)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
