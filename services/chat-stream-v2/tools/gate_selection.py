"""Resolve unit test files affected by a stack, without importing the service."""
from __future__ import annotations

import ast
import os
import subprocess
from pathlib import Path

SERVICE = 'services/chat-stream-v2/'
# Non-import dependencies, with a reason for each entry.
LINKS = {}


def resolve(root: Path, changed: list[str], links: dict = LINKS) -> tuple[list[str], list[str]]:
    service = root / SERVICE
    files = []
    for directory, dirs, names in os.walk(service):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d != '__pycache__']
        files.extend(Path(directory) / name for name in names if name.endswith('.py'))
    modules = {'.'.join(p.relative_to(service).with_suffix('').parts): p for p in files}
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
                package = name.split('.')[:-node.level] if node.level else []
                base = '.'.join(package + ([node.module] if node.module else []))
                names = [base, *(f'{base}.{alias.name}' for alias in node.names)]
            for imported in names:
                if imported in modules:
                    reverse.setdefault(imported, set()).add(name)
    selected: set[str] = set()
    unmapped = []
    for path in changed:
        rel = path.removeprefix(SERVICE) if path.startswith(SERVICE) else ''
        module = '.'.join(Path(rel).with_suffix('').parts) if rel.endswith('.py') else ''
        seen = {module} if module else set()
        pending = list(seen)
        while pending:
            for dependent in reverse.get(pending.pop(), set()) - seen:
                seen.add(dependent)
                pending.append(dependent)
        tests = {str(modules[n].relative_to(service)) for n in seen
                 if n in modules and n.startswith('tests.test_') and modules[n].is_file()}
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
