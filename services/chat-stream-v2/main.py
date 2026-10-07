"""main.py — wiring, config, startup order.

Contract (v2_design.md module table):
  owns: wiring, config, startup order.
  notes: port binds **first**; all init after the accept-loop is live.

Startup order is the whole point of this file (spec constraint 1, and the
port-bind-after-reconcile wedge root cause):

    bind() -> serve hello/snapshot -> THEN start background tasks

Nothing that scans, reconciles, probes hosts, or opens a large DB may run
before `Server.bind()` returns. B10 restart survival (v2_design.md): shutdown
never touches panes; startup re-adopts live panes by stream id from persisted
pane state (implemented with `sessions.py`, not yet in the skeleton).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import subprocess
import sys

import socket
from pathlib import Path

from alerts import Alerts
from assistant_composite import AssistantComposite, AssistantCompositeConfig
from assistant_router import AssistantRouterAdapter
import launch
from assets import DEFAULT_ASSETS_DB, Assets
from blobs import DEFAULT_BLOB_ROOT, BlobStore
from comms import Comms
from daemon_lifecycle import DaemonLifecycle
from hosts import Hosts, HostsConfig
from machines import load_machines
from ledger import (
    AwaiterResolutionConfig,
    AwaiterResolutionJob,
    Ledger,
    NudgeConfig,
    NudgeJob,
)
from logging_config import configure_logging
from machine_stats import STATS_INTERVAL_S, sample_machine_stats
from inventory import InventoryEmitter
from work_lanes_projection import WorkLanesInventory
from mirror import Mirror, MirrorConfig
from ingest import Ingest, IngestConfig
from notify import DEFAULT_NOTIFICATIONS_DB, Notify, NotificationExpiry
from outbound_notices import (
    NOTICE_INTERVAL_S,
    NOTICE_MAX_PER_PASS,
    OutboundNoticeConfig,
    OutboundNoticeQueue,
)
from presence import PresenceConfig, RemotePresence
from reconciler import ReconcileConfig, SessionReconciler
from event_push import EventPush
from usage_history import HISTORY_FILENAME, HistoryLog
from usage_provenance import ProvenanceSink
from retention import RetentionConfig, RetentionJob
from routing_integrity import RoutingIntegrity
from server import RECENT_LIMIT, Server
from sessions import Sessions
from spawnctl import SpawnCtl
from tmux_transport import Tmux
from store import Store
from submission_events import DurableUserEventProof
from uiverbs import UIVerbs
from usage_publisher import UsageStatePublisher
from voice_answers import dispatch_block as voice_answers_dispatch_block
from window_schedule import WindowSchedule

log = logging.getLogger("chat_streamd_v2")


def _wire_cosmo_reply_push(assistant_composites: dict, push_reply) -> None:
    """Wire the Cosmo reply-push hook to the Daff composite ONLY.

    Bart must never push to the Cosmo audience (a bart-scoped token would
    otherwise receive Bart reply text), so Bart's composite gets NO hook and its
    publish never invokes push.  Deny-by-default: any non-Daff composite is left
    without a reply-push hook.
    """
    for name, composite in (assistant_composites or {}).items():
        composite.reply_push = push_reply if name == "daff" else None


def _read_checkout_sha() -> str:
    """Read this daemon checkout's HEAD; callers must run it off the event loop."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _read_deploy_stamp_sha(path: str | None) -> str:
    """The deploy stamp deploy.py writes records the deployed SHA; use it when a
    non-git runtime (an exported tree) has no reachable git HEAD."""
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8") as handle:
            stamp = json.load(handle)
    except (OSError, ValueError):
        return ""
    sha = stamp.get("sha") if isinstance(stamp, dict) else ""
    return str(sha or "").strip()


def _read_version_file_sha(path: Path) -> str:
    """A release tarball (no .git and no deploy stamp) may ship a VERSION file
    whose first line records the built SHA/version."""
    try:
        lines = path.read_text(encoding="utf-8").strip().splitlines()
    except OSError:
        return ""
    return lines[0].strip() if lines else ""


def _select_runtime_sha(checkout_sha: str, deploy_stamp_path: str | None, version_path: Path) -> str:
    """Resolve the runtime SHA the welcome advertises: git HEAD, else the
    deploy-stamp SHA, else a shipped VERSION file. Empty only when none exist, so
    a non-git/tarball install no longer reports a spurious empty 'Daemon unknown'."""
    if checkout_sha.strip():
        return checkout_sha.strip()
    stamp_sha = _read_deploy_stamp_sha(deploy_stamp_path)
    if stamp_sha:
        return stamp_sha
    return _read_version_file_sha(version_path)


def _resolve_runtime_sha() -> str:
    """Off-loop runtime-SHA resolution; callers must run it off the event loop."""
    return _select_runtime_sha(
        _read_checkout_sha(),
        os.environ.get("PENTACLE_DEPLOY_STAMP_PATH"),
        Path(__file__).resolve().parents[2] / "VERSION",
    )


async def _run_machine_stats(server: Server, host: str) -> None:
    while True:
        try:
            sample = await asyncio.to_thread(sample_machine_stats, host)
            await server.merge_host_stats(host, sample)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a host sample must not stop the daemon
            log.warning("machine stats sample failed host=%s: %s", host, exc)
        await asyncio.sleep(STATS_INTERVAL_S)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="chat_streamd v2 (minimal core)")
    p.add_argument("--host", default="127.0.0.1", help="single-interface bind (alias for one --bind)")
    # v1 parity: bind multiple interfaces (Tailscale IP + 127.0.0.1) on one port.
    # Repeatable; when given it supersedes --host. All binds share --port, so a
    # multi-bind daemon needs an explicit port (not 0).
    p.add_argument("--bind", action="append", default=[], help="interface to bind; repeat for several")
    p.add_argument("--port", type=int, default=7791, help="0 picks a free port")
    p.add_argument("--db", default=":memory:", help="sessions.db path")
    p.add_argument("--log-level", default="INFO")
    p.add_argument("--local-host", default=socket.gethostname().split(".")[0], help="this machine's fleet name")
    p.add_argument("--tmux-bin", default="tmux")
    p.add_argument("--ssh-bin", default="ssh", help="ssh binary for remote host transport")
    p.add_argument("--spawn-command", default="", help="default provider command for `spawn`")
    # Local machine profile for the tuple-path launch-command + env construction
    # (item 1, lifted from v1). Overridable so a test can point a provider binary
    # at a stub and cwd at a tmp dir (the "profile override" the spawn-parity
    # validation uses). Lane 5 owns the full machines.json subsystem; these are
    # the lane-7 stopgap with v1's `_default_local_machine` defaults.
    p.add_argument("--claude-bin", default="", help="claude binary for tuple-path spawn")
    p.add_argument("--codex-bin", default="", help="codex binary for tuple-path spawn")
    p.add_argument("--spawn-cwd", default="", help="cwd a tuple-path spawn launches in")
    p.add_argument("--projects-root", default="", help="provider transcript projects root")
    p.add_argument("--agent-orch-bin-dir", default="", help="dir put on a spawned pane's PATH")
    p.add_argument("--notifications-db", default=DEFAULT_NOTIFICATIONS_DB, help="notifications.db path (v1's, for cutover)")
    p.add_argument("--assets-db", default=DEFAULT_ASSETS_DB, help="assets.db path (v1's, for cutover)")
    p.add_argument("--blob-root", default=DEFAULT_BLOB_ROOT, help="content-addressed blob store root")
    p.add_argument("--notification-expiry-interval-s", type=float, default=60.0)
    # Named kill switches, one per implemented recurring background task. Keep
    # this list aligned with the task owners and module docstrings; the
    # kill-switch actor pins that the two lists cannot drift.
    for task in (
        "mirror", "ingest", "hosts", "reconciler", "retention",
        "nudges", "notification-expiry",
        "remote-presence", "event-push-ingest", "routing-integrity",
        "outbound-notices",
        "awaiter-resolution",
        "window-schedule",
        "usage-state-publisher",
    ):
        p.add_argument(f"--disable-{task}", action="store_true")
    return p.parse_args(argv)


def _local_machine_for_spawn(args: argparse.Namespace, configured_machines: tuple[object, ...]):
    """Resolve the local launch profile from the same config as the peers."""
    local_profile = next(
        (
            profile
            for profile in configured_machines
            if profile.name == args.local_host and profile.is_local
        ),
        None,
    )
    if local_profile is not None:
        return launch.local_machine_from_config(
            local_profile,
            cwd=args.spawn_cwd or None,
            claude_bin=args.claude_bin or None,
            codex_bin=args.codex_bin or None,
            projects_root=args.projects_root or None,
            agent_orch_bin_dir=args.agent_orch_bin_dir or None,
        )
    return launch.local_machine(
        args.local_host,
        cwd=args.spawn_cwd or None,
        claude_bin=args.claude_bin or None,
        codex_bin=args.codex_bin or None,
        projects_root=args.projects_root or None,
        agent_orch_bin_dir=args.agent_orch_bin_dir or None,
    )


def _spec_catalog(sessions: Sessions) -> object | None:
    """Build the shared memory catalog without importing the v1 service tree."""
    services_root = Path(__file__).resolve().parents[1]
    if str(services_root) not in sys.path:
        sys.path.insert(0, str(services_root))
    try:
        from _shared.specs_service import SpecsSubsystem
    except ImportError as exc:  # pragma: no cover - deployment packaging fallback
        log.warning("spec binding catalog unavailable: %s", exc)
        return None
    return SpecsSubsystem(
        session_summaries=sessions.list_open,
        changed_callback=lambda _spec_ids: None,
    )


async def run(args: argparse.Namespace) -> int:
    store = Store(args.db)
    lifecycle = DaemonLifecycle(store, host=args.local_host)
    tmux = Tmux(args.tmux_bin)
    alerts = Alerts(store)
    # Peers come from `machines.json` (lifted v1 config: PENTACLE_MACHINES_JSON /
    # PENTACLE_MACHINES_FILE / ~/.config/pentacle-stream/machines.json). Every
    # machine with an ssh_target that is not this host is a peer; with none, v2
    # stays localhost-only (spawn/tell/close to a foreign host is refused). The
    # probe pool owns the SSH transport seam; its status-change broadcast is
    # wired after the server exists.
    configured_machines = load_machines(os.environ)
    peers = {
        m.name: m
        for m in configured_machines
        if not m.is_local and m.name != args.local_host
    }
    hosts = Hosts(
        local_host=args.local_host, peers=peers,
        tmux_bin=args.tmux_bin, ssh_bin=args.ssh_bin, config=HostsConfig.from_env(),
    )
    presence_config = PresenceConfig.from_env()
    sessions = Sessions(
        store, tmux=tmux, local_host=args.local_host, alerts=alerts, hosts=hosts,
        capture_timeout_s=presence_config.list_timeout_s,
    )
    machine = _local_machine_for_spawn(args, configured_machines)
    specs = _spec_catalog(sessions)
    store.set_spec_identity_resolver(
        getattr(specs, "canonical_spec_identity", None) if specs is not None else None,
    )
    # Build the prompt blob store before spawnctl so blob-backed
    # `initial_prompt_file` requests can be dereferenced at the same durable
    # handoff boundary as inline prompts.
    blobs = BlobStore(args.blob_root, attachment_store=store)
    submission_proof = DurableUserEventProof(
        store, local_host=args.local_host,
    )
    spawnctl = SpawnCtl(
        store, sessions, tmux=tmux, default_command=args.spawn_command,
        machine=machine, hosts=hosts, prompt_blobs=blobs, specs=specs,
        # Durable ownership token for reservation identity (spec §D3/INV-2): the
        # daemon instance id, so a reconcile pass can tell a finishing in-process
        # spawn (owned by THIS instance) from a dead daemon's interrupted intent.
        owner_instance_id=lifecycle.instance_id,
        submission_proof=submission_proof,
    )
    comms = Comms(
        store, sessions, spawnctl, hosts=hosts, blob_store=blobs,
        submission_proof=submission_proof,
    )
    outbound = OutboundNoticeQueue(
        store,
        comms,
        config=OutboundNoticeConfig.from_env(),
    )
    binds = list(args.bind) or [args.host]
    server = Server(
        host=binds[0], port=args.port, store=store, sessions=sessions,
        spawnctl=spawnctl, comms=comms, local_host=args.local_host, binds=binds,
        hosts=hosts,
    )
    # Fixed two-assistant map (bart, daff).  bart reads the existing unprefixed
    # env keys byte-identically; daff reads PENTACLE_ASSISTANT_DAFF_* and stays
    # inert until configured.
    assistant_configs = AssistantCompositeConfig.all_from_env()

    def _assistant_router_for(cfg: AssistantCompositeConfig):
        return (
            AssistantRouterAdapter(
                cfg.router_endpoint,
                timeout_s=cfg.router_timeout_s,
                ssh_bin=args.ssh_bin,
                action_path=cfg.router_action_path,
            ) if cfg.enabled and not cfg.direct_primary else None
        )

    async def _dispatch_assistant_route(route: dict[str, object]) -> dict[str, object]:
        """Deliver one already-intended backend turn without blocking routing.

        Shared across named assistants: the composite identity comes from the
        durable route row (its ``stream_id`` column), never a single config.
        """
        route_stream_id = str(route.get("stream_id") or "")
        target = str(route.get("route_target") or "")
        host, separator, session_name = target.partition(":")
        if not separator or not host or not session_name:
            return {"delivery": "failed", "reason": "assistant_backend_target_invalid"}
        try:
            attachments = json.loads(str(route.get("attachments_json") or "[]"))
        except (TypeError, ValueError):
            return {"delivery": "failed", "reason": "assistant_attachments_corrupt"}
        dispatch_id = str(route.get("dispatch_id") or "")
        source_message_id = str(route.get("input_identity") or "")
        body = str(route.get("body") or "")
        route_payload: dict[str, object] = {}
        try:
            loaded = json.loads(str(route.get("route_json") or "{}"))
            if isinstance(loaded, dict):
                route_payload = loaded
        except (TypeError, ValueError):
            return {"delivery": "failed", "reason": "assistant_route_payload_corrupt"}
        # The original body is preserved verbatim in this explicit envelope;
        # dispatch/correlation values come from durable daemon state, never a
        # model-generated reply id.  Attachments travel through the existing
        # send materializer unchanged.
        if route_payload.get("admission_mode") == "direct_primary":
            target_generation = str(route.get("route_target_generation") or "")
            if not target or not target_generation:
                return {"delivery": "failed", "reason": "assistant_direct_generation_conflict"}
            envelope = route_payload.get("direct_envelope")
            if not isinstance(envelope, dict) or (
                envelope.get("origin") != route_stream_id
                or envelope.get("dispatch_id") != dispatch_id
                or envelope.get("reply_to_message_id") != source_message_id
                or envelope.get("reply_to_question_id") != (str(route.get("reply_to_question_id") or "") or None)
                or envelope.get("target_stream_id") != target
                or envelope.get("target_generation") != target_generation
                or envelope.get("original_input") != {"text": body, "attachments": attachments}
                or not isinstance(envelope.get("wire_body"), str)
                or not isinstance(envelope.get("publish_command"), str)
                or envelope["publish_command"] not in envelope["wire_body"]
            ):
                return {"delivery": "failed", "reason": "assistant_direct_envelope_invalid"}
            wire_body = envelope["wire_body"]
        elif str(route.get("routing_state") or "") == "fallback_dispatched":
            routing_context = route_payload.get("routing_context")
            context_block = "{}"
            if isinstance(routing_context, dict):
                context_block = json.dumps(routing_context, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            wire_body = (
                "[assistant composite routing fallback]\n"
                f"dispatch_id: {dispatch_id}\n"
                f"reply_to_message_id: {source_message_id}\n"
                "Return no visible prose. Submit exactly one validated assistant-router/v1 "
                "classifier result with: agent-orch assistant operation --operation route.resolve "
                f"--request-id resolve:{dispatch_id} --composite-stream-id {route_stream_id} "
                f"--dispatch-id {dispatch_id} "
                f"--reply-to-message-id {source_message_id} --payload <router-result-json>\n"
                "Use only the supplied routing-context lane/unresolved IDs. If it does not establish "
                "one current target, resolve clarify.\n"
                "<assistant-routing-context>\n"
                f"{context_block}\n"
                "</assistant-routing-context>\n"
                "<assistant-original-input>\n"
                f"{body}\n"
                "</assistant-original-input>"
            )
        else:
            backend_context = route_payload.get("backend_context")
            context_block = "{}"
            if isinstance(backend_context, dict):
                context_block = json.dumps(
                    backend_context, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                )
            wire_body = (
                "[assistant composite dispatch]\n"
                f"dispatch_id: {dispatch_id}\n"
                f"reply_to_message_id: {source_message_id}\n"
                f"router_disposition: {route_payload.get('disposition', '')}\n"
                f"lane_id: {route_payload.get('lane_id', '')}\n"
                "Publish visible prose only with: agent-orch assistant publish "
                f"--request-id publish:{dispatch_id} --composite-stream-id {route_stream_id} "
                f"--dispatch-id {dispatch_id} --reply-to-message-id {source_message_id} "
                "--publish-kind prose --message <text>\n"
                "For a bound lane's terminal report, use the existing agent-orch report --result "
                "JSON with extras.assistant_composite containing exactly stream_id (the composite), "
                "lane_id and dispatch_id from this task. Preserve the same report_id on retry. "
                "When commissioning the bound lead, pass these IDs with the original input; "
                "the lane binding delegates this dispatch without another operator message.\n"
                "Treat dispatch/lane state only as bounded evidence; server-side receipts remain "
                "authoritative for publication, operations and question consent.\n"
                "<assistant-backend-context>\n"
                f"{context_block}\n"
                "</assistant-backend-context>\n"
                "<assistant-original-input>\n"
                f"{body}\n"
                "</assistant-original-input>"
            ) + voice_answers_dispatch_block(await store.get_voice_answer_binding_for_input(
                stream_id=route_stream_id, input_identity=source_message_id,
            ))
        return await comms.send_assistant_backend({
            "host": host,
            "session_name": session_name,
            "text": wire_body,
            "attachments": attachments,
            "request_id": dispatch_id,
            "optimistic_id": dispatch_id,
            "from_stream_id": route_stream_id,
            "_assistant_expected_generation": str(route.get("route_target_generation") or "")
            if route_payload.get("admission_mode") == "direct_primary" else None,
        })

    async def _validate_voice_answers(binding, *, stream_id):
        # Notify is built after the composites; resolve it at call time.
        if server.notify is None:
            raise RuntimeError("voice answers validation unavailable")
        return await server.notify.validate_voice_answers(binding, stream_id=stream_id)

    assistant_composites: dict[str, AssistantComposite] = {}
    for _name, _cfg in assistant_configs.items():
        assistant_composites[_name] = AssistantComposite(
            store,
            config=_cfg,
            router=_assistant_router_for(_cfg),
            dispatch=_dispatch_assistant_route if _cfg.enabled else None,
            broadcast=server.broadcast,
            question_operation=server._assistant_question_operation,
            question_answer=server._assistant_question_answer,
            publication_attachments=server._assistant_publication_attachments,
            voice_answers_validate=_validate_voice_answers,
        )
    server.assistant_composites = assistant_composites
    # Primary (bart) alias: existing single-composite call sites (notify binding,
    # backend routing-integrity, watch-wake root, handler resolution fallbacks)
    # keep referring to bart byte-identically.
    assistant_composite = assistant_composites["bart"]
    assistant_config = assistant_configs["bart"]
    server.assistant_composite = assistant_composite

    async def _assistant_ingress_policy(*, target_stream_id, body, msg, verb):
        """Fan routine backend ingress across named assistants.

        Only the router-backed assistant participates in the authority-request
        model; a direct-primary assistant (daff) is a harmless no-op here.
        """
        from outbound_notices import ASSISTANT_AUTHORITY_REQUEST_TOKEN
        authority = msg.get("_assistant_authority_request_token") is ASSISTANT_AUTHORITY_REQUEST_TOKEN
        for _composite in assistant_composites.values():
            if authority and _composite.config.direct_primary:
                continue
            result = await _composite.suppress_routine_backend_ingress(
                target_stream_id=target_stream_id, body=body, msg=msg, verb=verb,
            )
            if result is not None:
                return result
        return None

    comms.assistant_ingress_policy = _assistant_ingress_policy
    comms.front_desk_digest = assistant_composite.front_desk_digest
    outbound.front_desk_digest = assistant_composite.front_desk_digest

    # Cosmo reply push: one Expo push to the scoped CosmoPushTokens audience when
    # a DAFF reply commits.  Wired to the Daff composite ONLY — Bart must never
    # push to the Cosmo audience (a bart-scoped token would otherwise receive
    # Bart reply text), so Bart's publish never invokes the hook at all.
    from cosmo_push import CosmoPush
    import uiverbs as _uiverbs_mod

    def _cosmo_push_table():
        import boto3
        return (boto3.resource("dynamodb", region_name=_uiverbs_mod.AWS_REGION)
                .Table(_uiverbs_mod.COSMO_PUSH_TOKENS_TABLE))

    _cosmo_push = CosmoPush(
        table_factory=_cosmo_push_table,
        registry=server.operator_credential_registry,
    )
    _wire_cosmo_reply_push(assistant_composites, _cosmo_push.push_reply)
    window_schedule = WindowSchedule(
        store, sessions, comms, spawnctl,
        local_host=args.local_host, broadcast=server.broadcast,
    )
    spawnctl.window_schedule = window_schedule
    server.window_schedule = window_schedule
    server.handlers.update(window_schedule.wire_handlers())
    # Every inventory-changing observer shares this one bounded emitter. The
    # existing mirror interval remains the sole cadence setting.
    mcfg = MirrorConfig.from_env()
    inventory_emitter = InventoryEmitter(
        sessions, server.broadcast, min_interval_s=mcfg.inventory_min_interval_s,
    )
    sessions.set_inventory_emitter(inventory_emitter)
    work_lanes = WorkLanesInventory(store, sessions, server.broadcast)
    server.work_lanes = work_lanes
    _session_emit = inventory_emitter.emit_if_changed

    async def _emit_sessions_and_lanes(*, immediate: bool = False) -> bool:
        changed = await _session_emit(immediate=immediate)
        work_lanes.refresh()
        return changed
    inventory_emitter.emit_if_changed = _emit_sessions_and_lanes
    work_lanes.start()
    server.lifecycle = lifecycle
    # The ledger announces `child_report_ready` on the server's broadcast, so it
    # is built after the server and attached back (design L13, both paths).
    server.ledger = Ledger(
        store,
        sessions=sessions,
        comms=comms,
        broadcast=server.broadcast,
        outbound=outbound,
        alerts=alerts,
    )

    # Lifted CRUD subsystems (v1_code_reuse_map): questions + notifications
    # (their own notifications.db, off-loop store thread) and blob transport
    # (content-addressed, off-loop). Both broadcast/inject through the server +
    # comms, so they too are built after the server and merged into the one
    # dispatch table. Any v1 verb left unregistered answers `unsupported_in_v2`.
    notify = Notify(
        args.notifications_db,
        outbound=outbound,
        comms=comms,
        broadcast=server.broadcast,
        sessions=sessions,
        notice_store=store,
        assistant_binding=assistant_composite.binding if assistant_composite.enabled else None,
        assistant_stream_id=assistant_composite.config.stream_id if assistant_composite.enabled else "",
    )
    server.notify = notify

    async def _work_lanes_changed() -> None:
        work_lanes.refresh()
    for _composite in assistant_composites.values():
        _composite.work_lane_confirmation_reader = notify.work_lane_confirmation
        _composite.work_lanes_changed = _work_lanes_changed
    notify.consent_snapshot = server._consent_notifications_for_msg
    spawnctl.consent_notify = notify
    server.handlers.update(notify.wire_handlers())

    # D3 (daemon_updates_2026_09): a producer's confirmed-dead close or
    # replacement must also expire the open questions it asked, terminalizing the
    # paired notification + agent_question through the shared path so a departed
    # asker cannot leave an actionable question behind. Compose the notify
    # question-expiry with the ledger's existing await resolver on the single
    # session-close hook, invoking BOTH so a failing callback cannot skip the
    # other. The session-lifecycle lock is held by the caller before this fires,
    # keeping the session-lifecycle-before-notification-store order.
    _ledger_close_resolver = server.ledger.resolve_awaiters_on_close

    async def _on_producer_close(
        stream_id: str, *, session_generation: str | None = None,
        reason: str = "confirmed_dead_close",
    ) -> Any:
        results = None
        try:
            results = await _ledger_close_resolver(
                stream_id, session_generation=session_generation, reason=reason,
            )
        except Exception:  # noqa: BLE001 - one failing callback cannot skip the other
            log.exception("close resolver (awaiters) failed stream=%s", stream_id)
        try:
            await notify.expire_questions_for_closed_producer(
                stream_id, generation=session_generation,
            )
        except Exception:  # noqa: BLE001 - close truth is already durable
            log.exception("close resolver (question expiry) failed stream=%s", stream_id)
        for _composite in assistant_composites.values():
            try:
                await _composite.target_closed(stream_id, session_generation)
            except Exception:  # noqa: BLE001 - close truth is already durable
                log.exception("close resolver (assistant route) failed stream=%s", stream_id)
        return results

    sessions.set_awaiter_resolver(_on_producer_close)
    routing_integrity = RoutingIntegrity(
        store,
        sessions,
        notify=notify,
        broadcast=server.broadcast,
        inventory_emitter=inventory_emitter,
        assistant_backend_binding=(
            lambda stream_id, generation: assistant_composite.config.enabled and bool(generation) and stream_id in {
                assistant_composite.config.astra_stream_id,
                assistant_composite.config.luna_stream_id,
            }
        ),
    )
    server.handlers.update(blobs.wire_handlers())
    # The blob store owns partial upload state; the server tears a closed
    # connection's in-flight uploads down through this reference.
    server.blobs = blobs
    assets = Assets(args.assets_db, sessions=sessions, comms=comms, broadcast=server.broadcast)
    server.handlers.update(assets.wire_handlers())
    # UI-sent verb group (mobile/desktop day-1): models/grant_token/register_push
    # lifted clean; send.interrupt/close.cancel/question.dismiss/enroll/specs.*
    # degrade honestly where their subsystem is not in v2 yet. No store to open.
    uiverbs = UIVerbs(store, sessions, spawnctl, specs=specs)
    server.handlers.update(uiverbs.wire_handlers())
    # Presence remains observation-only; the durable reconciler owns the
    # thresholded state transition and the read-only reconcile.status verb.
    presence = None
    if not args.disable_remote_presence:
        presence = RemotePresence(
            sessions, hosts, config=presence_config, broadcast=server.broadcast,
            inventory_emitter=inventory_emitter,
        )
        server.working_state_trackers.append(presence.tracker)
    # Host and session liveness share one projection: every breaker flip emits
    # host.status and applies the same state to UI-visible inventory rows.
    host_status_change_lock = asyncio.Lock()

    async def _on_host_status_change(payload: dict[str, object]) -> None:
        async with host_status_change_lock:
            host = str(payload.get("host") or "")
            current = hosts.snapshot().get(host, {})
            if (
                bool(payload.get("online")) != bool(current.get("online"))
                or str(payload.get("host_status_reason") or "")
                != str(current.get("host_status_reason") or "")
            ):
                return
            await server.broadcast({"type": "host.status", **payload})
            if presence is not None:
                await presence.host_status_changed(payload)

    hosts._on_status_change = _on_host_status_change
    # `event.push`: per-host satellite ingest sink (satellite.py tails remote
    # transcripts and pushes here). Reuses the same append_session_event floor +
    # broadcast-iff-inserted as local ingest.
    # Usage provenance (metadata only): satellites via event.push and local
    # ingest share one sink; Codex rate-limit lines land beside sessions.db.
    provenance_sink = ProvenanceSink(
        store.record_provenance,
        HistoryLog(Path(args.db).with_name(HISTORY_FILENAME)) if args.db != ":memory:" else None,
    )
    event_push = EventPush(
        store, server.broadcast, alerts, recent_limit=RECENT_LIMIT,
        enabled=not args.disable_event_push_ingest,
        routing_integrity=routing_integrity,
        presence=presence,
        sessions=sessions,
        inventory_emitter=inventory_emitter,
        host_stats_handler=server.merge_host_stats,
        provenance=provenance_sink,
    )
    server.handlers.update(event_push.wire_handlers())
    from watch_wake import WatchWake, run_reconcile_callbacks
    watch_wake = WatchWake(
        store, sessions, outbound,
        root_binding=lambda: (
            (assistant_composite.config.direct_primary_stream_id,
             assistant_composite.config.direct_primary_generation)
            if assistant_composite.config.direct_primary else None
        ),
    )
    server.watch_wake = watch_wake
    server.handlers.update(watch_wake.wire_handlers())

    async def reconcile_callbacks():
        # The alarm runs before tick so a late wake fired in this pass still alerts.
        await run_reconcile_callbacks(event_push.check_pin_drift, watch_wake.missed_wake_alarm, watch_wake.tick)

    reconciler = SessionReconciler(
        sessions,
        hosts,
        presence=presence,
        alerts=alerts,
        notify=notify,
        comms=comms,
        config=ReconcileConfig.from_env(),
        outbound=outbound,
        spawnctl=spawnctl,
        on_reconcile_tick=reconcile_callbacks,
    )
    server.reconciler = reconciler

    # Daff seat recovery: daemon-owned respawn of the Daff assistant only.  Bart
    # is never recovered (the hook declines non-Daff rows).  Enabled only when
    # the daff composite is configured.
    if assistant_composites.get("daff") is not None and assistant_composites["daff"].config.enabled:
        import uuid as _uuid
        from recovery import DaffRecovery

        async def _tell_bart_degraded(text: str) -> None:
            bart = assistant_composites.get("bart")
            if bart is None:
                return
            tell_id = f"daff-degraded-{_uuid.uuid4().hex}"
            delivered = await server._deliver_composite_tell(
                bart, bart.config.stream_id, "daemon:recovery", text, tell_id, "",
            )
            if delivered is None:
                await store.enqueue_composite_tell(
                    name=bart.config.name, tell_id=tell_id,
                    from_stream_id="daemon:recovery", body=text, request_id="",
                )

        daff_recovery = DaffRecovery(
            sessions=sessions, spawnctl=spawnctl, store=store, local_host=args.local_host,
            composites=lambda: assistant_composites,
            flush_composite_tells=server._flush_composite_tells,
            tell_bart=_tell_bart_degraded,
            startup_prompt=os.environ.get("PENTACLE_ASSISTANT_DAFF_STARTUP_PROMPT", "")
            or "You are Daff, the operator's always-on assistant. Read your startup context and resume.",
        )
        reconciler.on_protected_dead = daff_recovery.on_dead
        server.daff_recovery = daff_recovery

    usage_state_path = (
        Path(args.db).with_name("usage_state.json")
        if args.db != ":memory:"
        else None
    )
    limits_publisher = UsageStatePublisher(server.broadcast, state_path=usage_state_path)
    server.limits = limits_publisher

    # Close the spawn gate BEFORE binding: from the instant the port accepts,
    # a spawn could arrive, and it must wait for reconciliation rather than race
    # it (QA #18). Every other verb still serves immediately once bound.
    server.spawn_ready.clear()
    # Same for the inventory: `list_sessions` waits out the boot adoption window
    # (below) rather than serve an empty inventory that would blank the UI of a
    # restarted daemon's live sessions (B10). Set once `refresh()` returns.
    server.inventory_ready.clear()

    # 1. BIND FIRST. Nothing above this line may block on I/O.
    port = await server.bind()
    print(f"chat_streamd_v2 listening on {binds[0]}:{port}", flush=True)
    # Install the shutdown handler immediately after bind, BEFORE the blocking
    # init below. A SIGTERM during boot must trigger the graceful path (set the
    # stop future), not the default disposition that kills the process (rc=-15);
    # boot still finishes, then `await stop` returns at once and shutdown runs.
    stop = asyncio.get_running_loop().create_future()

    def _request_stop(*_a: object) -> None:
        if not stop.done():
            stop.set_result(None)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, _request_stop)
        except NotImplementedError:  # pragma: no cover - non-POSIX
            signal.signal(sig, _request_stop)

    # 2. Init that may block goes after the accept loop is live. The lifted CRUD
    #    stores open their own DBs (PRAGMA integrity_check, WAL) + blob dirs off
    #    the loop; they start CONCURRENTLY with inventory adoption so a tier-0
    #    `prompt.*` verb is ready in ~ms rather than behind the tmux reconcile.
    #    Verbs arriving before their store is up park on a bounded readiness gate
    #    (never error); `hello`'s snapshot tolerates an unopened store.
    store.start()
    for _composite in assistant_composites.values():
        if _composite.config.enabled:
            await _composite.load_binding()
    if store.schedule_schema_health == "ok":
        window_schedule.mark_store_ready()
    else:
        window_schedule.mark_store_failed()
    await lifecycle.start()
    for _name, _composite in assistant_composites.items():
        if not _composite.config.enabled:
            continue
        projection = await _composite.ensure_projection()
        recovery = await _composite.recover()
        log.info(
            "assistant composite projection ready name=%s stream=%s generation=%s recovery=%s",
            _name,
            projection.get("stream_id") if projection else "",
            projection.get("session_generation") if projection else "",
            recovery,
        )
    # B10 re-adoption FIRST and uncontended: the inventory is rebuilt from the
    # persisted open rows before anything else competes for the loop, so a
    # restarted daemon serves `list_sessions` with its live sessions promptly.
    # Panes are never touched here — a restart must cause zero session deaths.
    adopted = await sessions.refresh()
    # Establish the dedup baseline only after boot adoption. Priming when _inv
    # is empty would turn the first periodic no-op into a spurious snapshot.
    inventory_emitter.prime()
    log.info("adopted %d open session(s)", adopted)
    if not args.disable_awaiter_resolution:
        startup_awaiter_result = await server.ledger.sweep_awaited_unreported(
            limit=AwaiterResolutionConfig.from_env().max_per_pass,
        )
        log.info("awaiter resolution startup sweep: %s", startup_awaiter_result)
    # Inventory is populated from persisted rows: `list_sessions` may serve.
    server.inventory_ready.set()
    # Keep the bind-first startup rule: git I/O happens only after the accept
    # loop, durable store, and initial inventory are live, so boot-SHA capture
    # cannot widen an existing readiness window. The value is captured once and
    # later compared read-only against the deploy-owned kv target on verdicts
    # and reconciler passes.
    event_push.set_daemon_sha(await asyncio.to_thread(_resolve_runtime_sha))
    server.runtime_sha = event_push.daemon_sha
    if event_push.daemon_sha:
        log.info("event.push boot daemon SHA: %s", event_push.daemon_sha)
    else:
        log.warning("event.push could not read boot daemon SHA; pin drift is unavailable")
    # The lifted CRUD stores then open concurrently with the tmux reconcile.
    lifted = asyncio.gather(notify.start(), blobs.start(), assets.start())
    # A spawn interrupted between `tmux new-session` and the row write left a
    # live pane with no row. Settle those intents now that the inventory is
    # loaded: adopt the ones whose pane survived, release the rest, and roll
    # back only an ownership-fenced pane whose session-row registration fails.
    log.info("spawn intents: %s", await spawnctl.reconcile_spawn_intents())
    await run_reconcile_callbacks(watch_wake.missed_wake_alarm)
    await watch_wake.tick()
    # Reconciliation has settled the inventory and every interrupted intent, so
    # a new spawn can no longer race it: open the spawn gate (QA #18).
    server.spawn_ready.set()
    if assistant_config.enabled and server.lane_rulings is not None:
        await server.lane_rulings.start()
    await lifted  # both are fast; ensure done before the background tasks below

    # 3. Background tasks start last, each under the loop rules
    #    (cadence, per-pass cap, backoff, kill switch).
    tasks: list[asyncio.Task] = []
    try:
        tasks.append(await server.start_consent())
    except (OSError, ValueError) as exc:
        log.error("consent initialization failed: %s", type(exc).__name__,
                  extra={"subsystem": "consent", "bug_ref": "mobile_faceid_privileged_consent_2026_09"})
    tasks.append(asyncio.create_task(
        _run_machine_stats(server, args.local_host), name="machine-stats"))
    if not args.disable_window_schedule and window_schedule.schema_health == "ok":
        tasks.append(asyncio.create_task(window_schedule.run_forever(), name="window-schedule"))
    if not args.disable_outbound_notices:
        ocfg = outbound.config
        tasks.append(asyncio.create_task(outbound.run_forever(), name="outbound-notices"))
        log.info(
            "outbound-notices: every %.1fs, cap %d notices/pass, lease %.1fs, max attempts %d",
            NOTICE_INTERVAL_S,
            NOTICE_MAX_PER_PASS,
            ocfg.lease_s,
            ocfg.max_attempts,
        )
    if not args.disable_awaiter_resolution:
        acfg = AwaiterResolutionConfig.from_env()
        tasks.append(asyncio.create_task(
            AwaiterResolutionJob(server.ledger, acfg).run_forever(),
            name="awaiter-resolution",
        ))
        log.info(
            "awaiter-resolution: every %.0fs, cap %d rows/pass",
            acfg.interval_s, acfg.max_per_pass,
        )
    if not args.disable_retention:
        cfg = RetentionConfig.from_env()
        cfg.blob_root = Path(args.blob_root)
        tasks.append(asyncio.create_task(RetentionJob(store, cfg).run_forever(), name="retention"))
        log.info("retention: every %.0fs, cap %d rows/pass", cfg.interval_s, cfg.max_rows_per_pass)
    # Structured transcript ingest owns local transcript event persistence and
    # activity.
    ingest = None
    if not args.disable_ingest:
        icfg = IngestConfig.from_env()
        ingest = Ingest(store, sessions, tmux, server.broadcast,
                        local_host=args.local_host, recent_limit=RECENT_LIMIT, config=icfg,
                        routing_integrity=routing_integrity,
                        inventory_emitter=inventory_emitter,
                        provenance=provenance_sink)
        tasks.append(asyncio.create_task(ingest.run_forever(), name="ingest"))
        log.info("ingest: every %.1fs, cap %d events/pass", icfg.interval_s, icfg.max_events_per_pass)

    mirror = None
    if not args.disable_mirror:
        mirror = Mirror(
            store, sessions, tmux, server.broadcast, local_host=args.local_host,
            config=mcfg, inventory_emitter=inventory_emitter,
        )
        tasks.append(asyncio.create_task(mirror.run_forever(), name="mirror"))
        log.info("mirror: every %.1fs, one batched liveness pass", mcfg.interval_s)

    # The host probe pool: bounded workers, per-host timeout + circuit breaker,
    # cadence, backoff, kill switch (`--disable-hosts`). An offline peer never
    # stalls the loop — each probe is a bounded subprocess behind a semaphore.
    if hosts.peers and not args.disable_hosts:
        tasks.append(asyncio.create_task(hosts.run_forever(), name="hosts"))
        log.info("hosts: %d peer(s), probe every %.0fs, breaker after %d fails",
                 len(hosts.peers), hosts.cfg.interval_s, hosts.cfg.breaker_threshold)
    server.ledger.routing_integrity = routing_integrity

    # Durable row↔presence reconciliation. The pass includes the remote
    # presence observation, so a reachable peer with no tmux server is distinct
    # from SSH-unreachable and only the former can cross the death threshold.
    if not args.disable_reconciler:
        rcfg = reconciler.cfg
        tasks.append(asyncio.create_task(reconciler.run_forever(), name="reconciler"))
        log.info("reconciler: every %.0fs, cap %d rows/pass, threshold %d checks",
                 rcfg.interval_s, rcfg.max_rows_per_pass, rcfg.threshold_checks)
        # Fast working-state refresh: the reconcile sweep is the sole working
        # owner but runs only every ~60s, so a short turn never surfaces. This
        # loop drives that same owner for hot streams (an in-flight turn) on a
        # tight cadence. Kill switch: PENTACLE_WORKING_REFRESH_INTERVAL_S<=0.
        if presence is not None and presence.cfg.working_refresh_interval_s > 0:
            tasks.append(asyncio.create_task(
                presence.working_refresh_loop(), name="working-refresh"))
            log.info("working-refresh: every %.1fs, hot streams only (active<=%.0fs)",
                     presence.cfg.working_refresh_interval_s, presence.cfg.working_active_ttl_s)
    if not args.disable_notification_expiry:
        expiry = NotificationExpiry(notify, interval_s=args.notification_expiry_interval_s)
        tasks.append(asyncio.create_task(expiry.run_forever(), name="notification-expiry"))
    # One bounded nudge cadence: context crossings include hidden children;
    # title/card reminders retain their visible top-level filter. Notify owns
    # the parentless operator route; the existing kill switch disables both.
    if not args.disable_nudges:
        nudge_cfg = NudgeConfig.from_env()
        tasks.append(asyncio.create_task(
            NudgeJob(
                sessions,
                comms,
                store,
                nudge_cfg,
                notify=notify,
                outbound=outbound,
                broadcast=server.broadcast,
            ).run_forever(), name="nudges"))
        log.info("nudges: every %.0fs, cap %d/pass, cooldown %.0fs",
                 nudge_cfg.interval_s, nudge_cfg.max_per_pass, nudge_cfg.cooldown_s)
    # Usage limits: watch the externally collected usage_state.json and publish
    # limits.update on change (no probing, no on-loop fsync — see usage_publisher).
    if not args.disable_usage_state_publisher:
        tasks.append(asyncio.create_task(limits_publisher.run_forever(), name="usage-state-publisher"))

    await stop
    log.info("shutdown requested")

    # Bounded, cancellation-aware shutdown (B14). Never touches panes (B10).
    # Background tasks stop before the store does, so none is mid-submit when
    # the worker thread goes away.
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.wait(tasks, timeout=5)
    for _composite in assistant_composites.values():
        await _composite.stop()
    if server.lane_rulings is not None:
        await server.lane_rulings.stop()
    await server.close()
    await notify.stop()
    await assets.stop()
    await lifecycle.stop(reason="signal")
    store.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(getattr(logging, str(args.log_level).upper(), logging.INFO))
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
