"""The functional probe closes only its created session through v2 proof."""
import contextlib
import io
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pentacle.live import _main


class LiveTest(unittest.TestCase):
    def test_reply_cleanup_uses_connection_bound_operator_transport(self):
        self.run_probe({"profiles": {
            "agent_orch": {"claude": ["ordinary-model", "high"]},
            "test_prompt": {"claude": ["test-model", "low"]}}}, True)

    def test_missing_or_disabled_test_profile_never_launches_ordinary_work(self):
        for profile in (None, [], ["test-model"], ["test-model", "high"], [None, "low"]):
            with self.subTest(profile=profile):
                self.run_probe({"profiles": {
                    "agent_orch": {"claude": ["ordinary-model", "high"]},
                    "test_prompt": {"claude": profile}}}, False)

    def run_probe(self, catalog_value, enabled):
        calls = []
        config = types.ModuleType("agent_orch.config")
        config.load_config = lambda: types.SimpleNamespace(host_id="example")
        client = types.ModuleType("agent_orch.wsclient")
        async def catalog(*args, **kwargs):
            return catalog_value
        async def spawn(configuration, payload, **kwargs):
            calls.append(("spawn", payload))
            return {"type": "spawn.ok", "stream_id": "example:probe"}
        async def inspect(*args, **kwargs):
            return {"recent_events": [{"kind": "ASSIST_TEXT", "text": "PENTACLE_DOCTOR_OK"}]}
        async def close(configuration, payload, **kwargs):
            calls.append(("close", payload, kwargs))
            return {"type": "close.ok"}
        client.spawn_catalog_get_once, client.spawn_once = catalog, spawn
        client.inspect_stream_once, client._operator_envelope_rpc = inspect, close
        modules = {"agent_orch": types.ModuleType("agent_orch"), "agent_orch.config": config, "agent_orch.wsclient": client}
        with patch.dict(sys.modules, modules), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(_main("claude"), (enabled, enabled))
        self.assertEqual(json.loads(output.getvalue()), {"replied": enabled, "closed": enabled})
        if not enabled:
            self.assertEqual(calls, [])
            return
        self.assertEqual(calls[0][1]["model"], "test-model")
        self.assertEqual(calls[0][1]["effort"], "low")
        self.assertEqual(calls[-1][1]["host"], "example")
        self.assertEqual(calls[-1][1]["session_name"], "probe")
        self.assertEqual(calls[-1][2]["caller"], None)
        self.assertEqual(calls[-1][2]["prefix"], "close")
        self.assertNotIn("token", calls[-1][1])
        self.assertNotIn("stream_token", calls[-1][1])


if __name__ == "__main__":
    unittest.main()
