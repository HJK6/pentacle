"""Cooperative whole-screen leases; no desktop input or presence detection."""

import logging
import time

log = logging.getLogger("chat_streamd_v2.screen_lease")


class ScreenLease:
    def __init__(self, store, hosts, *, env_binding=None, clock=time.time):
        self.store, self.hosts, self.env_binding, self.clock = (
            store,
            list(hosts),
            env_binding,
            clock,
        )

    def wire_handlers(self):
        return {
            "screen.lease." + v: self.handle
            for v in ("acquire", "renew", "release", "status")
        }

    async def handle(self, message):
        verb = str(message.get("type") or "").rpartition(".")[2]
        auth = message.get("_auth_context") or {}
        actor = auth.get("stream_id")
        if verb not in ("acquire", "renew", "release", "status"):
            result = {"error_code": "invalid_request"}
        elif verb != "status" and (
            not auth.get("token_verified")
            or not actor
            or message.get("from_stream_id") not in (None, "", actor)
        ):
            result = {"error_code": "not_authenticated"}
        else:
            try:
                result = await self.store.screen_lease_call(
                    verb,
                    hosts=self.hosts,
                    message=message,
                    actor=actor,
                    env_binding=(
                        self.env_binding() if callable(self.env_binding) else {}
                    ),
                    now=self.clock(),
                )
            except ValueError as exc:
                if str(exc) != "screen_front_desk_unavailable":
                    raise
                result = {"error_code": str(exc)}
        ok = "error_code" not in result
        if verb != "status":
            log.info(
                "subsystem=screen_lease bug_ref=screen_lease action=%s ok=%s", verb, ok
            )
        reply = {
            "type": f"screen.lease.{verb}.{'ok' if ok else 'error'}",
            "ok": ok,
            **result,
        }
        if message.get("request_id") is not None:
            reply["request_id"] = message["request_id"]
        return reply
