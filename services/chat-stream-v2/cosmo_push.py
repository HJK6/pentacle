"""Cosmo reply push: one Expo push to the scoped (CosmoPushTokens) audience when
an assistant reply is committed on the scoped stream.

Deduplication is inherent: the composite invokes this once per committed reply
(a replay/reconnect is a duplicate publication and never calls here), so no
per-message ledger is needed.  Revocation is re-checked per send and
``DeviceNotRegistered`` tokens are deleted.  The Expo transport is injectable so
tests use a stub (no real notification).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from consent_push import SEND_URL, post as _default_transport

log = logging.getLogger("chat_streamd_v2.cosmo_push")


class CosmoPush:
    def __init__(
        self,
        *,
        table_factory: Callable[[], Any],
        registry: Any = None,
        transport: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
        revoked: Callable[[str], bool] | None = None,
    ) -> None:
        # ``table_factory`` returns a DynamoDB Table (boto3) or a stub exposing
        # ``scan()`` and ``delete_item(Key=...)``.
        self._table_factory = table_factory
        self._registry = registry
        self._transport = transport or _default_transport
        self._revoked = revoked

    def _is_revoked(self, credential_id: str) -> bool:
        if self._revoked is not None:
            return bool(self._revoked(credential_id))
        if self._registry is None:
            return False
        try:
            record = self._registry.load().credentials.get(credential_id)
        except Exception:  # noqa: BLE001 - an unreadable registry fails closed
            return True
        return record is None or bool(record.get("revoked_at"))

    async def push_reply(self, *, stream_id: str, message_id: str, text: str) -> int:
        """Send one push to every active, non-revoked scoped token for ``stream_id``."""
        title = "Daff"
        body = (text or "")[:120]

        def _work() -> int:
            table = self._table_factory()
            try:
                items = table.scan().get("Items", []) or []
            except Exception:  # noqa: BLE001 - a push outage must not break the reply
                log.exception("cosmo push token scan failed stream=%s", stream_id)
                return 0
            sent = 0
            for item in items:
                if not item.get("active"):
                    continue
                if item.get("scope_stream") != stream_id:
                    continue
                if self._is_revoked(str(item.get("credential_id") or "")):
                    continue
                token = item.get("push_token")
                if not token:
                    continue
                try:
                    result = self._transport(SEND_URL, {
                        "to": token, "title": title, "body": body, "sound": "default",
                        "data": {"stream_id": stream_id, "message_id": message_id},
                    })
                except Exception:  # noqa: BLE001 - transient; best-effort
                    continue
                data = result.get("data") if isinstance(result, dict) else None
                if isinstance(data, list):
                    data = data[0] if data else None
                if not isinstance(data, dict):
                    continue
                if data.get("status") == "ok":
                    sent += 1
                elif str((data.get("details") or {}).get("error") or "") == "DeviceNotRegistered":
                    try:
                        table.delete_item(Key={"push_token": token})
                    except Exception:  # noqa: BLE001
                        log.exception("cosmo push stale-token delete failed")
            return sent

        return await asyncio.to_thread(_work)
