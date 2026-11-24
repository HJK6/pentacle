#!/usr/bin/env python3
"""Ratchet B04 protected-core and projection boundaries."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from collections import Counter
from pathlib import Path
import subprocess
import sys

from daemon_import_census import census
from daemon_b04_anchors import check_anchors

ROOT = Path(__file__).resolve().parents[3]
SERVICE = ROOT / 'services/chat-stream-v2'
FROZEN = 'f153ca8adce2a38e0c5092b2dc98ff5aec16e06e'
CORE = ('store.py', 'server.py', 'comms.py', 'sessions.py', 'ingest.py')
CENSUS_PATH = SERVICE / 'daemon_contract/b04_core_imports.json'
CENSUS_SHA256 = 'dc0907b598fb3f11a798d300ed25cc6a53773c9522a34b8aef5440722288a414'


def _owners() -> dict[str, list[str]]:
    raw = CENSUS_PATH.read_bytes()
    if hashlib.sha256(raw).hexdigest() != CENSUS_SHA256:
        raise RuntimeError('B04 frozen import census digest mismatch')
    data = json.loads(raw)
    if data.get('baseline') != FROZEN or data.get('composition_exempt') != 'main.py':
        raise RuntimeError('B04 frozen import census identity mismatch')
    modules = data.get('modules')
    allowed = {'core', 'F1', 'F2', 'F3', 'F5', 'F6'}
    if not isinstance(modules, dict) or any(not isinstance(v, list) or not v or
            len(v) != len(set(v)) or not set(v) <= allowed or
            ('core' in v and len(v) != 1) or ('F2' in v and 'F1' not in v)
            for v in modules.values()):
        raise RuntimeError('B04 import ownership mapping is invalid')
    return modules


def _module(site) -> str:
    name = site.module or ''
    if name == '_shared':
        return name + '.' + (site.name or '')
    if not name and site.form == 'from':
        return (site.name or '').split('.')[0]
    if name.startswith('_shared.'):
        return '.'.join(name.split('.')[:2])
    return name.split('.')[0]


def validate_census() -> list[str]:
    data = json.loads(CENSUS_PATH.read_text())
    owners = _owners()
    result = subprocess.run(['git', 'ls-tree', '-r', '--name-only', FROZEN, '--',
                             'services/chat-stream-v2', 'services/_shared'], cwd=ROOT,
                            capture_output=True, text=True, timeout=20, check=True)
    local = set()
    for raw in result.stdout.splitlines():
        if raw.startswith('services/chat-stream-v2/'):
            rest = raw.removeprefix('services/chat-stream-v2/').split('/')
            if len(rest) == 1 and rest[0].endswith('.py'):
                local.add(rest[0][:-3])
            elif len(rest) == 2 and rest[1] == '__init__.py':
                local.add(rest[0])
        elif raw.startswith('services/_shared/'):
            rest = raw.removeprefix('services/_shared/').split('/')
            if len(rest) == 1 and rest[0].endswith('.py'):
                local.add('_shared.' + rest[0][:-3])
    expected = []
    errors = []
    for path in CORE:
        for site in census(_git_source(FROZEN, path), path):
            module = _module(site)
            if module in local and module not in owners:
                errors.append(f'{path}:{site.line}: frozen local import {module} has no owner')
            if module in owners:
                expected.append({'path': path, 'line': site.line, 'scope': site.scope,
                                 'form': site.form, 'module': module, 'name': site.name,
                                 'alias': site.alias, 'owners': owners[module]})
    if data.get('sites') != expected:
        errors.append('B04 frozen import census does not match source')
    if set(data.get('module_evidence', {})) != set(owners):
        errors.append('B04 import owner evidence is incomplete')
    return errors


def _git_source(revision: str, name: str) -> str:
    path = f'services/chat-stream-v2/{name}'
    result = subprocess.run(['git', 'show', f'{revision}:{path}'], cwd=ROOT,
                            capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise RuntimeError(f'cannot read {revision}:{path}')
    return result.stdout


def _imports(source: str, name: str) -> Counter[tuple[str, str, str, str, str]]:
    owners = _owners()
    return Counter((s.scope, s.form, _module(s), s.name or '', s.alias or '')
                   for s in census(source, name)
                   if s.form == 'unresolved' or ('F1' in owners.get(_module(s), [])))


def new_imports(frozen: str, base: str, candidate: str, path: str) -> list[str]:
    old, merged, current = (_imports(text, path) for text in (frozen, base, candidate))
    owners = _owners()
    errors = []
    for site, count in sorted(current.items()):
        if site[1] == 'unresolved':
            errors.append(f'{path}:{site[0]}: unresolved dynamic import')
        elif count > max(old[site], merged[site]):
            rule = 'F2' if 'F2' in owners.get(site[2], []) else 'F1 feature'
            errors.append(f'{path}:{site[0]}: new {rule} import {site[2]}.{site[3]}')
    # New local modules require an explicit owner; do not silently treat them as
    # standard-library imports merely because they are absent from the census.
    for site in census(candidate, path):
        module = _module(site)
        if not module or module in owners or site.form == 'unresolved':
            continue
        parts = module.split('.')
        local = ((SERVICE / (parts[0] + '.py')).is_file() or
                 (parts[0] == '_shared' and len(parts) > 1 and
                  (ROOT / 'services/_shared' / (parts[1] + '.py')).is_file()))
        if local:
            errors.append(f'{path}:{site.line}: local import {module} has no owner')
    return errors


def _feature_references(source: str, path: str) -> Counter[tuple[str, str, str]]:
    tree = ast.parse(source, filename=path)
    owners = _owners()
    output: Counter[tuple[str, str, str]] = Counter()
    scope_types = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)

    def scan_scope(node: ast.AST, inherited: dict[str, set[str]], scope: tuple[str, ...]) -> None:
        own: list[ast.AST] = []
        nested: list[ast.AST] = []
        def collect(child: ast.AST) -> None:
            if isinstance(child, scope_types):
                nested.append(child)
                return
            own.append(child)
            for part in ast.iter_child_nodes(child):
                collect(part)
        for child in ast.iter_child_nodes(node):
            collect(child)
        aliases = {name: set(values) for name, values in inherited.items()}
        for item in own:
            if isinstance(item, ast.Import):
                for entry in item.names:
                    if 'F1' in owners.get(entry.name, []):
                        aliases.setdefault(entry.asname or entry.name.split('.')[0], set()).add(entry.name)
            elif isinstance(item, ast.ImportFrom):
                module = item.module or ''
                for entry in item.names:
                    key = f'{module}.{entry.name}' if module == '_shared' else module
                    if 'F1' in owners.get(key, []):
                        aliases.setdefault(entry.asname or entry.name, set()).add(key)
        def resolve(expr: ast.AST) -> set[str]:
            if isinstance(expr, ast.Name):
                return aliases.get(expr.id, set())
            if isinstance(expr, ast.Attribute):
                return resolve(expr.value)
            return set()
        assignments = [(target.id, item.value) for item in own
                       if isinstance(item, (ast.Assign, ast.AnnAssign)) and item.value
                       for target in (item.targets if isinstance(item, ast.Assign) else [item.target])
                       if isinstance(target, ast.Name)]
        for _ in range(len(assignments) + 1):
            changed = False
            for name, value in assignments:
                found = resolve(value)
                if found:
                    bucket = aliases.setdefault(name, set())
                    before = len(bucket)
                    bucket.update(found)
                    changed |= len(bucket) != before
            if not changed:
                break
        label = '.'.join(scope) or '<module>'
        for item in own:
            if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load):
                for module in aliases.get(item.id, ()):
                    output[(label, module, item.id)] += 1
        for child in nested:
            name = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else '<lambda>'
            scan_scope(child, aliases, (*scope, name))

    scan_scope(tree, {}, ())
    return output


def new_feature_references(frozen: str, base: str, candidate: str, path: str) -> list[str]:
    old, merged, current = (_feature_references(text, path) for text in (frozen, base, candidate))
    return [f'{path}:{scope}: new feature alias/reference {module}.{local}'
            for (scope, module, local), count in sorted(current.items())
            if count > max(old[(scope, module, local)], merged[(scope, module, local)])]


# The F1 primitive scan is a conservative admission boundary. It tracks both
# calls and first-class references, so moving a protected primitive into a
# wrapper or alias does not erase the reference.
F1_PRIMITIVES = frozenset({
    'submit', '_submit', 'create_task', 'ensure_future', 'call_later',
    'call_at', 'Timer', 'Thread', 'sleep', 'put_nowait', 'submit_chat', 'enqueue_chat',
})


def _primitive_sites(source: str, path: str) -> Counter[tuple[str, str, str]]:
    tree = ast.parse(source, filename=path)
    output: Counter[tuple[str, str, str]] = Counter()
    scope_types = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
    definitions: dict[tuple[str, ...], ast.AST] = {}
    callbacks: set[tuple[str, ...]] = set()

    def account_wrapper(node: ast.AST) -> bool:
        if not isinstance(node, ast.Attribute) or node.attr != 'run':
            return False
        receiver = ast.unparse(node.value).lower()
        return receiver.endswith('runtime') or (receiver == 'self' and path.endswith('/account_runtime.py'))

    def store_kv(node: ast.AST) -> bool:
        if not isinstance(node, ast.Attribute) or node.attr not in {'get', 'put'}:
            return False
        return ast.unparse(node.value).lower().split('.')[-1] in {'store', '_store'}

    def index(node: ast.AST, scope: tuple[str, ...] = ()) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            scope = (*scope, node.name)
            if not isinstance(node, ast.ClassDef):
                definitions[scope] = node
        for child in ast.iter_child_nodes(node):
            index(child, scope)
    index(tree)

    def scan_scope(node: ast.AST, scope: tuple[str, ...], inherited: dict) -> None:
        own: list[ast.AST] = []
        nested: list[ast.AST] = []
        def collect(child: ast.AST) -> None:
            if isinstance(child, scope_types):
                nested.append(child)
                return
            own.append(child)
            for part in ast.iter_child_nodes(child):
                collect(part)
        for child in ast.iter_child_nodes(node):
            collect(child)
        aliases = {name: set(values) for name, values in inherited['aliases'].items()}
        sqlite_names = set(inherited['sqlite_names'])
        sqlite_connect = set(inherited['sqlite_connect'])
        queue_names = set(inherited['queue_names'])
        thread_names = set(inherited['thread_names'])
        for item in own:
            if isinstance(item, ast.Import):
                sqlite_names.update(entry.asname or entry.name for entry in item.names if entry.name == 'sqlite3')
            elif isinstance(item, ast.ImportFrom):
                for entry in item.names:
                    if entry.name in F1_PRIMITIVES:
                        aliases.setdefault(entry.asname or entry.name, set()).add(entry.name)
                    if item.module == 'sqlite3' and entry.name == 'connect':
                        sqlite_connect.add(entry.asname or entry.name)
        bindings = [(target.id, item.value) for item in own
                    if isinstance(item, (ast.Assign, ast.AnnAssign)) and item.value
                    for target in (item.targets if isinstance(item, ast.Assign) else [item.target])
                    if isinstance(target, ast.Name)]
        name_aliases: dict[str, set[str]] = {}
        def dynamic_primitive(item: ast.AST) -> str | None:
            if not (isinstance(item, ast.Call) and isinstance(item.func, ast.Name)
                    and item.func.id == 'getattr' and len(item.args) >= 2):
                return None
            receiver = ast.unparse(item.args[0]).lower().split('.')[-1]
            attribute = item.args[1]
            if not (isinstance(attribute, ast.Constant) and isinstance(attribute.value, str)):
                return 'dynamic-getattr' if receiver in {'store', '_store', 'queue', '_queue', 'sqlite3'} | sqlite_names | queue_names else None
            name = attribute.value
            if name in F1_PRIMITIVES:
                return name
            if name in {'get', 'put'} and (receiver in {'store', '_store'} or receiver in queue_names or 'queue' in receiver):
                return name
            if name == 'connect' and receiver in sqlite_names:
                return name
            if name == 'start' and (receiver in thread_names or 'thread' in receiver or 'timer' in receiver):
                return name
            if name == 'run' and receiver.endswith('runtime'):
                return name
            return None
        for _ in range(len(bindings) + 1):
            changed = False
            for name, value in bindings:
                resolved = set()
                if isinstance(value, ast.Attribute):
                    if value.attr in F1_PRIMITIVES or account_wrapper(value) or store_kv(value):
                        resolved.add(value.attr)
                    if value.attr == 'connect' and isinstance(value.value, ast.Name) and value.value.id in sqlite_names:
                        resolved.add('connect')
                    if 'queue' in ast.unparse(value).lower() and name not in queue_names:
                        queue_names.add(name); changed = True
                elif isinstance(value, ast.Name):
                    resolved.update(aliases.get(value.id, ()))
                    if value.id in sqlite_connect:
                        resolved.add('connect')
                    if value.id in queue_names and name not in queue_names:
                        queue_names.add(name); changed = True
                    if value.id in thread_names and name not in thread_names:
                        thread_names.add(name); changed = True
                    name_aliases.setdefault(name, set()).add(value.id)
                elif isinstance(value, ast.Call):
                    dynamic = dynamic_primitive(value)
                    if dynamic:
                        resolved.add(dynamic)
                    creator = value.func.attr if isinstance(value.func, ast.Attribute) else (value.func.id if isinstance(value.func, ast.Name) else '')
                    if ({creator} | aliases.get(creator, set())) & {'Thread', 'Timer'} and name not in thread_names:
                        thread_names.add(name); changed = True
                if resolved:
                    bucket = aliases.setdefault(name, set())
                    before = len(bucket)
                    bucket.update(resolved)
                    changed |= before != len(bucket)
            if not changed:
                break
        label = '.'.join(scope) or '<module>'
        for item in own:
            if (scope and scope[-1] == 'reconcile_callbacks' and isinstance(item, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == 'callbacks' for target in item.targets)
                    and isinstance(item.value, (ast.List, ast.Tuple))):
                for reference in item.value.elts:
                    output[(label, 'recurring-reference', ast.dump(reference, include_attributes=False))] += 1
            if isinstance(item, (ast.While, ast.For, ast.AsyncFor)):
                loop_nodes = list(ast.walk(item))
                periodic = isinstance(item, ast.While) and (
                    isinstance(item.test, ast.Constant) and item.test.value is True or
                    '_running' in ast.dump(item.test, include_attributes=False))
                if periodic or any(isinstance(n, ast.Call) and isinstance(n.func, (ast.Name, ast.Attribute))
                                   and (n.func.id if isinstance(n.func, ast.Name) else n.func.attr)
                                   in {'sleep', 'wait', 'call_later', 'call_at'} for n in loop_nodes):
                    for call in (n for n in loop_nodes if isinstance(n, ast.Call)):
                        output[(label, 'recurring-call', ast.dump(call, include_attributes=False))] += 1
            if isinstance(item, ast.Call):
                dynamic = dynamic_primitive(item)
                if dynamic:
                    output[(label, 'getattr:' + dynamic, ast.dump(item, include_attributes=False))] += 1
                target = item.func
                primitive = target.attr if isinstance(target, ast.Attribute) else (target.id if isinstance(target, ast.Name) else '')
                possible = {primitive} | aliases.get(primitive, set())
                if account_wrapper(target):
                    possible.add('run')
                queue_put = primitive == 'put' and isinstance(target, ast.Attribute) and (
                    'queue' in ast.unparse(target.value).lower() or
                    isinstance(target.value, ast.Name) and target.value.id in queue_names)
                thread_start = primitive == 'start' and isinstance(target, ast.Attribute) and (
                    any(token in ast.unparse(target.value).lower() for token in ('thread', 'timer')) or
                    isinstance(target.value, ast.Name) and target.value.id in thread_names)
                alternate = ((isinstance(target, ast.Attribute) and target.attr == 'connect'
                              and isinstance(target.value, ast.Name) and target.value.id in sqlite_names)
                             or (isinstance(target, ast.Name) and (target.id in sqlite_connect or
                                 'connect' in aliases.get(target.id, set()))))
                if primitive in F1_PRIMITIVES or primitive in aliases or account_wrapper(target) or store_kv(target) or queue_put or thread_start or alternate:
                    output[(label, 'call:' + primitive, ast.dump(item, include_attributes=False))] += 1
                if item.args and possible & {'submit', '_submit', 'submit_chat', 'enqueue_chat', 'create_task', 'ensure_future', 'call_later', 'call_at', 'run'}:
                    for arg in item.args:
                        candidate = arg.func if isinstance(arg, ast.Call) else arg
                        names = {candidate.id} if isinstance(candidate, ast.Name) else ({candidate.attr} if isinstance(candidate, ast.Attribute) else set())
                        for _ in range(len(name_aliases) + 1):
                            before = len(names)
                            names.update(value for name in tuple(names) for value in name_aliases.get(name, ()))
                            if len(names) == before:
                                break
                        for name in names:
                            for length in range(len(scope), -1, -1):
                                key = (*scope[:length], name)
                                if key in definitions:
                                    callbacks.add(key)
                                    break
            if isinstance(item, ast.Attribute) and isinstance(item.ctx, ast.Load) and (item.attr in F1_PRIMITIVES or account_wrapper(item) or store_kv(item)):
                output[(label, item.attr, ast.dump(item, include_attributes=False))] += 1
            elif isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load) and item.id in F1_PRIMITIVES:
                output[(label, item.id, ast.dump(item, include_attributes=False))] += 1
        context = {'aliases': aliases, 'sqlite_names': sqlite_names, 'sqlite_connect': sqlite_connect,
                   'queue_names': queue_names, 'thread_names': thread_names}
        for child in nested:
            name = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else '<lambda>'
            scan_scope(child, (*scope, name), context)
    scan_scope(tree, (), {'aliases': {}, 'sqlite_names': {'sqlite3'}, 'sqlite_connect': set(),
                          'queue_names': set(), 'thread_names': set()})
    for key in callbacks:
        callback = definitions[key]
        # Count callback operations individually: removing an admitted operation
        # must not look like a newly introduced whole-function fingerprint.
        for item in ast.walk(callback):
            if isinstance(item, ast.Call):
                output[('.'.join(key), 'protected-callback-call', ast.dump(item, include_attributes=False))] += 1
            elif isinstance(item, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Return, ast.Raise, ast.Expr, ast.Delete, ast.Assert)):
                # The named scheduler list is compared per reference above.
                if (isinstance(item, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'callbacks' for t in item.targets)
                        and isinstance(item.value, (ast.List, ast.Tuple))):
                    continue
                output[('.'.join(key), 'protected-callback-statement', ast.dump(item, include_attributes=False))] += 1
            elif isinstance(item, (ast.If, ast.While)):
                output[('.'.join(key), 'protected-callback-condition', ast.dump(item.test, include_attributes=False))] += 1
    return output

def new_primitives(frozen: str, base: str, candidate: str, path: str,
                   adopted_f4: str = '') -> list[str]:
    old, merged, current = (_primitive_sites(text, path) for text in (frozen, base, candidate))
    adopted = _primitive_sites(adopted_f4, path) if adopted_f4 else Counter()
    return [f'{path}:{scope}: new F1 primitive {kind}'
            for (scope, kind, signature), count in sorted(current.items())
            if count > max(old[(scope, kind, signature)], merged[(scope, kind, signature)],
                           adopted[(scope, kind, signature)] if kind == 'call:connect' else 0)]


# Named F2 bulk views and traced callers in the frozen F2 sidecar. Generic
# method names are qualified below to avoid treating unrelated APIs as views.
BULK_NAMES = frozenset({
    'reconcile_index', 'reconcile_lane_lifecycle', 'reconcile_work_lane_episodes',
    'work_lane_rows', 'emit_if_changed', 'scan_work_observations',
    'reconcile_work_observations', 'open_questions', 'work_lane_snapshot',
    '_source_snapshot', 'build_frame', 'project_lanes', 'list_agent_questions', '_decoded_snapshot',
    'batched_time_metrics', 'effective_membership_ids_conn', '_prepare_records',
    '_prepare_file', 'resolved_route_dispatch_map', 'current_packets_conn',
    'work_lane_rows_conn', 'unblocks_conn', '_reconcile_lane_lifecycle_conn',
    '_lifecycle_lane_rows_conn', 'effective_membership_conn', 'questions_by_spec',
    'refresh_activity', 'assistant_composite_activity_summary',
    '_activity_summary_candidates', '_activity_summary_conn',
    'provenance_summary_conn', 'usage_provenance_summary', 'fetch_session_event_tail',
    'observe_claude_event',
})
GENERIC_BULK = {'current': {'work_lanes'}, 'list': {'work_lanes'},
                'refresh': {'worked', 'ledger'}, 'reconcile': {'tracking'}}


def _bulk_sites(source: str, path: str) -> Counter[tuple[str, str]]:
    tree = ast.parse(source, filename=path)
    output: Counter[tuple[str, str]] = Counter()
    scope_types = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
    receiver_roots = set().union(*GENERIC_BULK.values())

    def scan_scope(node: ast.AST, scope: tuple[str, ...], inherited: dict) -> None:
        own: list[ast.AST] = []
        nested: list[ast.AST] = []
        def collect(child: ast.AST) -> None:
            if isinstance(child, scope_types):
                nested.append(child)
                return
            own.append(child)
            for part in ast.iter_child_nodes(child):
                collect(part)
        for child in ast.iter_child_nodes(node):
            collect(child)
        bound = {item.id for item in own if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store)}
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            bound.update(arg.arg for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs))
            if node.args.vararg: bound.add(node.args.vararg.arg)
            if node.args.kwarg: bound.add(node.args.kwarg.arg)
        aliases = {name: set(values) for name, values in inherited['aliases'].items() if name not in bound}
        receivers = {name: set(values) for name, values in inherited['receivers'].items() if name not in bound}
        activity_aliases = set(inherited['activity']) - bound
        for item in own:
            if isinstance(item, ast.ImportFrom):
                for entry in item.names:
                    if entry.name in BULK_NAMES:
                        aliases.setdefault(entry.asname or entry.name, set()).add(entry.name)
        bindings = [(item.id, statement.value) for statement in own
                    if isinstance(statement, (ast.Assign, ast.AnnAssign)) and statement.value
                    for item in (statement.targets if isinstance(statement, ast.Assign) else [statement.target])
                    if isinstance(item, ast.Name)]
        def receiver_targets(expr: ast.AST) -> set[str]:
            tail = ast.unparse(expr).split('.')[-1]
            return ({tail} & receiver_roots) | receivers.get(tail, set())
        def target(expr: ast.AST) -> set[str]:
            if isinstance(expr, ast.Name):
                return aliases.get(expr.id, {expr.id} if expr.id in BULK_NAMES else set())
            if isinstance(expr, ast.Attribute):
                if expr.attr in BULK_NAMES:
                    return {expr.attr}
                result = set()
                if isinstance(expr.value, ast.Name) and expr.value.id in {'self', 'cls'}:
                    result.update(aliases.get(expr.attr, ()))
                if expr.attr in GENERIC_BULK:
                    result.update(receiver + '.' + expr.attr for receiver in receiver_targets(expr.value)
                                  if receiver in GENERIC_BULK[expr.attr])
                return result
            if (isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) and expr.func.id == 'getattr'
                    and len(expr.args) >= 2 and isinstance(expr.args[1], ast.Constant)
                    and isinstance(expr.args[1].value, str)):
                name = expr.args[1].value
                if name in BULK_NAMES:
                    return {name}
                if name in GENERIC_BULK:
                    return {receiver + '.' + name for receiver in receiver_targets(expr.args[0])
                            if receiver in GENERIC_BULK[name]}
            return set()
        def own_returns(function: ast.AST) -> list[ast.Return]:
            returns = []
            def gather(child: ast.AST) -> None:
                if isinstance(child, scope_types):
                    return
                if isinstance(child, ast.Return) and child.value:
                    returns.append(child)
                for part in ast.iter_child_nodes(child):
                    gather(part)
            for statement in function.body:
                gather(statement)
            return returns
        for _ in range(len(bindings) + len(nested) + 1):
            changed = False
            for name, value in bindings:
                tail = ast.unparse(value).split('.')[-1]
                receiver_values = ({tail} & receiver_roots) | (receivers.get(tail, set()) if isinstance(value, ast.Name) else set())
                if receiver_values:
                    bucket = receivers.setdefault(name, set()); before = len(bucket)
                    bucket.update(receiver_values); changed |= len(bucket) != before
                found = target(value)
                if found:
                    bucket = aliases.setdefault(name, set()); before = len(bucket)
                    bucket.update(found); changed |= len(bucket) != before
                if ((isinstance(value, ast.Attribute) and value.attr == 'assistant_composite_activity')
                        or (isinstance(value, ast.Name) and value.id in activity_aliases)) and name not in activity_aliases:
                    activity_aliases.add(name); changed = True
            for function in nested:
                if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)) or function.name == '_op':
                    continue
                found = set()
                for result in own_returns(function):
                    expr = result.value.value if isinstance(result.value, ast.Await) else result.value
                    if isinstance(expr, ast.Call):
                        found.update(target(expr.func))
                if found:
                    bucket = aliases.setdefault(function.name, set()); before = len(bucket)
                    bucket.update(found); changed |= len(bucket) != before
            if not changed:
                break
        label = '.'.join(scope) or '<module>'
        for item in own:
            if isinstance(item, (ast.Name, ast.Attribute)) and isinstance(item.ctx, ast.Load):
                for resolved in target(item):
                    output[(label, resolved)] += 1
            if isinstance(item, (ast.Assign, ast.AnnAssign)) and item.value:
                if isinstance(item.value, ast.Attribute) and item.value.attr == 'assistant_composite_activity':
                    output[(label, 'assistant_composite_activity alias')] += 1
            if isinstance(item, ast.Call):
                for resolved in target(item.func):
                    output[(label, resolved + ':call')] += 1
                if isinstance(item.func, ast.Name) and item.func.id == 'getattr':
                    for resolved in target(item):
                        output[(label, resolved)] += 1
                if ((isinstance(item.func, ast.Attribute) and item.func.attr == 'assistant_composite_activity'
                     or isinstance(item.func, ast.Name) and item.func.id in activity_aliases)
                        and not any(k.arg == 'input_ids' and not (isinstance(k.value, ast.Constant) and k.value.value is None)
                                    for k in item.keywords)):
                    output[(label, 'unbounded assistant_composite_activity')] += 1
                if isinstance(item.func, ast.Attribute) and item.func.attr == 'execute' and item.args:
                    arg = item.args[0]
                    if (isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                            and 'select' in arg.value.lower() and 'v2_assistant_composite_lanes' in arg.value.lower()
                            and 'where' not in arg.value.lower()):
                        output[(label, 'unbounded lane SQL')] += 1
        context = {'aliases': aliases, 'receivers': receivers, 'activity': activity_aliases}
        for child in nested:
            name = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else '<lambda>'
            scan_scope(child, (*scope, name), context)
    scan_scope(tree, (), {'aliases': {}, 'receivers': {}, 'activity': set()})
    return output

def new_bulk_sites(frozen: str, base: str, candidate: str, path: str) -> list[str]:
    old, merged, current = (_bulk_sites(text, path) for text in (frozen, base, candidate))
    return [f'{path}:{scope}: new F2 bulk reference {kind}'
            for (scope, kind), count in sorted(current.items())
            if count > max(old[(scope, kind)], merged[(scope, kind)])]


def _optional_git_source(revision: str, path: str) -> str:
    result = subprocess.run(['git', 'show', f'{revision}:{path}'], cwd=ROOT,
                            capture_output=True, text=True, timeout=20)
    return result.stdout if result.returncode == 0 else ''


def _changed_production(base: str) -> set[str]:
    paths: set[str] = set()
    for command in (['diff', '--name-only', FROZEN], ['diff', '--name-only', base],
                    ['ls-files', '--others', '--exclude-standard']):
        result = subprocess.run(['git', *command, '--', 'services/chat-stream-v2',
                                 'services/_shared/specs_service.py'], cwd=ROOT,
                                capture_output=True, text=True, timeout=20, check=True)
        paths.update(result.stdout.splitlines())
    return {p for p in paths if p.endswith('.py') and '/tests/' not in p
            and '/tools/' not in p and '/deploy/' not in p}


def check(base: str) -> list[str]:
    errors = validate_census()
    errors.extend(check_anchors(base))
    for path in CORE:
        frozen = _git_source(FROZEN, path)
        merged = _git_source(base, path)
        candidate = (SERVICE / path).read_text()
        errors.extend(new_imports(frozen, merged, candidate, path))
        errors.extend(new_feature_references(frozen, merged, candidate, path))
    for path in sorted(_changed_production(base)):
        target = ROOT / path
        if target.is_file():
            frozen = _optional_git_source(FROZEN, path)
            merged = _optional_git_source(base, path)
            candidate = target.read_text()
            # B02's already accepted offline preparation is an F4 exemption,
            # limited to the exact connections in its commissioned parent.
            adopted_f4 = (_optional_git_source('7f8b8d3e59665c16572ff55a1a1c16bcbd834df8', path)
                          if path.endswith('/daemon_migrations.py') else '')
            errors.extend(new_primitives(frozen, merged, candidate, path, adopted_f4))
            errors.extend(new_bulk_sites(frozen, merged, candidate, path))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', required=True)
    args = parser.parse_args()
    if len(args.base) != 40 or any(c not in '0123456789abcdef' for c in args.base):
        parser.error('--base must be a full lowercase commit SHA')
    try:
        errors = check(args.base)
    except (OSError, SyntaxError, RuntimeError, ValueError, UnicodeError) as exc:
        print(f'[daemon-b04] FAIL: {exc}', file=sys.stderr)
        return 1
    for error in errors:
        print(f'[daemon-b04] FAIL: {error}', file=sys.stderr)
    if errors:
        return 1
    print('[daemon-b04] PASS: B04 protected-core and projection selectors')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
