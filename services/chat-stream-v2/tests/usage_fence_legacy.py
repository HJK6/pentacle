"""Frozen EventPush fence oracle from public main82636b4e (before projection).

The original body below is retained byte-for-byte; its Store dependency is the
unchanged full inventory API. Do not update this oracle alongside the new query.
"""


class LegacyFences:
    def __init__(self, store):
        self.store = store

    async def _usage_fences(self, host: str) -> list[dict[str, str]]:
        """Return current coordinator-issued usage fences to a satellite."""
        rows = await self.store.list_open_sessions_with_event_summary()
        fences: list[dict[str, str]] = []
        for row in rows:
            if row.get("host") != host or row.get("provider") not in {"codex", "claude"}:
                continue
            fences.append({
                "stream_id": str(row.get("stream_id") or ""),
                "session_generation": str(row.get("session_generation") or ""),
                "provider": str(row.get("provider") or ""),
                "source_pane_pid": str(row.get("pane_pid") or ""),
                "session_created_at": str(row.get("created_at") or ""),
                **({"account_binding": row["account_binding"]} if row.get("account_binding") else {}),
            })
        return [fence for fence in fences if all(fence.values())]
