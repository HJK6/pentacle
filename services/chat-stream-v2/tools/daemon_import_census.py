#!/usr/bin/env python3
"""Deterministic, non-executing AST census of static and dynamic imports."""
from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath


@dataclass(frozen=True, order=True)
class ImportSite:
    path: str
    line: int
    column: int
    scope: str
    form: str
    module: str | None
    name: str | None = None
    alias: str | None = None


def census(source: str, path: str) -> list[ImportSite]:
    """Return import sites, including unresolved dynamic calls, without executing source.

    Relative imports are resolved relative to the supplied module path. Name
    aliases for importlib and its importer are resolved to a fixed point.
    """
    tree = ast.parse(source, filename=path)
    shadowed_scopes: set[tuple[str, ...]] = set()
    global_declarations: set[tuple[str, ...]] = set()
    scope_kinds: dict[tuple[str, ...], str] = {(): 'module'}
    evaluation_scope: dict[int, tuple[str, ...]] = {}

    def index_bindings(node: ast.AST, scope: tuple[str, ...] = ()) -> None:
        evaluation_scope[id(node)] = scope
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            if not isinstance(node, ast.Lambda) and node.name == 'globals':
                shadowed_scopes.add(scope)
            args = node.args
            # Defaults, decorators and annotations run in the enclosing scope.
            for child in (*getattr(node, 'decorator_list', ()), *args.defaults,
                          *(value for value in args.kw_defaults if value is not None)):
                index_bindings(child, scope)
            arguments = (*args.posonlyargs, *args.args, *args.kwonlyargs,
                         *((args.vararg,) if args.vararg else ()),
                         *((args.kwarg,) if args.kwarg else ()))
            for arg in arguments:
                if arg.annotation:
                    index_bindings(arg.annotation, scope)
            returns = getattr(node, 'returns', None)
            if returns:
                index_bindings(returns, scope)
            inner = (*scope, node.name if not isinstance(node, ast.Lambda) else '<lambda>')
            scope_kinds[inner] = 'function'
            if any(arg.arg == 'globals' for arg in arguments):
                shadowed_scopes.add(inner)
            body = node.body if not isinstance(node, ast.Lambda) else [node.body]
            for child in body:
                index_bindings(child, inner)
            return
        if isinstance(node, ast.ClassDef):
            if node.name == 'globals':
                shadowed_scopes.add(scope)
            for child in (*node.decorator_list, *node.bases, *node.keywords):
                index_bindings(child, scope)
            inner = (*scope, node.name)
            scope_kinds[inner] = 'class'
            for child in node.body:
                index_bindings(child, inner)
            return
        if ((isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and node.id == 'globals')
                or (isinstance(node, ast.alias) and (node.asname or node.name.split('.')[0]) == 'globals')):
            shadowed_scopes.add(scope)
        if isinstance(node, ast.Global) and 'globals' in node.names:
            global_declarations.add(scope)
        for child in ast.iter_child_nodes(node):
            index_bindings(child, scope)
    index_bindings(tree)
    if global_declarations & shadowed_scopes:
        shadowed_scopes.add(())

    def current_globals_visible(node: ast.AST) -> bool:
        actual = evaluation_scope.get(id(node), ())
        for i in range(len(actual) + 1):
            prefix = actual[:i]
            if prefix not in shadowed_scopes:
                continue
            # Python methods do not close over a class namespace.
            if (i < len(actual) and scope_kinds.get(prefix) == 'class'
                    and any(scope_kinds.get(actual[:j]) == 'function' for j in range(i + 1, len(actual) + 1))):
                continue
            return False
        return True
    # A name may be rebound in another scope. Keep every static binding: a
    # possible dynamic importer must not disappear because a later assignment
    # happens to use the same local spelling.
    aliases: dict[str, set[str]] = {'__import__': {'builtins.__import__'}}

    def expressions(node: ast.AST) -> set[str]:
        if isinstance(node, ast.Name):
            return aliases.get(node.id, {node.id})
        if isinstance(node, ast.Attribute):
            return {f'{base}.{node.attr}' for base in expressions(node.value)}
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == 'getattr' and len(node.args) == 2
                and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)):
            return {f'{base}.{node.args[1].value}' for base in expressions(node.args[0])}
        return set()

    bindings: list[tuple[str, ast.AST]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                name = item.asname or item.name.split('.')[0]
                aliases.setdefault(name, set()).add(item.name if item.asname else item.name.split('.')[0])
        elif isinstance(node, ast.ImportFrom) and not node.level:
            for item in node.names:
                aliases.setdefault(item.asname or item.name, set()).add(f'{node.module}.{item.name}')
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            bindings.extend((target.id, node.value) for target in targets if isinstance(target, ast.Name))
    # Only these two importer capabilities need propagation. Avoid expanding
    # arbitrary recursive attribute assignments such as x = x.y indefinitely.
    importers = {'importlib.import_module', 'builtins.__import__'}
    for _ in range(len(bindings) + 1):
        changed = False
        for name, value in bindings:
            found = expressions(value) & (importers | {'importlib', 'builtins'})
            bucket = aliases.setdefault(name, set())
            before = len(bucket)
            bucket.update(found)
            changed |= len(bucket) != before
        if not changed:
            break

    result: list[ImportSite] = []
    package = list(PurePosixPath(path).with_suffix('').parts[:-1])
    # Paths passed by the gate are relative to the Python import root.
    def relative(module: str | None, level: int) -> str | None:
        if not level:
            return module
        if level > len(package) + 1:
            return None
        prefix = package[:len(package) - level + 1]
        return '.'.join(prefix + ([module] if module else []))

    def walk(node: ast.AST, scope: tuple[str, ...] = ()) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            scope = (*scope, node.name)
        label = '.'.join(scope) or '<module>'
        def add(form: str, module: str | None, name: str | None = None, alias: str | None = None) -> None:
            result.append(ImportSite(path, node.lineno, node.col_offset, label, form, module, name, alias))
        if isinstance(node, ast.Import):
            for item in node.names:
                add('import', item.name, None, item.asname)
        elif isinstance(node, ast.ImportFrom):
            module = relative(node.module, node.level)
            for item in node.names:
                add('from' if module is not None else 'unresolved', module, item.name, item.asname)
        elif isinstance(node, ast.Call):
            # Computed access to an importer namespace may select an import
            # function; retain it as unresolved instead of losing the edge.
            if (isinstance(node.func, ast.Name) and node.func.id == 'getattr'
                    and len(node.args) >= 2 and expressions(node.args[0]) & {'importlib', 'builtins'}
                    and not (isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str))):
                add('unresolved', None)
            targets = expressions(node.func)
            for target in sorted(targets & importers):
                argument = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == 'name'), None)
                name = argument.value if isinstance(argument, ast.Constant) and isinstance(argument.value, str) else None
                if target == 'builtins.__import__':
                    level_kw = next((k.value for k in node.keywords if k.arg == 'level'), None)
                    level_arg = node.args[4] if len(node.args) > 4 else level_kw
                    unknown_args = any(isinstance(arg, ast.Starred) for arg in node.args) or any(k.arg is None for k in node.keywords)
                    if unknown_args or len(node.args) > 5 or (len(node.args) > 4 and level_kw is not None):
                        name = None
                    elif level_arg is None:
                        level = 0
                    elif isinstance(level_arg, ast.Constant) and type(level_arg.value) is int and level_arg.value >= 0:
                        level = level_arg.value
                    else:
                        level = None
                    if name is not None and not unknown_args and len(node.args) <= 5 and not (len(node.args) > 4 and level_kw is not None):
                        if level is None or name.startswith('.'):
                            name = None
                        elif level:
                            globals_kw = next((k.value for k in node.keywords if k.arg == 'globals'), None)
                            globals_arg = node.args[1] if len(node.args) > 1 else globals_kw
                            current_globals = (isinstance(globals_arg, ast.Call)
                                               and isinstance(globals_arg.func, ast.Name)
                                               and globals_arg.func.id == 'globals'
                                               and not globals_arg.args and not globals_arg.keywords
                                               and current_globals_visible(globals_arg)
                                               and not (len(node.args) > 1 and globals_kw is not None))
                            # Only current-scope globals and a known package can
                            # establish the relative import target.
                            name = relative(name or None, level) if current_globals and package and level <= len(package) else None
                elif name and name.startswith('.'):
                    package_arg = node.args[1] if len(node.args) > 1 else next(
                        (k.value for k in node.keywords if k.arg == 'package'), None)
                    package_name = package_arg.value if isinstance(package_arg, ast.Constant) and isinstance(package_arg.value, str) else None
                    try:
                        name = importlib.util.resolve_name(name, package_name) if package_name else None
                    except (ImportError, ValueError):
                        name = None
                add('dynamic' if name else 'unresolved', name)
        for child in ast.iter_child_nodes(node):
            walk(child, scope)
    walk(tree)
    return sorted(result, key=lambda site: (site.path, site.line, site.column, site.scope,
                                             site.form, site.module or '', site.name or '', site.alias or ''))


def main(argv: list[str] | None = None) -> int:
    """Print the census for a source file without executing that source."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--path', required=True, help='module path relative to its import root')
    parser.add_argument('--source', required=True, type=Path, help='Python source to inspect')
    args = parser.parse_args(argv)
    try:
        sites = census(args.source.read_text(encoding='utf-8'), args.path)
    except (OSError, UnicodeError, SyntaxError) as exc:
        print(f'[daemon-import-census] FAIL: {exc}', file=sys.stderr)
        return 1
    print(json.dumps([asdict(site) for site in sites], sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
