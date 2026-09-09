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
