"""Fingerprint a collected Python case and its declared local fixtures."""
from __future__ import annotations

import ast
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re


@lru_cache(maxsize=4096)
def _parse_cached(path, _sha256, content):
    # Preserve the universal newline handling of Path.read_text().
    text = content.decode('utf-8').replace('\r\n', '\n').replace('\r', '\n')
    return text, ast.parse(text, filename=path)


def _parse(path):
    content = path.read_bytes()
    return _parse_cached(str(path), hashlib.sha256(content).digest(), content)


def _source(text, node):
    first = min([node.lineno, *(d.lineno for d in getattr(node, 'decorator_list', []))])
    return ''.join(text.splitlines(keepends=True)[first - 1:node.end_lineno])


def _fixture(node):
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return None
    for decorator in node.decorator_list:
        call = decorator if isinstance(decorator, ast.Call) else None
        target = call.func if call else decorator
        if ((isinstance(target, ast.Name) and target.id == 'fixture') or
                (isinstance(target, ast.Attribute) and target.attr == 'fixture')):
            keywords = {kw.arg: kw.value for kw in call.keywords} if call else {}
            named = keywords.get('name')
            auto = keywords.get('autouse')
            return (named.value if isinstance(named, ast.Constant) and isinstance(named.value, str) else node.name,
                    isinstance(auto, ast.Constant) and auto.value is True)
    return None


def _mark_fixtures(node):
    result = set()
    for call in ast.walk(node):
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == 'usefixtures':
            result.update(arg.value for arg in call.args if isinstance(arg, ast.Constant) and isinstance(arg.value, str))
    return result


def _args(node):
    result = {arg.arg for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)}
    for decorator in node.decorator_list:
        result.update(_mark_fixtures(decorator))
    for call in ast.walk(node):
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == 'getfixturevalue':
            if not call.args or not isinstance(call.args[0], ast.Constant) or not isinstance(call.args[0].value, str):
                raise ValueError('dynamic fixture identity cannot be fingerprinted')
            result.add(call.args[0].value)
    return result


def _plugin_paths(path, text, tree, suite_root):
    result = []
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not any(isinstance(t, ast.Name) and t.id == 'pytest_plugins' for t in node.targets):
            continue
        try:
            names = ast.literal_eval(node.value)
        except (ValueError, TypeError):
            raise ValueError('dynamic pytest plugins cannot be fingerprinted')
        if isinstance(names, str):
            names = [names]
        for name in names:
            if not isinstance(name, str):
                raise ValueError('invalid pytest plugin name')
            for base in (path.parent, suite_root):
                candidate = base.joinpath(*name.split('.')).with_suffix('.py')
                if candidate.is_file() and candidate not in result:
                    result.append(candidate)
                    break
    return result


def _test_import(path, node, root):
    if not isinstance(node, ast.ImportFrom):
        return None
    name = node.module or ''
    bases = [path.parent, root / 'tests', root]
    if node.level:
        base = path.parent
        for _ in range(node.level - 1):
            base = base.parent
        bases = [base]
    for base in bases:
        candidate = base.joinpath(*name.split('.')).with_suffix('.py') if name else None
        if candidate and candidate.is_file() and candidate.resolve().is_relative_to(root / 'tests'):
            return candidate.resolve()
    return None


def python_fingerprint(suite_root, node_id):
    root = Path(suite_root).resolve()
    head = node_id.split('[', 1)[0]
    file_name, *path = head.split('::')
    source = (root / file_name).resolve()
    if not source.is_relative_to(root) or not source.is_file() or not path:
        raise ValueError('unknown test source or identity')
    text, tree = _parse(source)
    current = tree
    class_nodes = []
    for part in path:
        choices = [n for n in current.body if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == part]
        if len(choices) != 1:
            raise ValueError('test identity cannot be resolved')
        current = choices[0]
        if isinstance(current, ast.ClassDef):
            class_nodes.append(current)
    if not isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
        raise ValueError('test identity is not a function')
    modules = []
    relative = source.parent.relative_to(root)
    ancestors = [root]
    for component in relative.parts:
        ancestors.append(ancestors[-1] / component)
    for directory in ancestors:
        conf = directory / 'conftest.py'
        if conf.is_file():
            ctext, ctree = _parse(conf)
            modules.append((conf, ctext, ctree))
            for plugin in _plugin_paths(conf, ctext, ctree, root):
                ptext, ptree = _parse(plugin)
                modules.append((plugin, ptext, ptree))
    modules.append((source, text, tree))
    primary = {path for path, _, _ in modules}
    imports = {}
    known_paths = set(primary)
    index = 0
    while index < len(modules):
        mod_path, _, mod_tree = modules[index]
        index += 1
        scope = mod_path.relative_to(root).as_posix()
        for statement in mod_tree.body:
            imported = _test_import(mod_path, statement, root)
            if imported is None:
                continue
            for alias in statement.names:
                if alias.name == '*':
                    continue
                imports.setdefault(scope, {})[alias.asname or alias.name] = (imported, alias.name)
            if imported not in known_paths:
                imported_text, imported_tree = _parse(imported)
                modules.append((imported, imported_text, imported_tree))
                known_paths.add(imported)
    fixtures, definitions, autouse, marked, parts = {}, {}, set(), set(), {}
    for mod_path, mod_text, mod_tree in modules:
        prefix = mod_path.relative_to(root).as_posix()
        namespace = definitions.setdefault(prefix, {})
        for node in mod_tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                namespace[node.name] = (prefix, mod_text, node)
                fixture = _fixture(node)
                if fixture and mod_path in primary:
                    fixtures[fixture[0]] = (prefix, mod_text, node)
                    if fixture[1]:
                        autouse.add(fixture[0])
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        namespace[target.id] = (prefix, mod_text, node)
                        if target.id == 'pytestmark':
                            parts[prefix + ':pytestmark'] = _source(mod_text, node)
                            marked.update(_mark_fixtures(node))
            selector_hook = (root.name == 'chat-stream-v2' and mod_path == root / 'tests/conftest.py'
                             and node.name == 'pytest_sessionfinish') if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else False
            if mod_path.name == 'conftest.py' and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith('pytest_') and node.name != 'pytest_collection_modifyitems' and not selector_hook:
                parts[prefix + ':' + node.name] = _source(mod_text, node)
            if mod_path == source and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in ('setup_module', 'teardown_module', 'setup_function', 'teardown_function'):
                parts[prefix + ':xunit:' + node.name] = _source(mod_text, node)
    for scope, bindings in imports.items():
        if root / scope not in primary:
            continue
        for alias, (target, original) in bindings.items():
            prefix = target.relative_to(root).as_posix()
            definition = definitions.get(prefix, {}).get(original)
            if definition and (declared := _fixture(definition[2])):
                fixtures[declared[0] if declared[0] != original else alias] = definition
    parts[file_name + ':' + '::'.join(path)] = _source(text, current)
    for cls in class_nodes:
        for decorator in cls.decorator_list:
            marked.update(_mark_fixtures(decorator))
        first = min([cls.lineno, *(d.lineno for d in cls.decorator_list)])
        parts[file_name + ':' + cls.name + ':declaration'] = ''.join(
            text.splitlines(keepends=True)[first - 1:cls.body[0].lineno - 1])
        for node in cls.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(isinstance(target, ast.Name) and target.id == 'pytestmark' for target in targets):
                    parts[file_name + ':' + cls.name + ':pytestmark'] = _source(text, node)
                    marked.update(_mark_fixtures(node))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in ('setUp', 'tearDown', 'setUpClass', 'tearDownClass', 'setup_method', 'teardown_method', 'setup_class', 'teardown_class'):
                parts[file_name + ':' + cls.name + ':' + node.name] = _source(text, node)
    needed = list(_args(current) | autouse | marked)
    visited = set()
    relevant_nodes = [(file_name, text, current)]
    for cls in class_nodes:
        relevant_nodes.extend((file_name, text, node) for node in (*cls.bases, *cls.decorator_list))
        relevant_nodes.extend((file_name, text, node) for node in cls.body
                              if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                              and node.name in ('setUp', 'tearDown', 'setUpClass', 'tearDownClass',
                                                'setup_method', 'teardown_method', 'setup_class', 'teardown_class'))
    while needed:
        name = needed.pop()
        if name in visited:
            continue
        visited.add(name)
        if name in fixtures:
            prefix, mod_text, fixture = fixtures[name]
            parts[prefix + ':fixture:' + name] = _source(mod_text, fixture)
            relevant_nodes.append((prefix, mod_text, fixture))
            needed.extend(_args(fixture))
    # Local helper definitions and constants referenced by test/fixture code
    # are inputs too; do not fingerprint the product implementation under test.
    checked = set()
    while relevant_nodes:
        scope, _, node = relevant_nodes.pop()
        for name in {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}:
            identity = (scope, name)
            if identity in checked:
                continue
            checked.add(identity)
            definition_row = definitions.get(scope, {}).get(name)
            if not definition_row and name in imports.get(scope, {}):
                target, original = imports[scope][name]
                prefix = target.relative_to(root).as_posix()
                parts[prefix + ':imported-file'] = hashlib.sha256(target.read_bytes()).hexdigest()
                definition_row = definitions.get(prefix, {}).get(original)
            if not definition_row:
                continue
            prefix, mod_text, definition = definition_row
            parts[prefix + ':dependency:' + name] = _source(mod_text, definition)
            if isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                relevant_nodes.append((prefix, mod_text, definition))
    payload = json.dumps({'node': node_id, 'inputs': sorted(parts.items())}, ensure_ascii=False,
                         separators=(',', ':')).encode()
    return hashlib.sha256(payload).hexdigest()


def javascript_fingerprint(repo_root, file_name):
    root = Path(repo_root).resolve()
    source = (root / file_name).resolve()
    if not source.is_relative_to(root / 'test') or not source.is_file() or source.suffix not in ('.js', '.ts'):
        raise ValueError('unknown JavaScript test file')
    pending, seen, parts = [source], set(), {}
    pattern = re.compile(r"(?:require\s*\(\s*|\bfrom\s+|\bimport\s*\(\s*)['\"](\.[^'\"]+)['\"]")
    joined = re.compile(r"\b\w+\.(?:join|resolve)\s*\(\s*__dirname\s*,\s*([^)]*)\)")
    literal = re.compile(r"['\"`]([^'\"`]+)['\"`]")
    def include(target):
        target = target.resolve()
        if not target.is_relative_to(root) or 'fixtures' not in target.parts:
            return
        if target.is_dir():
            files = [p for p in target.rglob('*') if p.is_file()]
            if len(files) > 10000:
                raise ValueError('fixture directory too large')
            for entry in files:
                resolved = entry.resolve()
                if not resolved.is_relative_to(root):
                    raise ValueError('fixture escapes repository')
                pending.append(resolved)
        elif target.is_file():
            pending.append(target)
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        data = path.read_bytes()
        parts[path.relative_to(root).as_posix()] = hashlib.sha256(data).hexdigest()
        if path.suffix not in ('.js', '.ts', '.cjs', '.mjs'):
            continue
        text = data.decode('utf-8')
        for relative in pattern.findall(text):
            target = (path.parent / relative).resolve()
            candidates = [target, *(target.with_suffix(ext) for ext in ('.js', '.ts', '.cjs', '.mjs', '.json')),
                          *(target / ('index' + ext) for ext in ('.js', '.ts', '.cjs', '.mjs', '.json'))]
            for candidate in candidates:
                if candidate.is_relative_to(root) and candidate.is_file() and (candidate.is_relative_to(root / 'test') or 'fixtures' in candidate.parts):
                    pending.append(candidate)
                    break
        for match in joined.findall(text):
            segments = literal.findall(match)
            if segments and 'fixtures' in '/'.join(segments):
                include(path.parent.joinpath(*segments))
        for value in literal.findall(text):
            if 'fixtures/' not in value:
                continue
            static = value.split('${', 1)[0].rstrip('/')
            include((root if static.startswith('test/') else path.parent) / static)
    payload = json.dumps({'file': file_name, 'inputs': sorted(parts.items())}, separators=(',', ':')).encode()
    return hashlib.sha256(payload).hexdigest()
