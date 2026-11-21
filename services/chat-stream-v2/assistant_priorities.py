"""Atomic ranked priority lists and their immutable revisions."""
import json
import uuid
from assistant_owed import operator_route, route, stamp, text


def initialize(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS v2_assistant_priorities (stream_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, items_json TEXT NOT NULL)')
    conn.execute('CREATE TABLE IF NOT EXISTS v2_assistant_priorities_history (stream_id TEXT NOT NULL, revision INTEGER NOT NULL, items_json TEXT NOT NULL, actor TEXT NOT NULL, generation TEXT NOT NULL, changed_at TEXT NOT NULL, action TEXT NOT NULL, source TEXT, PRIMARY KEY(stream_id, revision))')


def snapshot(conn, stream, revision=None):
    if revision is None:
        row = conn.execute('SELECT revision,items_json FROM v2_assistant_priorities WHERE stream_id=?', (stream,)).fetchone()
        if row is None: return {'revision': 0, 'items': []}
    else:
        if revision == 0: return {'revision': 0, 'items': []}
        row = conn.execute('SELECT revision,items_json FROM v2_assistant_priorities_history WHERE stream_id=? AND revision=?', (stream, revision)).fetchone()
        if row is None: raise ValueError('assistant_priorities_revision_not_found')
    return {'revision': row['revision'], 'items': json.loads(row['items_json'])}


def validate_source(conn, stream, value):
    if not isinstance(value, str) or ':' not in value: raise ValueError('assistant_priorities_source_invalid')
    kind, key = value.split(':', 1)
    if kind == 'message' and operator_route(conn, stream, key):
        return route(conn, stream, key)['body']
    if kind == 'seat' and ':' in key:
        host, name = key.split(':', 1)
        if conn.execute('SELECT 1 FROM sessions WHERE host=? AND session_name=?', (host, name)).fetchone(): return None
    raise ValueError('assistant_priorities_source_invalid')


def clean(value, limit):
    try: return text(value, 1, limit)
    except ValueError as exc: raise ValueError('assistant_priorities_text_invalid') from exc


class Priorities:
    def __init__(self, composite): self.composite = composite; self.store = composite.store
    async def request(self, action, args, auth):
        stream = self.composite.config.stream_id
        def op(conn):
            conn.execute('BEGIN IMMEDIATE')
            try:
                self.composite.owed.authorize(conn, auth, action != 'list')
                result = snapshot(conn, stream, args.get('revision') if action == 'list' else None)
                if action == 'list': conn.commit(); return result
                items = result['items']; source = args.get('source')
                if action in ('add', 'move') or (action == 'remove' and not args.get('done')):
                    source_body = validate_source(conn, stream, source)
                if action == 'add':
                    words = args.get('words')
                    if not isinstance(words, str) or not 1 <= len(words.strip()) <= 400 or any(ord(c) < 32 and not c.isspace() for c in words):
                        raise ValueError('assistant_priorities_text_invalid')
                    words = words.strip()
                    if source_body is not None and ' '.join(words.casefold().split()) not in ' '.join(source_body.casefold().split()):
                        raise ValueError('assistant_priorities_words_not_in_source')
                    item = dict(id='priority-'+uuid.uuid4().hex[:12], label=clean(args.get('label'),80), words=words, source=source, owner=clean(args['owner'],160) if args.get('owner') and args['owner'] != 'none' else None, spec_id=clean(args['spec_id'],160) if args.get('spec_id') else None, words_checked=source_body is not None)
                else:
                    item = next ( (x for x in items if x['id'] == args.get('id')), None)
                    if item is None: raise ValueError('assistant_priorities_not_found')
                if action in ('add', 'move'):
                    if action == 'move': items.remove(item)
                    rank = args.get('rank')
                    if isinstance(rank, bool) or not isinstance(rank, int) or not 1 <= rank <= len(items)+1: raise ValueError('assistant_priorities_rank_invalid')
                    items.insert(rank-1,item)
                elif action == 'remove':
                    if bool(args.get('done')) == bool(source): raise ValueError('assistant_priorities_source_invalid')
                    items.remove(item)
                elif action == 'owner': item['owner'] = clean(args['owner'],160) if args.get('owner') and args['owner'] != 'none' else None
                else: raise ValueError('assistant_priorities_action_invalid')
                for rank, item in enumerate(items,1): item['rank'] = rank
                revision = result['revision']+1; encoded = json.dumps(items,ensure_ascii=False)
                conn.execute('INSERT INTO v2_assistant_priorities VALUES(?,?,?) ON CONFLICT(stream_id) DO UPDATE SET revision=excluded.revision,items_json=excluded.items_json',(stream,revision,encoded))
                conn.execute('INSERT INTO v2_assistant_priorities_history VALUES(?,?,?,?,?,?,?,?)',(stream,revision,encoded,auth.get('stream_id') or auth.get('operator_principal') or '',auth.get('session_generation') or '',stamp(),action,source))
                conn.commit(); return {'revision':revision,'items':items}
            except BaseException: conn.rollback(); raise
        return await self.store.submit(op)
