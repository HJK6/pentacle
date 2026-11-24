"""Match named frozen B04 exceptions to AST statements and their scopes."""
from __future__ import annotations

import ast
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SERVICE = 'services/chat-stream-v2/'
FROZEN = 'f153ca8adce2a38e0c5092b2dc98ff5aec16e06e'


def source(revision: str, path: str) -> str | None:
    if revision == 'candidate':
        file = ROOT / path
        return file.read_text() if file.is_file() else None
    result = subprocess.run(['git', 'show', f'{revision}:{path}'], cwd=ROOT,
                            capture_output=True, text=True, timeout=20)
    return result.stdout if result.returncode == 0 else None


def parse_entries(text: str) -> list[tuple[str, int, str, str]]:
    entries = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        location, symbol = raw.split(' ', 1)
        path, number = location.rsplit(':', 1)
        if not path.startswith('services/'):
            path = SERVICE + path
        entries.append((path, int(number), symbol, raw))
    return entries


class Index:
    def __init__(self, text: str | None, path: str):
        self.path = path
        self.lines: list[ast.stmt] = []
        self.attributes: list[ast.Attribute] = []
        self.scopes: dict[int, str] = {}
        self.by_scope: dict[str, list[ast.stmt]] = defaultdict(list)
        self.memo: dict[tuple[str, str, str], int] = {}
        if text is None:
            return
        tree = ast.parse(text, filename=path)
        def visit(node: ast.AST, scope: tuple[str, ...] = ()) -> None:
            if isinstance(node, (ast.stmt, ast.Attribute)):
                label = '.'.join(scope) or '<module>'
                if isinstance(node, ast.stmt):
                    self.lines.append(node)
                else:
                    self.attributes.append(node)
                self.scopes[id(node)] = label
                self.by_scope[label].append(node)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                scope = (*scope, node.name)
            for child in ast.iter_child_nodes(node):
                visit(child, scope)
        visit(tree)

    def locate(self, line: int, symbol: str) -> tuple[str, str, str] | None:
        # Six separately owned scheduler callbacks occupy one list statement.
        # Bind each named receiver/member rather than the containing list.
        if self.path.endswith('/main.py') and line in {846, 847} and '.' in symbol:
            receiver, member = symbol.rsplit('.', 1)
            normalize = lambda value: value.replace('_', '').lower()
            attributes = [node for node in self.attributes if node.lineno == line and node.attr == member
                          and normalize(ast.unparse(node.value).split('.')[-1]) == normalize(receiver)]
            if len(attributes) != 1:
                return None
            node = attributes[0]
            return self.scopes[id(node)], type(node).__name__, ast.dump(node, include_attributes=False)
        matches = [node for node in self.lines if node.lineno <= line <= node.end_lineno]
        if not matches:
            return None
        node = min(matches, key=lambda n: (n.end_lineno - n.lineno, -n.col_offset))
        return self.scopes[id(node)], type(node).__name__, ast.dump(node, include_attributes=False)

    def count(self, selector: tuple[str, str, str]) -> int:
        if selector not in self.memo:
            scope, kind, fingerprint = selector
            self.memo[selector] = sum(type(n).__name__ == kind and ast.dump(n, include_attributes=False) == fingerprint
                                      for n in self.by_scope.get(scope, ()))
        return self.memo[selector]


def check_anchors(base: str) -> list[str]:
    errors = []
    cache: dict[tuple[str, str], Index] = {}
    def index(revision: str, path: str) -> Index:
        key = (revision, path)
        if key not in cache:
            cache[key] = Index(source(revision, path), path)
        return cache[key]
    for name in ('F1_protected_core', 'F2_projections'):
        frozen_path = ROOT / SERVICE / 'daemon_contract' / 'exceptions' / (name + '.txt')
        active_path = ROOT / SERVICE / 'daemon_contract' / 'active' / (name + '.txt')
        frozen = parse_entries(frozen_path.read_text())
        active_rows = parse_entries(active_path.read_text())
        frozen_counts = Counter(row[3] for row in frozen)
        active_counts = Counter(row[3] for row in active_rows)
        active_relative = active_path.relative_to(ROOT).as_posix()
        base_active = source(base, active_relative)
        if base_active is not None:
            base_counts = Counter(row[3] for row in parse_entries(base_active))
            if active_counts - base_counts:
                errors.append(f'{name}: exception reintroduced since merge base')
        if active_counts - frozen_counts:
            errors.append(f'{name}: added, duplicated or moved exception')
        grouped: dict[tuple[str, tuple[str, str, str]], list[str]] = defaultdict(list)
        for path, line, symbol, raw in frozen:
            selector = index(FROZEN, path).locate(line, symbol)
            if selector is None:
                errors.append(f'{name}: frozen anchor missing {path}:{line} {symbol}')
                continue
            grouped[(path, selector)].append(raw)
        for (path, selector), rows in grouped.items():
            old = index(FROZEN, path).count(selector)
            merged = index(base, path).count(selector)
            current = index('candidate', path).count(selector)
            if current > max(old, merged):
                errors.append(f'{name}: duplicated frozen selector {path}:{selector[0]}')
            retired = sum(1 for row in rows if not active_counts[row])
            remaining = max(0, old - retired)
            if current < remaining:
                errors.append(f'{name}: stale exception {path}:{selector[0]}')
            if retired and current > remaining:
                errors.append(f'{name}: removed exception still has selector {path}:{selector[0]}')
    return sorted(set(errors))
