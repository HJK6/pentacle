import asyncio
import pytest

from reconciler import SessionReconciler
from sessions import Sessions
from store import Store
from tmux_transport import Tmux


class SshFormattingTmux(Tmux):
    async def run(self, *args, **kwargs):
        rendered = args[-1]
        for field, value in {'session_name': 'v2-owned', 'pane_pid': '123', 'pane_id': '%1',
                             'pane_tty': '/dev/ttys001', 'socket_path': '/tmp/tmux-owned'}.items():
            rendered = rendered.replace('#{' + field + '}', value)
        return 0, rendered.replace('\t', '_') + '\n'


class Hosts:
    local_host = 'hosta'
    peers = {'hostc': object()}
    def tmux_for(self, host):
        return SshFormattingTmux()
    async def run_command(self, host, *args, **kwargs):
        if args[0] == 'ps':
            return 0, '123 1 501 123 123 Mon Jan 1 00:00:00 2026 shell\n'
        return 0, 'boot-owned'


@pytest.mark.parametrize('line', ['v2-owned_123_%1_/dev/tty_/tmp/socket',
                                 'v2-owned|123|%1|/dev/tty|/tmp/socket|extra',
                                 'v2-owned|bad|%1|/dev/tty|/tmp/socket'])
def test_reap_inventory_rejects_whole_mixed_observation(line):
    class BadTmux(SshFormattingTmux):
        async def run(self, *args, **kwargs):
            return 0, 'v2-valid|123|%1|/dev/tty|/tmp/socket\n' + line + '\n'
    hosts = Hosts()
    hosts.tmux_for = lambda host: BadTmux()
    sessions = Sessions(Store(':memory:'), tmux=None, local_host='hosta')
    assert asyncio.run(SessionReconciler(sessions, hosts)._reap_inventory('hostc')) is None


def test_pane_identity_rejects_a_malformed_record_even_after_a_valid_pane():
    class BadTmux(SshFormattingTmux):
        async def run(self, *args, **kwargs):
            return 0, '123|%1|/dev/tty|/tmp/socket|v2-owned\nv2-owned_456\n'
    assert asyncio.run(BadTmux().pane_identity('v2-owned')) is None


def test_remote_pane_identity_survives_underscore_for_tab_transport():
    identity = asyncio.run(SshFormattingTmux().pane_identity('v2-owned'))
    assert identity == {'session_name': 'v2-owned', 'pane_pid': '123', 'pane_id': '%1',
                        'tty': '/dev/ttys001', 'tmux_socket': '/tmp/tmux-owned'}


def test_remote_reap_inventory_survives_underscore_for_tab_transport():
    sessions = Sessions(Store(':memory:'), tmux=None, local_host='hosta')
    result = asyncio.run(SessionReconciler(sessions, Hosts())._reap_inventory('hostc'))
    assert result is not None
    panes, processes, boot = result
    assert panes[0]['session_name'] == 'v2-owned' and panes[0]['pane_pid'] == 123
    assert 123 in processes and boot == 'boot-owned'


CLOSE_FIELDS = {
    "pane_pid": "123", "pane_id": "%1", "pane_tty": "/dev/ttys001",
    "socket_path": "/tmp/tmux-owned", "session_name": "v2-owned",
    "session_id": "$7", "pid": "99", "version": "3.7c",
    "pane_dead": "0", "pane_pipe": "0", "pane_dead_status": "", "pane_dead_signal": "",
}


class CloseBindingTmux(Tmux):
    def __init__(self, **changes):
        super().__init__()
        self.fields = {**CLOSE_FIELDS, **changes}
        self.extra = ""
        self.calls = []

    async def run(self, *args, **kwargs):
        self.calls.append(args)
        rendered = args[-1]
        for key, value in self.fields.items():
            rendered = rendered.replace("#{" + key + "}", value)
        return 0, rendered + "\n" + self.extra


@pytest.mark.parametrize("version", ["3.4", "3.7c"])
def test_close_binding_requires_complete_qualified_live_snapshot(version):
    tmux = CloseBindingTmux(version=version)
    result = asyncio.run(tmux.close_pane_identity("v2-owned"))
    assert result == {"pane_pid": "123", "pane_id": "%1", "tty": "/dev/ttys001",
                      "tmux_socket": "/tmp/tmux-owned", "session_name": "v2-owned",
                      "session_id": "$7", "server_pid": "99", "server_version": version,
                      "pane_dead": "0", "pane_pipe": "0",
                      "pane_dead_status": "", "pane_dead_signal": ""}
    assert tmux.calls[0][:4] == ("list-panes", "-s", "-t", "=v2-owned:")


@pytest.mark.parametrize("field,value", [
    ("pane_dead", "1"), ("pane_dead", ""), ("pane_pipe", "1"),
    ("pane_dead_status", "0"), ("pane_dead_status", "1"), ("pane_dead_signal", "9"),
    ("version", ""), ("version", "3.7"), ("version", "3.4a"),
    ("pane_pid", "0"), ("pane_pid", "-1"), ("pane_pid", "１２３"),
    ("pid", ""), ("pid", "0"), ("pane_id", ""), ("session_id", ""),
    ("pane_tty", ""), ("socket_path", ""), ("session_name", "other"),
    ("pane_tty", "/dev/tty|extra"), ("pane_tty", "/dev/tty\nextra"),
])
def test_close_binding_rejects_terminal_missing_unknown_or_malformed_fields(field, value):
    assert asyncio.run(CloseBindingTmux(**{field: value}).close_pane_identity("v2-owned")) is None


def test_close_binding_rejects_hidden_second_window_and_partial_response():
    tmux = CloseBindingTmux()
    tmux.extra = "456|%2|/dev/tty2|/tmp/tmux-owned|v2-owned|$7|99|3.7c|0|0||\n"
    assert asyncio.run(tmux.close_pane_identity("v2-owned")) is None
    class Partial(Tmux):
        async def run(self, *args, **kwargs):
            return 0, "123|%1|/dev/tty|/tmp/tmux-owned|v2-owned|$7|99|3.7c|0|0\n"
    assert asyncio.run(Partial().close_pane_identity("v2-owned")) is None


@pytest.mark.parametrize("exit_kind", ["status", "signal"])
def test_private_tmux_terminal_pane_refuses_anchor_and_preserves_foreign_sentinel(
    isolated_tmux_env, tmp_path, exit_kind,
):
    import shlex
    import subprocess
    import sys
    marker = tmp_path / "exit-owned-pane"
    code = ("import os,signal,time; from pathlib import Path; "
            f"p=Path({str(marker)!r}); "
            "exec('while not p.exists(): time.sleep(0.01)'); "
            + ("raise SystemExit(0)" if exit_kind == "status" else
               "os.kill(os.getpid(),signal.SIGTERM)"))
    async def go():
        tmux = Tmux(isolated_tmux_env)
        sentinel = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            await tmux.new_session("v2-owned", "exec " + shlex.join([sys.executable, "-c", code]))
            rc, _ = await tmux.run("set-option", "-w", "-t", "=v2-owned:", "remain-on-exit", "on")
            assert rc == 0
            live = await tmux.close_pane_identity("v2-owned")
            assert live is not None
            assert live["server_version"] in {"3.4", "3.7c"}
            # This server is private. Latch the actual retained-pane exit before
            # triggering it, so scheduler load cannot exhaust a polling guess.
            rc, _ = await tmux.run("set-hook", "-g", "pane-died",
                                   "wait-for -S owned-pane-exited")
            assert rc == 0
            # Only the pane's own code exits or signals itself. No foreign PID reuse.
            marker.touch()
            rc, _ = await tmux.run("wait-for", "owned-pane-exited")
            assert rc == 0
            rc, out = await tmux.run("list-panes", "-s", "-t", "=v2-owned:", "-F",
                                     "#{pane_dead}|#{pane_dead_status}|#{pane_dead_signal}")
            assert rc == 0
            dead, terminal = out.rstrip("\n").split("|", 1)
            assert dead == "1"
            assert (terminal == "0|" if exit_kind == "status" else
                    terminal.startswith("|") and len(terminal) > 1), out
            assert await tmux.has_session("v2-owned")  # retained dead pane, not missing session
            assert await tmux.close_pane_identity("v2-owned") is None
            assert sentinel.poll() is None
        finally:
            await tmux.run("kill-server")
            sentinel.terminate()
            sentinel.wait(timeout=5)
    asyncio.run(go())


def test_private_tmux_pipe_and_second_window_refuse_anchor(isolated_tmux_env):
    async def go():
        tmux = Tmux(isolated_tmux_env)
        try:
            await tmux.new_session("v2-owned", "exec sleep 60")
            assert await tmux.close_pane_identity("v2-owned") is not None
            rc, _ = await tmux.run("pipe-pane", "-t", "=v2-owned:", "cat >/dev/null")
            assert rc == 0
            assert await tmux.close_pane_identity("v2-owned") is None
            await tmux.run("pipe-pane", "-t", "=v2-owned:")
            assert await tmux.close_pane_identity("v2-owned") is not None
            # The extra pane lives in an unselected window. -s must still see it.
            rc, _ = await tmux.run("new-window", "-d", "-t", "=v2-owned:", "exec sleep 60")
            assert rc == 0
            assert await tmux.close_pane_identity("v2-owned") is None
        finally:
            await tmux.run("kill-server")
    asyncio.run(go())
