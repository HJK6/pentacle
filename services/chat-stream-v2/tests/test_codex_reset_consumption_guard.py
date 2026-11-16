"""Reset-consumption guard regressions.

These tests distinguish a reset interstitial from passive availability text in
scrollback. The interstitial is operator-only; a normal composer remains usable
without invoking the guarded ``/usage`` action.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import boot_ready  # noqa: E402
from comms import Comms, is_codex_usage_command  # noqa: E402
from sessions import Sessions, VerbError  # noqa: E402
from spawnctl import SpawnCtl  # noqa: E402
from store import Store  # noqa: E402


HOST = "localhost"
NAME = "reset-guard-target"
RESET_AVAILABILITY = "You have 3 usage limit resets available. Run /usage to use one."
RESET_INTERSTITIAL = (
    "View account usage or redeem an earned reset.\n"
    "› Show usage\n"
    "  Redeem usage limit reset"
)
RESET_CONFIRMATION = "Use this reset?\n› Yes, use reset\n  No, go back"
CODEX_CHROME = "OpenAI Codex\n─────────\n› \n  gpt-5-codex high"


@pytest.mark.parametrize("body", ["/usage", " /USAGE --reset ", "/usage\nmore text"])
def test_usage_command_guard_covers_the_first_command_token(body: str) -> None:
    assert is_codex_usage_command(body) is True


def test_usage_word_inside_ordinary_text_is_not_a_command() -> None:
    assert is_codex_usage_command("explain /usage without running it") is False


def test_reset_availability_alone_is_not_an_interstitial() -> None:
    assert boot_ready.codex_reset_interstitial_visible(RESET_AVAILABILITY) is False
    assert boot_ready.codex_tui_readiness(RESET_AVAILABILITY) == "not_ready"
    assert boot_ready.codex_tui_ready(RESET_AVAILABILITY) is False


def test_reset_availability_text_with_normal_prompt_chrome_is_usable() -> None:
    pane = f"{CODEX_CHROME}\n{RESET_AVAILABILITY}"
    assert boot_ready.codex_reset_interstitial_visible(pane) is False
    assert boot_ready.codex_tui_readiness(pane) == "ready"
    assert boot_ready.codex_tui_ready(pane) is True


def test_reset_availability_during_active_turn_is_not_reset_blocked() -> None:
    pane = (
        "OpenAI Codex\n"
        "› perform ordinary work\n"
        f"• {RESET_AVAILABILITY}\n"
        "Working (2s • esc to interrupt)\n"
        "gpt-5-codex high"
    )
    assert boot_ready.codex_reset_interstitial_visible(pane) is False
    assert boot_ready.codex_tui_readiness(pane) == "not_ready"
    assert boot_ready.codex_tui_ready(pane) is False
    assert boot_ready.codex_tui_session_visible(pane) is True


def test_reset_menu_is_terminally_blocked_even_with_old_prompt_chrome() -> None:
    pane = f"{CODEX_CHROME}\n{RESET_INTERSTITIAL}"
    assert boot_ready.codex_reset_interstitial_visible(pane) is True
    assert boot_ready.codex_tui_readiness(pane) == "reset_blocked"
    assert boot_ready.codex_tui_ready(pane) is False


def test_resolved_reset_menu_in_scrollback_does_not_reblock_new_composer() -> None:
    pane = f"{RESET_INTERSTITIAL}\n{CODEX_CHROME}"
    assert boot_ready.codex_reset_interstitial_visible(pane) is False
    assert boot_ready.codex_tui_readiness(pane) == "ready"
    assert boot_ready.codex_tui_ready(pane) is True


def test_reset_confirmation_is_terminally_blocked() -> None:
    pane = f"{CODEX_CHROME}\n{RESET_CONFIRMATION}"
    assert boot_ready.codex_reset_interstitial_visible(pane) is True
    assert boot_ready.codex_tui_readiness(pane) == "reset_blocked"
    assert boot_ready.codex_tui_ready(pane) is False


def test_consumed_reset_confirmation_is_not_an_offer() -> None:
    pane = "Usage reset. You have 2 usage limit resets left.\n" + CODEX_CHROME
    assert boot_ready.codex_reset_interstitial_visible(pane) is False
    assert boot_ready.codex_tui_ready(pane) is True


class ResetPaneTmux:
    """A live Codex pane that must never receive automated input."""

    def __init__(self, pane: str | None = None) -> None:
        self.alive = False
        self.pane = pane or RESET_INTERSTITIAL
        self.pastes: list[str] = []
        self.enters = 0
        self.runs: list[tuple[object, ...]] = []
        self.kills = 0
        self.nonce = ""

    async def capture(self, _name: str) -> str:
        return self.pane

    async def session_state(self, _name: str) -> str:
        return "alive" if self.alive else "gone"

    async def has_session(self, _name: str) -> bool:
        return self.alive

    async def new_session(
        self, _name: str, _command: str, **kwargs: object,
    ) -> None:
        self.alive = True
        env = kwargs.get("env")
        if isinstance(env, dict):
            self.nonce = str(env.get("EXAMPLE_SPAWN_NONCE") or "")

    async def pane_pid(self, _name: str) -> str:
        return ""

    async def paste(self, _name: str, text: str) -> None:
        self.pastes.append(text)
        raise AssertionError("reset-blocked pane received a paste")

    async def send_enter(self, _name: str) -> None:
        self.enters += 1
        raise AssertionError("reset-blocked pane received Enter")

    async def kill_session(self, _name: str) -> None:
        self.kills += 1
        raise AssertionError("reset-blocked pane was killed")

    async def run(self, *args: object, **_kwargs: object) -> tuple[int, str]:
        self.runs.append(args)
        if args and args[0] == "show-environment" and self.nonce:
            return 0, f"EXAMPLE_SPAWN_NONCE={self.nonce}\n"
        return 0, ""


def test_provider_ready_returns_typed_reset_block_without_input() -> None:
    async def run() -> tuple[object, ResetPaneTmux]:
        tmux = ResetPaneTmux()
        store = Store(":memory:")
        ctl = SpawnCtl(store, Sessions(store, tmux=tmux, local_host=HOST), tmux=tmux)
        outcome = await ctl._await_provider_ready(NAME, "codex", tmux)
        return outcome, tmux

    outcome, tmux = asyncio.run(run())
    assert outcome.ready is False
    assert outcome.reason == "reset_blocked"
    assert tmux.pastes == []
    assert tmux.enters == 0
    assert tmux.runs == []


def test_provider_ready_ignores_reset_availability_behind_normal_composer() -> None:
    async def run() -> tuple[object, ResetPaneTmux]:
        tmux = ResetPaneTmux(f"{RESET_AVAILABILITY}\n{CODEX_CHROME}")
        store = Store(":memory:")
        ctl = SpawnCtl(store, Sessions(store, tmux=tmux, local_host=HOST), tmux=tmux)
        outcome = await ctl._await_provider_ready(NAME, "codex", tmux)
        return outcome, tmux

    outcome, tmux = asyncio.run(run())
    assert outcome.ready is True
    assert outcome.reason is None
    assert tmux.pastes == []
    assert tmux.enters == 0


def test_spawn_reset_block_preserves_live_pane_and_typed_durable_state() -> None:
    async def run() -> tuple[VerbError, dict, dict, list[dict], ResetPaneTmux]:
        store = Store(":memory:")
        store.start()
        tmux = ResetPaneTmux(RESET_INTERSTITIAL)
        sessions = Sessions(store, tmux=tmux, local_host=HOST)
        ctl = SpawnCtl(store, sessions, tmux=tmux)
        try:
            with pytest.raises(VerbError) as raised:
                await ctl.spawn({"objective": "Exercise the existing spawn contract",
                    "command": "codex",
                    "provider": "codex",
                    "session_name": NAME,
                    "request_id": "spawn-reset-guard",
                    "initial_prompt": "must not be pasted",
                }, HOST)
            row = await store.fetch_session(HOST, NAME)
            outcome = await store.get_spawn_outcome(HOST, NAME)
            reservations = await store.reservations(include_expired=True)
            assert row is not None
            assert outcome is not None
            return raised.value, row, outcome, reservations, tmux
        finally:
            store.stop()

    error, row, outcome, reservations, tmux = asyncio.run(run())
    assert error.code == "reset_blocked"
    assert error.extra["readiness_reason"] == "reset_blocked"
    assert row["status"] == "open"
    assert row["pane_status"] == "pane_alive"
    assert row["bootstrap_state"] == "reset_blocked"
    assert outcome["state"] == "failed"
    assert outcome["reason"] == "reset_blocked"
    assert outcome["delivery_evidence"] == "reset_blocked"
    assert reservations and json.loads(reservations[0]["payload"])["readiness_state"] == "reset_blocked"
    assert tmux.pastes == []
    assert tmux.enters == 0
    assert tmux.kills == 0


class SendTmux:
    def __init__(self) -> None:
        self.screen = CODEX_CHROME
        self.captures = 0
        self.pastes: list[str] = []
        self.enters = 0
        self.runs: list[tuple[object, ...]] = []

    async def capture(self, _name: str) -> str:
        self.captures += 1
        return self.screen

    async def paste(self, _name: str, text: str) -> None:
        self.pastes.append(text)
        self.screen = f"OpenAI Codex\n• {text}\n› \n  gpt-5-codex high"

    async def send_enter(self, _name: str) -> None:
        self.enters += 1

    async def run(self, *args: object, **_kwargs: object) -> tuple[int, str]:
        self.runs.append(args)
        return 0, ""


def _new_send_comms(tmux: SendTmux) -> tuple[Comms, Store, Sessions]:
    store = Store(":memory:")
    store.start()
    sessions = Sessions(store, tmux=tmux, local_host=HOST)
    ctl = SpawnCtl(store, sessions, tmux=tmux)
    return Comms(store, sessions, ctl), store, sessions


def test_agent_usage_send_is_nonretryable_and_pre_tmux_with_provenance() -> None:
    async def run() -> tuple[VerbError, SendTmux, dict | None]:
        tmux = SendTmux()
        comms, store, sessions = _new_send_comms(tmux)
        try:
            await sessions.open(HOST, NAME, provider="codex")
            with pytest.raises(VerbError) as raised:
                await comms.send({
                    "stream_id": f"{HOST}:{NAME}",
                    "message": "  /USAGE  \n",
                    "request_id": "send-agent-usage",
                    "from_stream_id": "hosta:agent",
                })
            receipt = await store.get_send_receipt(f"{HOST}:{NAME}", "send-agent-usage")
            return raised.value, tmux, receipt
        finally:
            store.stop()

    error, tmux, receipt = asyncio.run(run())
    assert error.code == "reset_blocked"
    assert error.extra["retryable"] is False
    assert error.extra["nonretryable"] is True
    assert error.extra["readiness_reason"] == "reset_blocked"
    assert tmux.captures == 0
    assert tmux.pastes == []
    assert tmux.enters == 0
    assert receipt is not None
    assert receipt["reason"] == "reset_blocked"
    assert receipt["from_stream_id"] == "hosta:agent"
    assert receipt["actor_stream_id"] == "hosta:agent"
    assert receipt["actor_trusted"] is False


def test_ordinary_codex_send_remains_allowed_and_records_provenance() -> None:
    async def run() -> tuple[dict, dict | None, SendTmux]:
        tmux = SendTmux()
        comms, store, sessions = _new_send_comms(tmux)
        try:
            await sessions.open(HOST, NAME, provider="codex")
            result = await comms.send({
                "stream_id": f"{HOST}:{NAME}",
                "message": "ordinary work",
                "request_id": "send-ordinary",
                "from_stream_id": "hosta:agent",
            })
            return result, await store.get_send_receipt(f"{HOST}:{NAME}", "send-ordinary"), tmux
        finally:
            store.stop()

    result, receipt, tmux = asyncio.run(run())
    assert result["delivery"] == "landed"
    assert tmux.pastes == ["ordinary work"]
    assert receipt is not None
    assert receipt["from_stream_id"] == "hosta:agent"
    assert receipt["actor_stream_id"] == "hosta:agent"
    assert receipt["actor_trusted"] is False


def test_ordinary_send_reconciles_stale_reset_block_at_normal_composer() -> None:
    async def run() -> tuple[dict, dict, SendTmux]:
        tmux = SendTmux()
        comms, store, sessions = _new_send_comms(tmux)
        try:
            await sessions.open(
                HOST,
                NAME,
                provider="codex",
                bootstrap_state="reset_blocked",
            )
            result = await comms.send({
                "stream_id": f"{HOST}:{NAME}",
                "message": "ordinary work without consuming a reset",
                "request_id": "send-stale-reset-state",
                "from_stream_id": "hosta:agent",
            })
            return result, await store.fetch_session(HOST, NAME) or {}, tmux
        finally:
            store.stop()

    result, row, tmux = asyncio.run(run())
    assert result["delivery"] == "landed"
    assert row["bootstrap_state"] == "started"
    assert tmux.pastes == ["ordinary work without consuming a reset"]
    assert "/usage" not in tmux.pastes


def test_ordinary_send_reconciles_stale_reset_block_during_active_turn() -> None:
    async def run() -> tuple[dict, dict, SendTmux]:
        tmux = SendTmux()
        tmux.screen = (
            "OpenAI Codex\n"
            "› existing work\n"
            f"• {RESET_AVAILABILITY}\n"
            "Working (2s • esc to interrupt)\n"
            "gpt-5-codex high"
        )
        comms, store, sessions = _new_send_comms(tmux)
        try:
            await sessions.open(
                HOST, NAME, provider="codex", bootstrap_state="reset_blocked",
            )
            result = await comms.send({
                "stream_id": f"{HOST}:{NAME}",
                "message": "queue ordinary work without consuming a reset",
                "request_id": "send-stale-reset-busy",
                "from_stream_id": "hosta:agent",
            })
            return result, await store.fetch_session(HOST, NAME) or {}, tmux
        finally:
            store.stop()

    result, row, tmux = asyncio.run(run())
    assert result["delivery"] == "committed_pending_proof"
    assert row["bootstrap_state"] == "started"
    assert tmux.pastes == ["queue ordinary work without consuming a reset"]


def test_agent_send_to_reset_interstitial_never_reaches_input() -> None:
    async def run() -> tuple[VerbError, SendTmux]:
        tmux = SendTmux()
        tmux.screen = RESET_INTERSTITIAL
        comms, store, sessions = _new_send_comms(tmux)
        try:
            await sessions.open(
                HOST, NAME, provider="codex", bootstrap_state="reset_blocked",
            )
            with pytest.raises(VerbError) as raised:
                await comms.send({
                    "stream_id": f"{HOST}:{NAME}",
                    "message": "ordinary input must wait for the composer",
                    "request_id": "send-reset-interstitial",
                    "from_stream_id": "hosta:agent",
                })
            return raised.value, tmux
        finally:
            store.stop()

    error, tmux = asyncio.run(run())
    assert error.code == "reset_blocked"
    assert tmux.captures == 1
    assert tmux.pastes == []
    assert tmux.enters == 0


def test_live_reset_menu_blocks_tell_and_persists_state_before_paste() -> None:
    async def run() -> tuple[VerbError, dict, SendTmux]:
        tmux = SendTmux()
        tmux.screen = RESET_INTERSTITIAL
        comms, store, sessions = _new_send_comms(tmux)
        try:
            await sessions.open(HOST, NAME, provider="codex")
            with pytest.raises(VerbError) as raised:
                await comms.tell({
                    "stream_id": f"{HOST}:{NAME}",
                    "message": "must not select a reset-menu choice",
                    "tell_id": "tell-live-reset-menu",
                    "from_stream_id": "hosta:agent",
                })
            return raised.value, await store.fetch_session(HOST, NAME) or {}, tmux
        finally:
            store.stop()

    error, row, tmux = asyncio.run(run())
    assert error.code == "reset_blocked"
    assert error.extra["phase"] == "not_started"
    assert row["bootstrap_state"] == "reset_blocked"
    assert tmux.pastes == []
    assert tmux.enters == 0


def test_live_reset_menu_returns_send_not_landed_before_paste() -> None:
    async def run() -> tuple[dict, dict, SendTmux]:
        tmux = SendTmux()
        tmux.screen = RESET_INTERSTITIAL
        comms, store, sessions = _new_send_comms(tmux)
        try:
            await sessions.open(HOST, NAME, provider="codex")
            result = await comms.send({
                "stream_id": f"{HOST}:{NAME}",
                "message": "must not select a reset-menu choice",
                "request_id": "send-live-reset-menu",
                "from_stream_id": "hosta:agent",
            })
            return result, await store.fetch_session(HOST, NAME) or {}, tmux
        finally:
            store.stop()

    result, row, tmux = asyncio.run(run())
    assert result["delivery"] == "not_landed"
    assert result["reason"] == "reset_blocked"
    assert row["bootstrap_state"] == "reset_blocked"
    assert tmux.pastes == []
    assert tmux.enters == 0


def test_tell_while_codex_initial_submission_unproven_is_pending_nonretryable_and_unmerged() -> None:
    async def run() -> tuple[VerbError, SendTmux, dict | None]:
        tmux = SendTmux()
        comms, store, sessions = _new_send_comms(tmux)
        try:
            await sessions.open(HOST, NAME, provider="codex", bootstrap_state="unproven")
            with pytest.raises(VerbError) as raised:
                await comms.tell({
                    "stream_id": f"{HOST}:{NAME}",
                    "message": "later tell must not merge into the staged pointer",
                    "tell_id": "tell-before-bootstrap-proof",
                    "from_stream_id": "hosta:agent",
                })
            return raised.value, tmux, await store.get_tell_delivery(
                "tell-before-bootstrap-proof"
            )
        finally:
            store.stop()

    error, tmux, tell_row = asyncio.run(run())
    assert error.code == "initial_prompt_pending"
    assert error.extra["pending"] is True
    assert error.extra["nonretryable"] is True
    assert error.extra["confirmation_pending"] is False
    assert error.extra["action_committed"] is False
    assert tmux.captures == 0
    assert tmux.pastes == []
    assert tmux.enters == 0
    assert tell_row is None


def test_verified_operator_usage_is_the_only_reset_override_and_records_trust() -> None:
    async def run() -> tuple[dict, dict | None, SendTmux]:
        tmux = SendTmux()
        comms, store, sessions = _new_send_comms(tmux)
        try:
            await sessions.open(
                HOST, NAME, provider="codex", bootstrap_state="reset_blocked",
            )
            result = await comms.send({
                "stream_id": f"{HOST}:{NAME}",
                "message": " /USAGE ",
                "request_id": "operator-usage-reset",
                "_auth_context": {
                    "operator_authenticated": True,
                    "operator_principal": "operator:test",
                    "connection_client": "example-client",
                },
            })
            receipt = await store.get_send_receipt(
                f"{HOST}:{NAME}", "operator-usage-reset",
            )
            return result, receipt, tmux
        finally:
            store.stop()

    result, receipt, tmux = asyncio.run(run())
    assert result["delivery"] == "landed"
    assert tmux.pastes == [" /USAGE "]
    assert receipt is not None
    assert receipt["actor_stream_id"] == "operator:test"
    assert receipt["actor_trusted"] is True


def test_reset_usage_rejection_does_not_probe_a_peer_host() -> None:
    class Hosts:
        peers = {"hostb": object()}

        def __init__(self) -> None:
            self.ensure_calls = 0

        def is_local(self, host: str) -> bool:
            return host == "hosta"

        async def ensure_reachable(self, _host: str, _verb: str) -> None:
            self.ensure_calls += 1

        def tmux_for(self, _host: str) -> SendTmux:
            raise AssertionError("blocked usage must not resolve peer tmux")

    async def run() -> tuple[VerbError, Hosts, SendTmux]:
        store = Store(":memory:")
        store.start()
        hosts = Hosts()
        tmux = SendTmux()
        sessions = Sessions(store, tmux=tmux, local_host="hosta", hosts=hosts)
        ctl = SpawnCtl(store, sessions, tmux=tmux, hosts=hosts)
        comms = Comms(store, sessions, ctl, hosts=hosts)
        try:
            await sessions.open("hostb", NAME, provider="codex")
            with pytest.raises(VerbError) as raised:
                await comms.send({
                    "stream_id": f"hostb:{NAME}",
                    "message": "/usage",
                    "request_id": "peer-agent-usage",
                    "from_stream_id": "hosta:agent",
                })
            return raised.value, hosts, tmux
        finally:
            store.stop()

    error, hosts, tmux = asyncio.run(run())
    assert error.code == "reset_blocked"
    assert hosts.ensure_calls == 0
    assert tmux.captures == 0
