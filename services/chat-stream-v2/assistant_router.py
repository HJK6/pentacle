"""Authenticated, bounded local-Qwen adapter for assistant routing.

The daemon never calls the mic HTTP listener directly: that listener is
loopback-only and currently unauthenticated.  It uses the fleet's existing SSH
transport to execute one fixed adapter command and exchanges one JSON object on
stdin/stdout.  Operator text is data on stdin, never part of a shell command.
"""

from __future__ import annotations

import asyncio
import json
import re
import shlex
from typing import Any
from urllib.parse import urlparse

from machines import load_machines, ssh_command
_HOST_RE = re.compile(r"^[A-Za-z0-9_.-]{1,253}$")
_PROCESS_DIAGNOSTIC_BYTES = 2048


class AssistantRouterProcessError(RuntimeError):
    """Bounded process evidence; a script error does not identify its root cause."""

    def __init__(self, returncode: int, stdout: bytes, stderr: bytes) -> None:
        self.returncode = returncode
        self.stdout = stdout[:_PROCESS_DIAGNOSTIC_BYTES].decode("utf-8", errors="ignore")
        self.stderr = stderr[:_PROCESS_DIAGNOSTIC_BYTES].decode("utf-8", errors="ignore")
        script_error = False
        if returncode == 2 and len(stderr) <= _PROCESS_DIAGNOSTIC_BYTES:
            try:
                error = json.loads(stderr)
                script_error = isinstance(error, dict) and error.get("error") == "assistant_router_failed"
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
        super().__init__("assistant_router_script_failed" if script_error else "assistant_router_transport_failed")


class AssistantRouterAdapter:
    """One request -> one bounded authenticated adapter invocation."""

    def __init__(
        self,
        endpoint: str,
        *,
        timeout_s: float,
        ssh_bin: str = "ssh",
        action_path: str,
    ) -> None:
        parsed = urlparse(endpoint)
        if parsed.scheme != "ssh" or not parsed.hostname or parsed.path != "/assistant-router-v1":
            raise ValueError("assistant_router_endpoint_invalid")
        if not _HOST_RE.fullmatch(parsed.hostname):
            raise ValueError("assistant_router_endpoint_invalid")
        if not action_path.startswith("/") or "\x00" in action_path:
            raise ValueError("assistant_router_action_path_invalid")
        self.endpoint = endpoint
        self.host = parsed.hostname
        self.timeout_s = float(timeout_s)
        self.ssh_bin = ssh_bin
        self.action_path = action_path

    @property
    def fixed_command(self) -> tuple[str, str, str]:
        return ("python3", self.action_path, "assistant-router-stdin")

    def _ssh_target(self) -> str:
        # Endpoint host names are portable fleet identities, not a requirement
        # that every workstation's ssh_config happens to contain an alias.
        # Reuse the daemon's existing machine configuration/secure SSH target.
        for machine in load_machines():
            if machine.name == self.host and machine.ssh_target:
                return machine.ssh_target
        return self.host

    def argv(self) -> list[str]:
        # shlex.join is applied only to fixed program words; the route payload
        # is written to stdin below and cannot influence a remote shell.
        return ssh_command(
            self._ssh_target(),
            shlex.join(self.fixed_command),
            ssh_bin=self.ssh_bin,
            connect_timeout=min(30.0, max(1.0, self.timeout_s / 3)),
        )

    async def classify(self, route: dict[str, Any]) -> dict[str, Any]:
        payload = json.dumps(route, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(payload) > 64 * 1024:
            raise ValueError("assistant_router_payload_oversize")
        process = await asyncio.create_subprocess_exec(
            *self.argv(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(payload), timeout=self.timeout_s)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise TimeoutError("assistant_router_timeout")
        if process.returncode != 0:
            raise AssistantRouterProcessError(process.returncode, stdout, stderr)
        if len(stdout) > 64 * 1024:
            raise ValueError("assistant_router_response_oversize")
        try:
            result = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("assistant_router_response_invalid") from exc
        if not isinstance(result, dict):
            raise ValueError("assistant_router_response_invalid")
        return result
