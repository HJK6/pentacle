"""Additive intake checkpoint storage, confined to the Store worker."""
from ingest_checkpoint import MAX_RECORD_BYTES


def initialize(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS v2_ingest_positions (
        stream_id TEXT PRIMARY KEY,
        position_json TEXT NOT NULL,
        provenance_json TEXT NOT NULL,
        routing_json TEXT NOT NULL
    )""")


class IngestCheckpointStoreMixin:
    async def invalidate_ingest_position(self, stream_id, generation):
        def invalidate(conn):
            with conn:
                conn.execute("DELETE FROM v2_ingest_positions WHERE stream_id=? "
                             "AND json_extract(position_json,'$.generation')=?", (stream_id, generation))
        await self.submit(invalidate)

    async def load_ingest_position(self, stream_id):
        def load(conn):
            size = conn.execute("SELECT length(CAST(position_json AS BLOB)) + "
                                "length(CAST(provenance_json AS BLOB)) + length(CAST(routing_json AS BLOB)) "
                                "FROM v2_ingest_positions WHERE stream_id=?", (stream_id,)).fetchone()
            if size is None:
                return None
            if size[0] is None or size[0] > MAX_RECORD_BYTES:
                return {"invalid": "state_too_large"}
            row = conn.execute("SELECT position_json,provenance_json,routing_json "
                               "FROM v2_ingest_positions WHERE stream_id=?", (stream_id,)).fetchone()
            return dict(row)
        return await self.submit(load)

    async def save_ingest_position(self, row, changes):
        """Write only changed columns, after checking the current open lifecycle."""
        allowed = {"position_json", "provenance_json", "routing_json"}
        if not changes or not set(changes) <= allowed:
            raise ValueError("invalid checkpoint columns")
        host, name = row["stream_id"].split(":", 1)

        def save(conn):
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                current = conn.execute(
                    "SELECT s.created_at,g.generation,s.provider,s.pane_pid FROM sessions s "
                    "JOIN v2_session_generations g ON g.host=s.host AND g.session_name=s.session_name "
                    "WHERE s.host=? AND s.session_name=? AND s.status='open'",
                    (host, name)).fetchone()
                if (current is None or current["generation"] != row.get("session_generation")
                        or current["created_at"] != row.get("created_at")
                        or current["provider"] != row.get("provider")
                        or str(current["pane_pid"] or "") != str(row.get("pane_pid") or "")):
                    return False
                exists = conn.execute("SELECT 1 FROM v2_ingest_positions WHERE stream_id=?",
                                      (row["stream_id"],)).fetchone()
                if exists:
                    columns = list(changes)
                    conn.execute("UPDATE v2_ingest_positions SET " + ",".join(f"{c}=?" for c in columns)
                                 + " WHERE stream_id=?", [changes[c] for c in columns] + [row["stream_id"]])
                else:
                    if set(changes) != allowed:
                        return False
                    conn.execute("INSERT INTO v2_ingest_positions VALUES (?,?,?,?)",
                                 (row["stream_id"], changes["position_json"], changes["provenance_json"], changes["routing_json"]))
            return True
        return await self.submit(save)
