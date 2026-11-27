"""Resolve unit test files affected by a stack, without importing the service."""
from __future__ import annotations

import ast
import os
import subprocess
from pathlib import Path

SERVICE = 'services/chat-stream-v2/'
# Non-import dependencies, with a reason for each entry.
LINKS = {
    # The checker model is loaded by file path, not imported by module name.
    SERVICE + 'tests/helpers/u4_checker_model.py': ['tests/test_u4_checker_contract.py'],
}


def resolve(root: Path, changed: list[str], links: dict = LINKS) -> tuple[list[str], list[str]]:
    service = root / SERVICE
    files = []
    for directory, dirs, names in os.walk(service):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d != '__pycache__']
        files.extend(Path(directory) / name for name in names if name.endswith('.py'))
    def module_key(path):
        parts = path.relative_to(service).with_suffix('').parts
        return '.'.join(parts[:-1] if parts[-1] == '__init__' else parts)

    modules = {module_key(p): p for p in files}
    aliases = {}
    for name in modules:
        aliases.setdefault(name, set()).add(name)
        if name.startswith('tests.'):
            aliases.setdefault(name.removeprefix('tests.'), set()).add(name)
            aliases.setdefault(name.rsplit('.', 1)[-1], set()).add(name)
    reverse: dict[str, set[str]] = {}
    for name, path in modules.items():
        try:
            tree = ast.parse(path.read_text(encoding='utf-8'))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                package = name.split('.') if path.name == '__init__.py' else name.split('.')[:-1]
                package = package[:1 - node.level] if node.level > 1 else (package if node.level else [])
                base = '.'.join(package + ([node.module] if node.module else []))
                names = [base, *(f'{base}.{alias.name}' for alias in node.names)]
            for imported in names:
                for target in aliases.get(imported, ()):
                    reverse.setdefault(target, set()).add(name)
    selected: set[str] = set()
    unmapped = []
    for path in changed:
        rel = path.removeprefix(SERVICE) if path.startswith(SERVICE) else ''
        module = module_key(service / rel) if rel.endswith('.py') else ''
        seen = {module} if module else set()
        pending = list(seen)
        while pending:
            for dependent in reverse.get(pending.pop(), set()) - seen:
                seen.add(dependent)
                pending.append(dependent)
        tests = {str(modules[n].relative_to(service)) for n in seen if n in modules
                 and modules[n].is_relative_to(service / 'tests') and modules[n].name.startswith('test_')}
        if rel and Path(rel).name == 'conftest.py':
            parent = (service / rel).parent
            tests.update(str(p.relative_to(service)) for p in files if p.is_relative_to(service / 'tests')
                         and p.is_relative_to(parent) and p.name.startswith('test_'))
        tests.update(links.get(path, []))
        if tests:
            selected.update(tests)
        else:
            unmapped.append(path)
    return sorted(selected), sorted(unmapped)


def stack(root: Path, base: str) -> tuple[list[str], list[str]]:
    paths = subprocess.check_output(['git', 'diff', '--name-only', '-z', f'{base}..HEAD'], cwd=root, text=True).split('\0')
    paths = [path for path in paths if path]
    return resolve(root, paths)
