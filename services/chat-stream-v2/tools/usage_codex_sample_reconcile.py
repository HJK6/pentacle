#!/usr/bin/env python3
"""Read-only Codex ledger-vs-rows sample reconciliation.

spec_pentacle__usage_codex_ledger_vs_rows_sample_reconciliation_2026_10. For a Codex native session this
compares three sources of token mass: the original rollout transcript, the cumulative ledger row
(``v2_usage_records``) and the per-response rows (``v2_usage_codex_responses``).

There is no write path: the ledger is opened with ``mode=ro`` and only ``SELECT`` statements are issued; the
transcript is opened for reading; output goes to stdout or a caller-named CSV/JSON file. Native session ids
and account ids are hashed in every redacted output. Stdlib only, Python 3.9 compatible (the ledger host's system interpreter).

    transcripts  read rollout files on THIS host        -> JSON lines of transcript facts (run over ssh)
    sample       stratified, seeded session selection   -> JSON (ledger snapshot only)
    join         transcript facts + ledger (ro)         -> redacted CSV and a JSON summary
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

USAGE = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens', 'output_tokens', 'reasoning_output_tokens')
FIELDS = ('input', 'cached_input', 'cache_write_input', 'output', 'reasoning_output')
BUCKETS = ('uncached_input', 'cache_read', 'cache_write', 'output')
NAME_ID = re.compile(r'rollout-.*-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$')
SALT = 'pentacle-codex-sample-reconcile|'
BIG_TOKENS = 10_000_000


def digest(value: Any) -> str:
    """Stable 12-hex pseudonym for a native/account id; never reversible from the CSV."""
    return hashlib.sha256((SALT + str(value)).encode()).hexdigest()[:12]


def _int(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _vector(usage: Any) -> dict[str, int] | None:
    """The five counters of one usage object, or None when any is missing/negative/non-int."""
    if not isinstance(usage, dict):
        return None
    out = {}
    for field, key in zip(FIELDS, USAGE):
        value = usage.get(key)
        if field == 'cache_write_input' and value is None:
            value = 0
        value = _int(value)
        if value is None:
            return None
        out[field] = value
    return out


def _zero() -> dict[str, int]:
    return dict.fromkeys(FIELDS, 0)


# --- transcript side -----------------------------------------------------------------------------------------

def analyze_transcript(path: Path, *, with_hash: bool = False) -> dict[str, Any]:
    """Facts from one whole rollout file. Mirrors the backfill's own-response rule (usage_accounting
    ``_codex_provenance``) so row-set comparisons are like for like, and keeps what it would drop."""
    size = path.stat().st_size
    name = NAME_ID.search(path.name)
    sha = hashlib.sha256() if with_hash else None
    lines = malformed = 0
    meta_ids: list[str] = []
    accounts: list[str] = []
    cli_version = thread_source = source = None
    native: str | None = None
    seen: dict[str, dict[str, int]] = {}
    own_order: list[str] = []
    foreign: dict[str, dict[str, int]] = {}
    dup_same = dup_conflict = invalid = 0
    snapshots: list[list[Any]] = []
    snap_null = snap_invalid = 0
    last_thread: dict[str, int] | None = None
    running = _zero()
    thread_inconsistent = 0
    tail_after_snapshot = 0
    gap_events = zero_last = prefix_synced = synced_total = 0
    cached_informative = reasoning_informative = False
    gap_prev = 0
    first_gap = None
    with open(path, 'rb') as handle:
        for raw in handle:
            if sha is not None:
                sha.update(raw)
            lines += 1
            try:
                record = json.loads(raw)
            except ValueError:
                malformed += 1
                continue
            if not isinstance(record, dict):
                malformed += 1
                continue
            kind = record.get('type')
            payload = record.get('payload') if isinstance(record.get('payload'), dict) else {}
            if kind == 'session_meta':
                meta_id = payload.get('id') or payload.get('session_id')
                if isinstance(meta_id, str) and meta_id:
                    meta_ids.append(meta_id)
                    if native is None:
                        native = meta_id
                        cli_version = payload.get('cli_version')
                        thread_source = payload.get('thread_source')
                        source = payload.get('source') if isinstance(payload.get('source'), (str, dict)) else None
                    account = payload.get('creator_account_id')
                    if meta_id == native and isinstance(account, str) and account and account not in accounts:
                        accounts.append(account)
            elif kind == 'token_usage_record':
                response_id = payload.get('response_id')
                vector = _vector(payload.get('usage'))
                if not isinstance(response_id, str) or not response_id or vector is None:
                    invalid += 1
                    continue
                thread_vec = _vector(payload.get('thread_token_usage'))
                if thread_vec is not None:
                    last_thread = thread_vec
                owner = payload.get('thread_id')
                is_foreign = isinstance(owner, str) and owner and native is not None and owner != native
                bucket = foreign if is_foreign else seen
                if response_id in bucket:
                    if bucket[response_id] == vector:
                        dup_same += 1
                    else:
                        dup_conflict += 1
                    continue
                bucket[response_id] = vector
                if not is_foreign:
                    own_order.append(response_id)
                    tail_after_snapshot += 1
                    for field in FIELDS:
                        running[field] += vector[field]
                    if thread_vec is not None and thread_vec != running:
                        thread_inconsistent += 1
            elif kind == 'event_msg' and payload.get('type') == 'token_count':
                info = payload.get('info')
                if not isinstance(info, dict):
                    snap_null += 1
                    continue
                vector = _vector(info.get('total_token_usage'))
                if vector is None:
                    snap_invalid += 1
                    continue
                snapshots.append([record.get('timestamp'), *(vector[f] for f in FIELDS)])
                tail_after_snapshot = 0
                # gap = response mass the transcript's own counter has not (yet) applied to total_token_usage
                gap = sum(running[f] for f in FIELDS) - sum(vector.values())
                last = _vector(info.get('last_token_usage'))
                if gap > gap_prev:
                    gap_events += 1
                    if first_gap is None:
                        first_gap = len(snapshots) - 1
                    if last is not None and not any(last.values()):
                        zero_last += 1
                if gap == 0 and first_gap is None:
                    prefix_synced += 1
                if all(running[f] == vector[f] for f in FIELDS):
                    # the counter equals the field-wise sum of the responses so far, so the bucket reading
                    # (cached inside input, reasoning inside output) is exact wherever those counters are non-zero
                    synced_total += 1
                    cached_informative = cached_informative or vector['cached_input'] > 0
                    reasoning_informative = reasoning_informative or vector['reasoning_output'] > 0
                gap_prev = gap
    sums = _zero()
    for vector in seen.values():
        for field in FIELDS:
            sums[field] += vector[field]
    foreign_sums = _zero()
    for vector in foreign.values():
        for field in FIELDS:
            foreign_sums[field] += vector[field]
    maxima = _zero()
    resets = []
    for index, snap in enumerate(snapshots):
        for position, field in enumerate(FIELDS, start=1):
            maxima[field] = max(maxima[field], snap[position])
        if index and any(snap[p] < snapshots[index - 1][p] for p in range(1, len(FIELDS) + 1)):
            resets.append(index)
    final = dict(zip(FIELDS, snapshots[-1][1:])) if snapshots else None
    return {
        'file_name': path.name, 'file_size': size, 'file_sha256': sha.hexdigest() if sha else None,
        'name_id': name.group(1) if name else None,
        'native': native, 'meta_ids': sorted(set(meta_ids)), 'meta_records': len(meta_ids),
        'accounts': accounts, 'cli_version': cli_version, 'thread_source': thread_source, 'source': source,
        'lines': lines, 'malformed_lines': malformed,
        'response_ids': own_order, 'response_unique': len(own_order), 'response_invalid': invalid,
        'dup_same': dup_same, 'dup_conflict': dup_conflict,
        'foreign_unique': len(foreign), 'foreign_ids': sorted(foreign),
        'sums': sums, 'foreign_sums': foreign_sums,
        'snapshots': snapshots, 'snapshot_null_info': snap_null, 'snapshot_invalid': snap_invalid,
        'final': final, 'maxima': maxima, 'resets': resets, 'last_thread_usage': last_thread,
        'tail_responses': tail_after_snapshot, 'thread_inconsistent': thread_inconsistent,
        'gap_events': gap_events, 'gap_zero_last_events': zero_last, 'first_gap_snapshot': first_gap,
        'prefix_synced_snapshots': prefix_synced, 'synced_snapshots': synced_total,
        'sync_proves_input_includes_cached': cached_informative,
        'sync_proves_output_includes_reasoning': reasoning_informative,
    }


def semantics(facts: dict[str, Any]) -> dict[str, Any]:
    """Bucket semantics from the transcript's own counters. A snapshot whose total_token_usage equals the
    field-wise sum of the response records before it proves the reading wherever the subset counter is
    non-zero (cached inside input; reasoning inside output); None means no informative snapshot."""
    sums, final = facts['sums'], facts['final']
    return {
        'recomputed_equals_final': None if final is None else sums == final,
        'input_includes_cached': True if facts['sync_proves_input_includes_cached'] else None,
        'output_includes_reasoning': True if facts['sync_proves_output_includes_reasoning'] else None,
    }


# --- ledger side (read-only) ---------------------------------------------------------------------------------

def open_ro(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=1')
    return conn


def ledger_view(conn: sqlite3.Connection, host: str, native: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT stream_id, generation, tokens FROM v2_usage_records "
        "WHERE host=? AND provider='codex' AND native_session_id=? AND record_key='cumulative'", (host, native)).fetchone()
    cumulative = None
    owner = None
    if row is not None:
        raw = json.loads(row['tokens'])
        cumulative = {'input': raw.get('input_total', 0), 'cached_input': raw.get('cached_input', 0),
                      'cache_write_input': raw.get('cache_write', 0), 'output': raw.get('output', 0),
                      'reasoning_output': raw.get('reasoning', 0)}
        owner = (row['stream_id'], row['generation'])
    rows = {r['response_id']: {'input': r['input'], 'cached_input': r['cached_input'],
                               'cache_write_input': r['cache_write_input'], 'output': r['output'],
                               'reasoning_output': r['reasoning_output']}
            for r in conn.execute(
                'SELECT response_id,input,cached_input,cache_write_input,output,reasoning_output '
                'FROM v2_usage_codex_responses WHERE host=? AND native_session_id=?', (host, native))}
    identity = conn.execute(
        "SELECT account_id, account_source, conflict, cli_version FROM v2_usage_identity "
        "WHERE host=? AND provider='codex' AND native_session_id=?", (host, native)).fetchone()
    reasons: list[str] = []
    if owner is not None:
        state = conn.execute('SELECT reasons FROM v2_usage_state WHERE stream_id=? AND generation=?', owner).fetchone()
        reasons = json.loads(state['reasons']) if state else []
    return {'cumulative': cumulative, 'owner': owner, 'rows': rows, 'reasons': reasons,
            'identity': dict(identity) if identity else None}


def ledger_buckets(c: dict[str, int]) -> dict[str, int]:
    """Same four buckets as usage_rollup.codex_ledger_buckets: cached input is a subset of input."""
    return {'uncached_input': c['input'] - c['cached_input'], 'cache_read': c['cached_input'],
            'cache_write': c['cache_write_input'], 'output': c['output']}


def classify(rows_sum: dict[str, int], cumulative: dict[str, int] | None) -> str:
    """The rollup's CodexSession rule: any bucket where rows exceed the cumulative row is unverifiable."""
    if cumulative is None:
        return 'unverifiable'
    r, c = ledger_buckets(rows_sum), ledger_buckets(cumulative)
    if any(r[b] > c[b] for b in BUCKETS):
        return 'unverifiable'
    return 'reconciled' if all(r[b] == c[b] for b in BUCKETS) else 'partial'


def sum_rows(rows: Iterable[dict[str, int]]) -> dict[str, int]:
    total = _zero()
    for row in rows:
        for field in FIELDS:
            total[field] += row[field]
    return total


def redacted_path(facts: dict[str, Any], native: str | None) -> str:
    """``<yyyy/mm/dd>/rollout-<ts>-<native hash>.jsonl`` relative to the sessions root; the id is pseudonymised."""
    name = facts['file_name'].replace(native, digest(native)) if native else facts['file_name']
    return f"{facts.get('rel_dir', '')}/{name}"


# --- reconciliation row --------------------------------------------------------------------------------------

def reconcile(host: str, facts: dict[str, Any], ledger: dict[str, Any]) -> dict[str, Any]:
    """One table row. Every value is a count, a boolean or a pseudonym; the predicate fields are explicit."""
    native = facts['native'] or facts['name_id']
    rows = ledger['rows']
    row_sum = sum_rows(rows.values())
    final, cumulative = facts['final'], ledger['cumulative']
    t_ids, r_ids = set(facts['response_ids']), set(rows)
    sem = semantics(facts)
    # a per-response value drift shows as sums_equal False with the id sets equal
    sums_equal = facts['sums'] == row_sum
    snapshots = facts['snapshots']
    match_idx = None
    if cumulative is not None:
        want = [cumulative[f] for f in FIELDS]
        for index, snap in enumerate(snapshots):
            if snap[1:] == want:
                match_idx = index
                break
    ledger_vs_final = None
    if cumulative is not None and final is not None:
        diffs = [cumulative[f] - final[f] for f in FIELDS]
        ledger_vs_final = 'equal' if not any(diffs) else ('lower' if all(d <= 0 for d in diffs) else
                                                          ('higher' if all(d >= 0 for d in diffs) else 'mixed'))
    ledger_lt_final = ledger_vs_final == 'lower'
    ids_equal = t_ids == r_ids
    thread_final = facts['last_thread_usage']
    rows_equal_final = final is not None and row_sum == final
    rows_equal_thread = thread_final is not None and row_sum == thread_final
    ledger_le_thread = (cumulative is not None and thread_final is not None
                        and all(cumulative[f] <= thread_final[f] for f in FIELDS))
    ledger_eq_final = ledger_vs_final == 'equal'
    # the predicate: complete, consistent rows are the session's own total, and the ledger is a lower bound
    hit = bool(ids_equal and sums_equal and rows_equal_thread and facts['thread_inconsistent'] == 0
               and ledger_le_thread and facts['foreign_unique'] == 0 and facts['dup_conflict'] == 0
               and not facts['resets'] and facts['malformed_lines'] == 0)
    ratio = None
    if cumulative is not None:
        base = sum(ledger_buckets(cumulative).values())
        ratio = round(sum(ledger_buckets(row_sum).values()) / base, 4) if base else None
    first_ts = snapshots[0][0] if snapshots else None
    last_ts = snapshots[-1][0] if snapshots else None
    return {
        'host': host, 'native_hash': digest(native or ''),
        'stream_id': ledger['owner'][0] if ledger['owner'] else None,
        'generation_hash': digest(ledger['owner'][1]) if ledger['owner'] else None,
        'account_hash': digest(ledger['identity']['account_id']) if ledger['identity'] and ledger['identity']['account_id'] else None,
        'account_state': (ledger['identity']['account_source'] if ledger['identity'] else None),
        'transcript_path': redacted_path(facts, native),
        'cli_version': facts['cli_version'], 'thread_source': facts['thread_source'],
        'file_size': facts['file_size'], 'file_sha256': facts['file_sha256'],
        'meta_records': facts['meta_records'], 'lines': facts['lines'], 'malformed_lines': facts['malformed_lines'],
        'transcript_responses': facts['response_unique'], 'db_rows': len(rows),
        'ids_only_in_transcript': len(t_ids - r_ids), 'ids_only_in_rows': len(r_ids - t_ids),
        'ids_equal': ids_equal, 'row_sum_equals_transcript_sum': sums_equal,
        'dup_same': facts['dup_same'], 'dup_conflict': facts['dup_conflict'],
        'foreign_unique': facts['foreign_unique'], 'response_invalid': facts['response_invalid'],
        'snapshots': len(snapshots), 'snapshot_null_info': facts['snapshot_null_info'],
        'snapshot_invalid': facts['snapshot_invalid'],
        'counter_resets': len(facts['resets']), 'first_snapshot_ts': first_ts, 'last_snapshot_ts': last_ts,
        'input_includes_cached': sem['input_includes_cached'],
        'output_includes_reasoning': sem['output_includes_reasoning'],
        'transcript_recomputed_equals_final': sem['recomputed_equals_final'],
        'ledger_input': cumulative['input'] if cumulative else None,
        'ledger_cached': cumulative['cached_input'] if cumulative else None,
        'ledger_output': cumulative['output'] if cumulative else None,
        'final_input': final['input'] if final else None, 'final_cached': final['cached_input'] if final else None,
        'final_output': final['output'] if final else None,
        'rows_input': row_sum['input'], 'rows_cached': row_sum['cached_input'], 'rows_output': row_sum['output'],
        'ledger_vs_final': ledger_vs_final, 'ledger_matches_snapshot_index': match_idx,
        'rows_over_ledger_ratio': ratio, 'rollup_class': classify(row_sum, cumulative),
        'ledger_reasons': ','.join(ledger['reasons']),
        'rows_equal_transcript_final': rows_equal_final, 'rows_equal_thread_final': rows_equal_thread,
        'ledger_le_thread_final': ledger_le_thread, 'ledger_equals_final_snapshot': ledger_eq_final,
        'tail_responses_after_last_snapshot': facts['tail_responses'], 'thread_inconsistent': facts['thread_inconsistent'],
        'synced_snapshots': facts['synced_snapshots'],
        'gap_events': facts['gap_events'], 'gap_zero_last_events': facts['gap_zero_last_events'],
        'prefix_synced_snapshots': facts['prefix_synced_snapshots'],
        'predicate_rows_authoritative': hit,
    }


COLUMNS = [
    'host', 'native_hash', 'stream_id', 'generation_hash', 'account_hash', 'account_state', 'transcript_path',
    'cli_version', 'thread_source', 'file_size', 'file_sha256', 'meta_records', 'lines', 'malformed_lines',
    'transcript_responses', 'db_rows', 'ids_only_in_transcript', 'ids_only_in_rows', 'ids_equal',
    'row_sum_equals_transcript_sum', 'dup_same', 'dup_conflict', 'foreign_unique', 'response_invalid',
    'snapshots', 'snapshot_null_info', 'snapshot_invalid', 'counter_resets', 'first_snapshot_ts',
    'last_snapshot_ts', 'input_includes_cached', 'output_includes_reasoning',
    'transcript_recomputed_equals_final', 'ledger_input', 'ledger_cached', 'ledger_output', 'final_input',
    'final_cached', 'final_output', 'rows_input', 'rows_cached', 'rows_output', 'ledger_vs_final',
    'ledger_matches_snapshot_index', 'rows_over_ledger_ratio', 'rollup_class', 'ledger_reasons',
    'rows_equal_transcript_final', 'rows_equal_thread_final', 'ledger_le_thread_final',
    'ledger_equals_final_snapshot', 'tail_responses_after_last_snapshot', 'thread_inconsistent',
    'gap_events', 'gap_zero_last_events', 'prefix_synced_snapshots', 'synced_snapshots',
    'predicate_rows_authoritative',
]


# --- sample selection ----------------------------------------------------------------------------------------

def population(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every Codex native session whose rollup class is unverifiable, with its stratum fields (ledger only)."""
    rows: dict[tuple[str, str], dict[str, int]] = defaultdict(_zero)
    for r in conn.execute('SELECT host,native_session_id,input,cached_input,cache_write_input,output,reasoning_output '
                          'FROM v2_usage_codex_responses'):
        total = rows[(r['host'], r['native_session_id'])]
        for field in FIELDS:
            total[field] += r[field]
    out = []
    for r in conn.execute("SELECT host,native_session_id,stream_id,tokens FROM v2_usage_records "
                          "WHERE provider='codex' AND record_key='cumulative'"):
        key = (r['host'], r['native_session_id'])
        raw = json.loads(r['tokens'])
        cumulative = {'input': raw.get('input_total', 0), 'cached_input': raw.get('cached_input', 0),
                      'cache_write_input': raw.get('cache_write', 0), 'output': raw.get('output', 0),
                      'reasoning_output': raw.get('reasoning', 0)}
        if key not in rows or classify(rows[key], cumulative) != 'unverifiable':
            continue
        base = sum(ledger_buckets(cumulative).values())
        ratio = sum(ledger_buckets(rows[key]).values()) / base if base else float('inf')
        identity = conn.execute("SELECT account_id,account_source FROM v2_usage_identity WHERE host=? AND provider='codex' "
                                "AND native_session_id=?", key).fetchone()
        out.append({
            'host': key[0], 'native': key[1], 'stream_id': r['stream_id'], 'ledger_total': base, 'ratio': ratio,
            'account': 'known' if identity and identity['account_id'] else 'unknown',
            'ratio_bucket': '1-1.2' if ratio < 1.2 else ('1.2-2' if ratio < 2 else '>2'),
            'size': 'big' if base >= BIG_TOKENS else 'small',
        })
    return sorted(out, key=lambda s: (s['host'], s['native']))


def select_sample(pop: list[dict[str, Any]], *, n: int, seed: int) -> list[dict[str, Any]]:
    """Round-robin over the non-empty (host, account, ratio bucket, size) cells in seeded order; within a
    cell draw without replacement. Deterministic for a given population and seed."""
    rng = random.Random(seed)
    cells: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for item in pop:
        cells[(item['host'], item['account'], item['ratio_bucket'], item['size'])].append(item)
    order = sorted(cells)
    rng.shuffle(order)
    for key in order:
        cells[key].sort(key=lambda s: s['native'])
        rng.shuffle(cells[key])
    chosen: list[dict[str, Any]] = []
    while len(chosen) < n and any(cells[k] for k in order):
        for key in order:
            if cells[key] and len(chosen) < n:
                chosen.append(cells[key].pop())
    return chosen


# --- CLI -----------------------------------------------------------------------------------------------------

def find_transcripts(root: Path, ids: set[str]) -> dict[str, Path]:
    """Exact bounded lookup: rollouts live at most four directories under the sessions root."""
    found: dict[str, Path] = {}
    for depth in range(1, 5):
        pattern = '/'.join(['*'] * (depth - 1) + ['rollout-*.jsonl'])
        for path in root.glob(pattern):
            match = NAME_ID.search(path.name)
            if match and match.group(1) in ids:
                found.setdefault(match.group(1), path)
    return found


def cmd_transcripts(args: argparse.Namespace) -> int:
    ids = set(json.load(open(args.ids_file))) if args.ids_file else set(args.id or [])
    root = Path(args.root).expanduser()
    found = find_transcripts(root, ids)
    for native in sorted(ids):
        path = found.get(native)
        if path is None:
            print(json.dumps({'name_id': native, 'missing': True}))
            continue
        facts = analyze_transcript(path, with_hash=args.hash)
        facts['rel_dir'] = str(path.parent.relative_to(root))
        facts['host'] = args.host
        print(json.dumps(facts, separators=(',', ':')))
    return 0


def cmd_sample(args: argparse.Namespace) -> int:
    conn = open_ro(args.ledger)
    pop = population(conn)
    chosen = select_sample(pop, n=args.n, seed=args.seed)
    print(json.dumps({'population': len(pop), 'seed': args.seed, 'n': args.n, 'sample': chosen}, indent=1))
    return 0


def cmd_join(args: argparse.Namespace) -> int:
    conn = open_ro(args.ledger)
    out_rows = []
    missing = 0
    with open(args.facts) as handle:
        for line in handle:
            facts = json.loads(line)
            if facts.get('missing'):
                missing += 1
                continue
            host = facts['host']
            out_rows.append(reconcile(host, facts, ledger_view(conn, host, facts['native'] or facts['name_id'])))
    with open(args.out, 'w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(out_rows)
    print(json.dumps({'rows': len(out_rows), 'missing_transcripts': missing}))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('transcripts')
    p.add_argument('--root', default='~/.codex/sessions')
    p.add_argument('--id', action='append')
    p.add_argument('--ids-file', help='JSON list of native ids')
    p.add_argument('--hash', action='store_true', help='also sha256 each file')
    p.add_argument('--host', required=True, help='host label recorded in each facts line')
    p.set_defaults(func=cmd_transcripts)
    p = sub.add_parser('sample')
    p.add_argument('--ledger', required=True)
    p.add_argument('--n', type=int, default=30)
    p.add_argument('--seed', type=int, required=True)
    p.set_defaults(func=cmd_sample)
    p = sub.add_parser('join')
    p.add_argument('--ledger', required=True)
    p.add_argument('--facts', required=True)
    p.add_argument('--out', required=True)
    p.set_defaults(func=cmd_join)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
