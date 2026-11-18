"""Advisory coordination holds. Expiration is evaluated on reads, not swept."""
from datetime import datetime, timezone
import uuid


def initialize(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS holds (
        id TEXT PRIMARY KEY, resource TEXT NOT NULL, owner_stream TEXT NOT NULL,
        reason TEXT NOT NULL, acquired_at TEXT NOT NULL, expires_at TEXT,
        released_at TEXT, release_cause TEXT)""")
    conn.execute('CREATE INDEX IF NOT EXISTS holds_resource_live ON holds(resource, released_at, expires_at)')
    conn.execute('CREATE INDEX IF NOT EXISTS holds_owner_live ON holds(owner_stream, released_at, expires_at)')


def instant(value):
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timezone required')
    return parsed.astimezone(timezone.utc)


def stamp(value):
    return value.isoformat(timespec='microseconds').replace('+00:00', 'Z')


class Holds:
    def __init__(self, store, composite=None):
        self.store = store
        self.composite = composite

    def _identity(self, conn, auth):
        operator = (auth.get('operator_authenticated') is True
                    and str(auth.get('operator_principal') or '').startswith('operator:'))
        if operator:
            return str(auth['operator_principal']), True
        if not auth.get('token_verified') or auth.get('scoped_principal') or auth.get('dot_principal'):
            raise ValueError('hold_unauthorized')
        actor = auth.get('stream_id')
        from store_routing import _assistant_actor_conn
        try:
            _assistant_actor_conn(conn, actor, auth.get('session_generation'))
        except ValueError as exc:
            raise ValueError('hold_unauthorized') from exc
        binding = self.composite.owed.binding(conn) if self.composite else {}
        front_desk = (binding.get('stream_id') == actor
                      and binding.get('generation') == auth.get('session_generation'))
        return actor, front_desk

    async def request(self, action, args, auth):
        def op(conn):
            conn.execute('BEGIN IMMEDIATE')
            try:
                if action == 'list':
                    if not any(auth.get(key) for key in (
                        'operator_authenticated', 'token_verified', 'service_authenticated',
                        'scoped_principal', 'dot_principal',
                    )):
                        raise ValueError('hold_unauthorized')
                    actor, privileged = '', False
                else:
                    actor, privileged = self._identity(conn, auth)
                now = datetime.now(timezone.utc)
                # Legacy rows may have no expiry; retain their existing live semantics.
                def live(row):
                    if row['released_at'] is not None:
                        return False
                    return not row['expires_at'] or instant(row['expires_at']) > now

                if action == 'acquire':
                    if not privileged:
                        raise ValueError('hold_unauthorized')
                    if not args.get('expires_at'):
                        raise ValueError('hold_expiry_required: pass --ttl')
                    try:
                        end = instant(args['expires_at'])
                    except (ValueError, TypeError) as exc:
                        raise ValueError('hold_expiry_invalid') from exc
                    seconds = (end-now).total_seconds()
                    if seconds > 86400:
                        raise ValueError('hold_expiry_too_long')
                    if seconds <= 0:
                        raise ValueError('hold_expiry_invalid')
                    resource = str(args.get('resource') or '').strip()
                    reason = str(args.get('reason') or '').strip()
                    owner = str(args.get('owner') or actor).strip()
                    if not resource or not reason or not owner:
                        raise ValueError('hold_arguments_invalid')
                    rows = conn.execute('SELECT * FROM holds WHERE resource=? AND released_at IS NULL', (resource,))
                    for row in rows:
                        if live(row):
                            raise ValueError(f"hold_taken: {row['id']} owned by {row['owner_stream']}")
                    ident = 'hold-' + uuid.uuid4().hex
                    conn.execute('INSERT INTO holds VALUES (?,?,?,?,?,?,NULL,NULL)',
                                 (ident, resource, owner, reason, stamp(now), stamp(end)))
                    result = {'hold': dict(conn.execute('SELECT * FROM holds WHERE id=?', (ident,)).fetchone())}
                elif action == 'release':
                    if bool(args.get('hold_id')) == bool(args.get('resource')):
                        raise ValueError('hold_arguments_invalid')
                    field = 'id' if args.get('hold_id') else 'resource'
                    value = args.get('hold_id') or args['resource']
                    rows = conn.execute(f'SELECT * FROM holds WHERE {field}=? AND released_at IS NULL ORDER BY acquired_at', (value,))
                    row = next((dict(r) for r in rows if live(r)), None)
                    if row is None:
                        raise ValueError('hold_not_live')
                    own = row['owner_stream'] == actor
                    if not own:
                        if not privileged:
                            raise ValueError('hold_unauthorized')
                        if args.get('force') is not True:
                            raise ValueError('hold_force_required')
                    conn.execute('UPDATE holds SET released_at=?,release_cause=? WHERE id=?',
                                 (stamp(now), 'released' if own else 'forced', row['id']))
                    result = {'hold': dict(conn.execute('SELECT * FROM holds WHERE id=?', (row['id'],)).fetchone())}
                elif action == 'list':
                    sql = 'SELECT * FROM holds WHERE released_at IS NULL'
                    params = ()
                    if args.get('resource'):
                        sql += ' AND resource=?'
                        params = (args['resource'],)
                    result = {'holds': [dict(r) for r in conn.execute(sql+' ORDER BY acquired_at,id', params) if live(r)]}
                else:
                    raise ValueError('hold_action_invalid')
                conn.commit()
                return result
            except BaseException:
                conn.rollback()
                raise
        return await self.store.submit(op)
