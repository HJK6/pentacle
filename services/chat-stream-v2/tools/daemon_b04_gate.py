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
ADMISSIONS_PATH = SERVICE / 'daemon_contract/b04_f1_admissions.json'
ADMISSIONS_SHA256 = 'd83833681df20f08ee00688a4065207b6d632ec78af0abc3ab1a02a1b1c34e08'
STORE_PATH = 'services/chat-stream-v2/store.py'


def require_signature_python() -> None:
    if sys.version_info[:2] != (3, 13):
        version = '.'.join(map(str, sys.version_info[:3]))
        raise ValueError(f'B04 admission signatures require the Python 3.13 AST format. Run with python3.13 (found {version})')


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
        root = ROOT / 'services' if parts[0] == '_shared' else SERVICE
        target = root.joinpath(*parts)
        local = target.with_suffix('.py').is_file() or (target / '__init__.py').is_file()
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


ADMITTABLE_KINDS = frozenset({
    'submit', 'call:submit', 'run', 'call:run', 'protected-callback-call',
    'protected-callback-statement', 'protected-callback-condition', 'recurring-call',
})
NEVER_ADMIT = F1_PRIMITIVES - {'submit', '_submit'}


def admission_digest(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _one_line(value) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value.splitlines() == [value]


def _production_path(value) -> bool:
    if not isinstance(value, str):
        return False
    path = Path(value)
    return (path.as_posix() == value and '..' not in path.parts and path.suffix == '.py'
            and (path.parts[:2] == ('services', 'chat-stream-v2') or value == 'services/_shared/specs_service.py')
            and not {'tests', 'tools', 'deploy'}.intersection(path.parts))


def _admission_counts(blocks: list) -> dict[str, Counter]:
    if not isinstance(blocks, list):
        raise ValueError('B04 F1 admissions must be a list')
    result = {}
    specs = set()
    for block in blocks:
        if not isinstance(block, dict) or set(block) != {'spec_id', 'spec_review_id', 'reason', 'scopes', 'sha256'}:
            raise ValueError('invalid B04 F1 admission block fields')
        payload = {k: v for k, v in block.items() if k != 'sha256'}
        if admission_digest(payload) != block['sha256']:
            raise ValueError('B04 F1 admission block digest mismatch')
        if not all(_one_line(block[key]) for key in ('spec_id', 'spec_review_id', 'reason')):
            raise ValueError('invalid B04 F1 admission metadata')
        if block['spec_id'] in specs or not isinstance(block['scopes'], list) or not block['scopes']:
            raise ValueError('duplicate or empty B04 F1 item')
        specs.add(block['spec_id'])
        scopes = set()
        for row in block['scopes']:
            if not isinstance(row, dict) or set(row) != {'path', 'scope', 'nodes'}:
                raise ValueError('invalid B04 F1 admission scope fields')
            path, scope, nodes = row['path'], row['scope'], row['nodes']
            if not _production_path(path) or not _one_line(scope) or (path, scope) in scopes:
                raise ValueError('invalid or duplicate B04 F1 scope')
            scopes.add((path, scope))
            if not isinstance(nodes, list) or not nodes:
                raise ValueError('empty B04 F1 admission scope')
            counts = Counter()
            for node in nodes:
                if not isinstance(node, dict) or set(node) != {'kind', 'sha256', 'count'}:
                    raise ValueError(f'invalid B04 F1 admission node in {scope}')
                kind, digest, count = node['kind'], node['sha256'], node['count']
                if (not isinstance(kind, str) or kind not in ADMITTABLE_KINDS
                        or not isinstance(digest, str) or len(digest) != 64
                        or any(c not in '0123456789abcdef' for c in digest)
                        or type(count) is not int or count < 1 or (scope, kind, digest) in counts):
                    raise ValueError(f'invalid B04 F1 admission kind, fingerprint or count in {scope}')
                counts[scope, kind, digest] = count
            result.setdefault(path, Counter()).update(counts)
    return result


def _load_admissions() -> dict[str, Counter]:
    raw = ADMISSIONS_PATH.read_bytes()
    if hashlib.sha256(raw).hexdigest() != ADMISSIONS_SHA256:
        raise ValueError('B04 F1 admissions digest mismatch')
    return _admission_counts(json.loads(raw))


def _primitive_sites(source: str, path: str, *, admissible: set | None = None) -> Counter[tuple[str, str, str]]:
    tree = ast.parse(source, filename=path)
    output: Counter[tuple[str, str, str]] = Counter()
    scope_types = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
    definitions: dict[tuple[str, ...], ast.AST] = {}
    callbacks: set[tuple[str, ...]] = set()
    writer_submits: dict[tuple[str, ...], set[tuple[str, str, str]]] = {}

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
        callback_names = {'callbacks'} if scope == ('run', 'reconcile_callbacks') and path.endswith('/main.py') else set()
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
                    if value.id in sqlite_names and name not in sqlite_names:
                        sqlite_names.add(name); changed = True
                    if value.id in callback_names and name not in callback_names:
                        callback_names.add(name); changed = True
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
            if (isinstance(item, ast.Call) and isinstance(item.func, ast.Attribute)
                    and isinstance(item.func.value, ast.Name) and item.func.value.id in callback_names
                    and item.func.attr in {'append', 'extend', 'insert'}):
                output[(label, 'recurring-reference-mutation', ast.dump(item, include_attributes=False))] += 1
            elif (isinstance(item, ast.AugAssign) and isinstance(item.op, ast.Add)
                  and isinstance(item.target, ast.Name) and item.target.id in callback_names):
                output[(label, 'recurring-reference-mutation', ast.dump(item, include_attributes=False))] += 1
            elif isinstance(item, ast.Assign) and any(
                    isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)
                    and target.value.id in callback_names for target in item.targets):
                output[(label, 'recurring-reference-mutation', ast.dump(item, include_attributes=False))] += 1
            if (scope == ('run', 'reconcile_callbacks') and path.endswith('/main.py') and isinstance(item, ast.Assign)
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
                        site = (label, 'recurring-call', ast.dump(call, include_attributes=False))
                        output[site] += 1
                        target = call.func
                        name = getattr(target, 'attr', None) or getattr(target, 'id', '')
                        if admissible is not None and not ({name} | aliases.get(name, set())) & NEVER_ADMIT:
                            admissible.add(site)
            if isinstance(item, ast.Call):
                dynamic = dynamic_primitive(item)
                if dynamic and dynamic != 'connect':
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
                if admissible is not None and account_wrapper(target):
                    admissible.add((label, 'call:run', ast.dump(item, include_attributes=False)))
                    admissible.add((label, 'run', ast.dump(target, include_attributes=False)))
                store_write = (admissible is not None and path == STORE_PATH and scope[:1] == ('Store',)
                               and primitive == 'submit' and isinstance(target, ast.Attribute)
                               and isinstance(target.value, ast.Name) and target.value.id == 'self')
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
                                    if (store_write and arg is item.args[0] and isinstance(arg, ast.Name)
                                            and key == (*scope, arg.id) and isinstance(definitions[key], ast.FunctionDef)):
                                        sites = writer_submits.setdefault(key, set())
                                        sites.add((label, 'call:submit', ast.dump(item, include_attributes=False)))
                                        sites.add((label, 'submit', ast.dump(target, include_attributes=False)))
                                    break
            if isinstance(item, ast.Attribute) and isinstance(item.ctx, ast.Load) and (item.attr in F1_PRIMITIVES or account_wrapper(item) or store_kv(item)):
                site = (label, item.attr, ast.dump(item, include_attributes=False))
                output[site] += 1
                if admissible is not None and account_wrapper(item):
                    admissible.add(site)
            elif isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load) and item.id in F1_PRIMITIVES:
                output[(label, item.id, ast.dump(item, include_attributes=False))] += 1
        context = {'aliases': aliases, 'sqlite_names': sqlite_names, 'sqlite_connect': sqlite_connect,
                   'queue_names': queue_names, 'thread_names': thread_names}
        for child in nested:
            name = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else '<lambda>'
            scan_scope(child, (*scope, name), context)
    scan_scope(tree, (), {'aliases': {}, 'sqlite_names': {'sqlite3'}, 'sqlite_connect': set(),
                          'queue_names': set(), 'thread_names': set()})
    # A callback fingerprint must not turn an existing scheduling operation into
    # an admissible writer operation merely by passing that callback to submit.
    primitive_scopes = {scope for scope, _, _ in output} if writer_submits else set()
    for key in callbacks:
        callback = definitions[key]
        label = '.'.join(key)
        short_writer = key in writer_submits and not any(
            scope == label or scope.startswith(label + '.') for scope in primitive_scopes)
        if short_writer:
            admissible.update(writer_submits[key])
        # Count callback operations individually: removing an admitted operation
        # must not look like a newly introduced whole-function fingerprint.
        for item in ast.walk(callback):
            if isinstance(item, ast.Call):
                kind = 'protected-callback-call'
            elif isinstance(item, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Return, ast.Raise, ast.Expr, ast.Delete, ast.Assert)):
                # The named scheduler list is compared per reference above.
                if (isinstance(item, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'callbacks' for t in item.targets)
                        and isinstance(item.value, (ast.List, ast.Tuple))):
                    continue
                kind = 'protected-callback-statement'
            elif isinstance(item, (ast.If, ast.While)):
                kind = 'protected-callback-condition'
                item = item.test
            else:
                continue
            site = (label, kind, ast.dump(item, include_attributes=False))
            output[site] += 1
            if admissible is not None:
                admissible.add(site)
    return output

def new_primitives(frozen: str, base: str, candidate: str, path: str, *, admissions=None) -> list[str]:
    old = _primitive_sites(frozen, path)
    merged = _primitive_sites(base, path)
    remaining = Counter(admissions)
    admissible = set() if remaining else None
    current = _primitive_sites(candidate, path, admissible=admissible)
    errors = []
    for site, count in sorted(current.items()):
        scope, kind, signature = site
        allowance = 0
        if admissible and site in admissible:
            digest = hashlib.sha256(signature.encode('utf-8')).hexdigest()
            allowance = remaining.pop((scope, kind, digest), 0)
            if allowance and count < old[site] + allowance:
                errors.append(f'{path}:{scope}: stale F1 admission count for {kind}')
        if count > max(old[site] + allowance, merged[site]):
            errors.append(f'{path}:{scope}: new F1 primitive {kind}')
    for scope, kind, digest in sorted(remaining):
        errors.append(f'{path}:{scope}: stale or ineligible F1 admission {kind} {digest}')
    return errors


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
        bound.update(child.name for child in nested if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
        for item in own:
            if isinstance(item, (ast.Import, ast.ImportFrom)):
                bound.update(a.asname or a.name.split('.')[0] for a in item.names
                             if not (isinstance(item, ast.ImportFrom) and item.module == 'builtins'
                                     and a.name == 'getattr' and (a.asname or a.name) == 'getattr'))
        getter_builtin = inherited['getter_builtin'] and 'getattr' not in bound
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
            if (getter_builtin and isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) and expr.func.id == 'getattr'
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
                if getter_builtin and isinstance(item.func, ast.Name) and item.func.id == 'getattr':
                    for resolved in target(item):
                        output[(label, resolved)] += 1
                    if (len(item.args) >= 2 and 'work_lanes' in receiver_targets(item.args[0])
                            and not (isinstance(item.args[1], ast.Constant)
                                     and isinstance(item.args[1].value, str))):
                        output[(label, 'unresolved work_lanes getattr')] += 1
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
        context = {'aliases': aliases, 'receivers': receivers, 'activity': activity_aliases,
                   'getter_builtin': getter_builtin}
        for child in nested:
            name = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else '<lambda>'
            scan_scope(child, (*scope, name), context)
    scan_scope(tree, (), {'aliases': {}, 'receivers': {}, 'activity': set(), 'getter_builtin': True})
    return output

def new_bulk_sites(frozen: str, base: str, candidate: str, path: str) -> list[str]:
    old, merged, current = (_bulk_sites(text, path) for text in (frozen, base, candidate))
    return [f'{path}:{scope}: new F2 bulk reference {kind}'
            for (scope, kind), count in sorted(current.items())
            if count > max(old[(scope, kind)], merged[(scope, kind)])]


# The registered scheduler is an intentionally closed assembly language.  Its
# individual members remain governed by the active exception rows.
def scheduler_grammar(source: str, active: str) -> list[str]:
    path = 'services/chat-stream-v2/main.py'
    error = f'{path}:run.reconcile_callbacks: F1 recurring callback grammar violation'
    tree = ast.parse(source, filename=path)
    runs = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'run']
    if len(runs) != 1:
        return [error]
    run = runs[0]
    callbacks = [n for n in run.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'reconcile_callbacks']
    registrations = [k.value for n in ast.walk(run) if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Name) and n.func.id == 'SessionReconciler'
                     for k in n.keywords if k.arg == 'on_reconcile_tick']
    uses = [n for n in ast.walk(run) if isinstance(n, ast.Name) and n.id == 'reconcile_callbacks']
    if (len(callbacks) != 1 or len(registrations) != 1 or len(uses) != 1
            or registrations[0] is not uses[0] or not isinstance(registrations[0].ctx, ast.Load)):
        return [error]
    fn = callbacks[0]
    if (fn.decorator_list or fn.args.posonlyargs or fn.args.args or fn.args.kwonlyargs
            or fn.args.vararg or fn.args.kwarg or fn.returns or fn.type_comment):
        return [error]
    rows = set(active.splitlines())
    members = [
        ('event_push.check_pin_drift', 'EventPush.check_pin_drift', 846),
        ('watch_wake.missed_wake_alarm', 'WatchWake.missed_wake_alarm', 846),
        ('watch_wake.tick', 'WatchWake.tick', 847),
        ('capacity_retry.tick', 'CapacityRetry.tick', 847),
        ('account_runtime.tick', 'AccountRuntime.tick', 847),
        ('queued_send_now.tick', 'QueuedSendNow.tick', 847),
    ]
    expected = [expr for expr, symbol, line in members if f'{path}:{line} {symbol}' in rows]
    restore = f'{path}:849 AssistantRestore.tick' in rows
    body = fn.body
    if len(body) not in (2, 3) or not isinstance(body[0], ast.Assign):
        return [error]
    init = body[0]
    if (len(init.targets) != 1 or not isinstance(init.targets[0], ast.Name)
            or init.targets[0].id != 'callbacks' or not isinstance(init.value, ast.List)
            or [ast.unparse(e) for e in init.value.elts] != expected
            or any(not isinstance(e, ast.Attribute) or not isinstance(e.value, ast.Name) for e in init.value.elts)):
        return [error]
    if len(body) == 3:
        optional = body[1]
        guard = ast.parse('server.assistant_restore is not None', mode='eval').body
        append = ast.parse('callbacks.append(server.assistant_restore.tick)').body[0]
        if (not isinstance(optional, ast.If) or optional.orelse
                or ast.dump(optional.test, include_attributes=False) != ast.dump(guard, include_attributes=False)
                or len(optional.body) != 1
                or (ast.dump(optional.body[0], include_attributes=False) != ast.dump(append, include_attributes=False)
                    if restore else not isinstance(optional.body[0], ast.Pass))):
            return [error]
    elif restore:
        return [error]
    dispatch = ast.parse('await run_reconcile_callbacks(*callbacks)').body[0]
    if ast.dump(body[-1], include_attributes=False) != ast.dump(dispatch, include_attributes=False):
        return [error]
    return []


# Identity sets describe values that can reach either protected boundary.  The
# environment follows lexical scopes and joins both feasible branch outcomes.
def identity_sites(source: str, path: str) -> Counter[tuple[str, str, str]]:
    tree = ast.parse(source, filename=path)
    sites: Counter[tuple[str, str, str]] = Counter()
    G, B, S, C, W, P, O = 'getter', 'builtins', 'sqlite', 'connect', 'work_lanes', 'bulk', 'other'
    protected = {S, C, W, P}

    def record(scope, kind, node):
        sites[('.'.join(scope) or '<module>', kind, ast.dump(node, include_attributes=False))] += 1

    def local_names(node):
        found = set()
        class Bindings(ast.NodeVisitor):
            def visit_Name(self, n):
                if isinstance(n.ctx, (ast.Store, ast.Del)): found.add(n.id)
            def visit_Import(self, n):
                found.update(a.asname or a.name.split('.')[0] for a in n.names)
            def visit_ImportFrom(self, n):
                found.update(a.asname or a.name for a in n.names)
            def visit_FunctionDef(self, n): found.add(n.name)
            visit_AsyncFunctionDef = visit_FunctionDef
            def visit_ClassDef(self, n): found.add(n.name)
            def visit_Lambda(self, n): pass
        visitor = Bindings()
        for stmt in node.body if isinstance(node.body, list) else []: visitor.visit(stmt)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            found.update(a.arg for a in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs))
            if node.args.vararg: found.add(node.args.vararg.arg)
            if node.args.kwarg: found.add(node.args.kwarg.arg)
        return found

    def scan(node, scope, inherited):
        env = {name: set(value) for name, value in inherited.items()}
        nested = []
        escaping_names = set()
        class Declarations(ast.NodeVisitor):
            def visit_Global(self, n): escaping_names.update(n.names)
            def visit_Nonlocal(self, n): escaping_names.update(n.names)
            def visit_FunctionDef(self, n): pass
            visit_AsyncFunctionDef = visit_FunctionDef
            def visit_ClassDef(self, n): pass
            def visit_Lambda(self, n): pass
        if isinstance(node.body, list):
            for stmt in node.body: Declarations().visit(stmt)
        if scope:
            for name in local_names(node): env[name] = {O}
            # A nonlocal/global declaration refers to an enclosing binding, not
            # a fresh local. Keep its inherited identity for branch/loop joins.
            for name in escaping_names:
                if name in inherited: env[name] = set(inherited[name])
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                for a in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
                    if a.arg == 'work_lanes': env[a.arg] = {W}

        def value(expr, current, emit=True):
            if isinstance(expr, ast.Lambda):
                if emit: scan(expr, (*scope, '<lambda>'), current)
                return {O}
            if isinstance(expr, (ast.Yield, ast.YieldFrom)):
                v = value(expr.value, current, emit) if expr.value else {O}
                if v & {S, C} and emit: record(scope, 'F1 SQLite opaque escape', expr)
                if v & {W, P} and emit: record(scope, 'F2 bulk opaque escape', expr)
                return {O}
            if isinstance(expr, ast.Name):
                return current.get(expr.id, {G} if expr.id == 'getattr' else ({W} if expr.id == 'work_lanes' else {O}))
            if isinstance(expr, ast.Attribute):
                base = value(expr.value, current, emit)
                if expr.attr == 'work_lanes': return {W}
                out = set()
                if B in base and expr.attr == 'getattr': out.add(G)
                if S in base and expr.attr == 'connect': out.add(C)
                if expr.attr in {'__dict__', '__getattr__', '__getattribute__'}:
                    if W in base and emit: record(scope, 'F2 bulk unresolved reflection', expr)
                    if S in base and emit: record(scope, 'F1 SQLite unresolved reflection', expr)
                if W in base and expr.attr in {'current', 'list'}:
                    out.add(P)
                    if emit: record(scope, 'F2 bulk reference', expr)
                return out or {O}
            if isinstance(expr, ast.Subscript):
                base = value(expr.value, current, emit)
                value(expr.slice, current, emit)
                if W in base and emit: record(scope, 'F2 bulk unresolved member', expr)
                if S in base and emit: record(scope, 'F1 SQLite unresolved member', expr)
                return {O}
            if isinstance(expr, (ast.Tuple, ast.List, ast.Set)):
                return set().union(*(value(e, current, emit) for e in expr.elts)) if expr.elts else {O}
            if isinstance(expr, ast.Starred): return value(expr.value, current, emit)
            if isinstance(expr, ast.IfExp):
                return value(expr.body, current, emit) | value(expr.orelse, current, emit)
            if isinstance(expr, ast.Call):
                function = value(expr.func, current, emit)
                args = [value(arg, current, emit) for arg in expr.args]
                kwargs = [value(k.value, current, emit) for k in expr.keywords]
                if G in function:
                    receivers = args[0] if args and not isinstance(expr.args[0], ast.Starred) else set().union(*args) if args else set()
                    packed = any(isinstance(a, ast.Starred) for a in expr.args) or any(k.arg is None for k in expr.keywords)
                    if packed and (receivers & protected or any(a & protected for a in args + kwargs)):
                        if emit: record(scope, 'F2 bulk or F1 SQLite unsupported reflection packing', expr)
                        return {P, C}
                    if receivers & {W, S}:
                        member = expr.args[1] if len(expr.args) > 1 else None
                        name = member.value if isinstance(member, ast.Constant) and isinstance(member.value, str) else None
                        result = set()
                        if W in receivers and (name is None or name in {'current', 'list', '__dict__', '__getattr__', '__getattribute__'}):
                            result.add(P)
                            if emit: record(scope, 'F2 bulk reference reflection', expr)
                        if S in receivers and (name is None or name in {'connect', '__dict__', '__getattr__', '__getattribute__'}):
                            result.add(C)
                            if name != 'connect' and emit: record(scope, 'F1 SQLite unresolved reflection', expr)
                        return result or {O}
                if C in function and emit: record(scope, 'F1 SQLite connect call', expr)
                if P in function and emit: record(scope, 'F2 bulk reference call', expr)
                shadowed_getattr = (isinstance(expr.func, ast.Name) and expr.func.id == 'getattr'
                                    and function == {O})
                if G not in function and not shadowed_getattr:
                    for arg in args + kwargs:
                        if arg & {S, C} and emit: record(scope, 'F1 SQLite opaque escape', expr)
                        if arg & {W, P} and emit: record(scope, 'F2 bulk opaque escape', expr)
                return {O}
            if isinstance(expr, ast.Dict):
                entries = [part for part in (*expr.keys, *expr.values) if part is not None]
                return set().union(*(value(part, current, emit) for part in entries)) if entries else {O}
            # Inspect nested expressions even when their resulting value is opaque.
            for child in ast.iter_child_nodes(expr):
                if isinstance(child, ast.expr): value(child, current, emit)
            return {O}

        def bind_target(target, identities, state):
            if isinstance(target, ast.Name):
                state[target.id] = set(identities)
            elif isinstance(target, ast.Starred):
                bind_target(target.value, identities, state)
            elif isinstance(target, (ast.Tuple, ast.List)):
                for item in target.elts: bind_target(item, identities, state)

        def block(statements, current):
            for stmt in statements:
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    nested.append((stmt, {k: set(v) for k, v in current.items()}))
                    current[stmt.name] = {O}
                elif isinstance(stmt, ast.Import):
                    for a in stmt.names:
                        current[a.asname or a.name.split('.')[0]] = {S if a.name == 'sqlite3' else B if a.name == 'builtins' else O}
                elif isinstance(stmt, ast.ImportFrom):
                    for a in stmt.names:
                        current[a.asname or a.name] = {G if stmt.module == 'builtins' and a.name == 'getattr' else C if stmt.module == 'sqlite3' and a.name == 'connect' else O}
                elif isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                    v = value(stmt.value, current) if stmt.value else {O}
                    for target in stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]:
                        if isinstance(target, ast.Name):
                            current[target.id] = set(v)
                            if target.id in escaping_names and v & protected:
                                record(scope, 'F1/F2 protected identity escape', stmt)
                        elif v & protected: record(scope, 'F1/F2 protected identity escape', stmt)
                elif isinstance(stmt, (ast.Return, ast.Yield, ast.YieldFrom)) and stmt.value:
                    v = value(stmt.value, current)
                    if v & {S, C}: record(scope, 'F1 SQLite opaque escape', stmt)
                    if v & {W, P}: record(scope, 'F2 bulk opaque escape', stmt)
                elif isinstance(stmt, (ast.Try, ast.TryStar)):
                    initial = {k: set(v) for k, v in current.items()}
                    body = block(stmt.body, {k: set(v) for k, v in initial.items()})
                    normal = block(stmt.orelse, {k: set(v) for k, v in body.items()})
                    exceptional = {key: initial.get(key, {O}) | body.get(key, {O})
                                   for key in initial.keys() | body.keys()}
                    branches = [normal]
                    for handler in stmt.handlers:
                        state = {k: set(v) for k, v in exceptional.items()}
                        if handler.type: value(handler.type, state)
                        if handler.name: state[handler.name] = {O}
                        branches.append(block(handler.body, state))
                    for key in set().union(*(branch.keys() for branch in branches)):
                        current[key] = set().union(*(branch.get(key, {O}) for branch in branches))
                    current = block(stmt.finalbody, current)
                elif isinstance(stmt, ast.Match):
                    subject = value(stmt.subject, current)
                    branches = [{k: set(v) for k, v in current.items()}]
                    for case in stmt.cases:
                        state = {k: set(v) for k, v in current.items()}
                        for pattern in ast.walk(case.pattern):
                            name = (pattern.name if isinstance(pattern, (ast.MatchAs, ast.MatchStar))
                                    else pattern.rest if isinstance(pattern, ast.MatchMapping) else None)
                            if name: state[name] = set(subject)
                        if case.guard: value(case.guard, state)
                        branches.append(block(case.body, state))
                    for key in set().union(*(branch.keys() for branch in branches)):
                        current[key] = set().union(*(branch.get(key, {O}) for branch in branches))
                elif isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
                    if isinstance(stmt, ast.While):
                        value(stmt.test, current)
                        iter_values = {O}
                    else:
                        iter_values = value(stmt.iter, current)
                    loop = {k: set(v) for k, v in current.items()}
                    if not isinstance(stmt, ast.While):
                        bind_target(stmt.target, iter_values, loop)
                    if not (isinstance(stmt, ast.While) and isinstance(stmt.test, ast.Constant)
                            and not stmt.test.value):
                        loop = block(stmt.body, loop)
                        for key in current.keys() | loop.keys():
                            current[key] = current.get(key, {O}) | loop.get(key, {O})
                    current = block(stmt.orelse, current)
                elif isinstance(stmt, ast.If):
                    value(stmt.test, current)
                    feasible = [stmt.body, stmt.orelse]
                    if isinstance(stmt.test, ast.Constant): feasible = [stmt.body if stmt.test.value else stmt.orelse]
                    branches = [block(part, {k: set(v) for k, v in current.items()}) for part in feasible]
                    for key in set().union(*(b.keys() for b in branches)):
                        current[key] = set().union(*(b.get(key, {O}) for b in branches))
                else:
                    for child in ast.iter_child_nodes(stmt):
                        if isinstance(child, ast.expr): value(child, current)
                        elif isinstance(child, ast.stmt): block([child], current)
            return current
        if isinstance(node.body, list): block(node.body, env)
        else: value(node.body, env)
        # Closure cells may be assigned after the nested definition.
        for child, captured in nested:
            for key, val in env.items(): captured.setdefault(key, set()).update(val)
            scan(child, (*scope, child.name), captured)
    scan(tree, (), {})
    return sites


def new_identity_sites(frozen: str, base: str, candidate: str, path: str) -> list[str]:
    old, merged, current = (identity_sites(text, path) for text in (frozen, base, candidate))
    return [f'{path}:{scope}: new {kind}' for (scope, kind, fingerprint), count in sorted(current.items())
            if count > max(old[(scope, kind, fingerprint)], merged[(scope, kind, fingerprint)])]

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
    require_signature_python()
    admissions = _load_admissions()
    for path in admissions:
        target = ROOT / path
        if not target.is_file() or target.resolve() != target:
            return [f'{path}: stale F1 admissions for missing or redirected file']
    errors = validate_census()
    errors.extend(check_anchors(base))
    errors.extend(scheduler_grammar((SERVICE / "main.py").read_text(),
                  (SERVICE / "daemon_contract/active/F1_protected_core.txt").read_text()))
    for path in CORE:
        frozen = _git_source(FROZEN, path)
        merged = _git_source(base, path)
        candidate = (SERVICE / path).read_text()
        errors.extend(new_imports(frozen, merged, candidate, path))
        errors.extend(new_feature_references(frozen, merged, candidate, path))
    paths = _changed_production(base) | admissions.keys()
    for path in sorted(paths):
        target = ROOT / path
        if target.is_file():
            frozen = _optional_git_source(FROZEN, path)
            merged = _optional_git_source(base, path)
            candidate = target.read_text()
            errors.extend(new_primitives(frozen, merged, candidate, path,
                                         admissions=admissions.get(path)))
            errors.extend(new_bulk_sites(frozen, merged, candidate, path))
            errors.extend(new_identity_sites(frozen, merged, candidate, path))
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
