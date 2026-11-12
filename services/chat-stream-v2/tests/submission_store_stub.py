"""Read API adapter for existing event-list test doubles, never a real Store."""


class SubmissionReadStoreStub:
    async def fetch_session_event_watermark(self, stream_id: str) -> int:
        # Retain each fixture's event progression, injected failures and call
        # counts when its pre-action snapshot moves to the watermark API.
        events = await self.fetch_session_event_tail(stream_id, limit=500)
        return max((int(event.get("daemon_seq") or 0) for event in events), default=0)
