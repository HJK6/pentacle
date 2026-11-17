"""Read-only Mach-O and SBPL closure discovery for exact Seatbelt grants.

No paths are learned from denial logs. Failure to resolve or read any required
input is fatal; directory-wide fallback grants are never returned.
"""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess

OTOOL = '/usr/bin/otool'
class DependencyError(RuntimeError):
    pass


def _resolve(path, metadata):
    """Resolve component by component, retaining every symlink lookup path."""
    seen = set()
    def walk(p):
        p = Path(p)
        if not p.is_absolute():
            p = Path.cwd() / p
        current = Path(p.anchor)
        for component in p.parts[1:]:
            if component == '.':
                continue
            if component == '..':
                current = current.parent
                continue
            current = current / component
            try:
                mode = current.lstat().st_mode
            except OSError as exc:
                raise DependencyError('unresolved path: ' + str(current)) from exc
            if stat.S_ISLNK(mode):
                key = str(current)
                if key in seen or len(seen) >= 40:
                    raise DependencyError('cyclic or excessive symlink chain: ' + key)
                seen.add(key); metadata.add(key)
                try:
                    target = Path(os.readlink(current))
                except OSError as exc:
                    raise DependencyError('unreadable symlink: ' + key) from exc
                current = walk(target if target.is_absolute() else current.parent / target)
                seen.remove(key)
        return current
    result = walk(path)
    if not result.is_file():
        raise DependencyError('dependency is not an exact regular file: ' + str(result))
    return result


def _read(path):
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        raise DependencyError('unreadable dependency: ' + str(path)) from exc


def _otool(path, flag, run):
    try:
        out = run([OTOOL, flag, str(path)], capture_output=True, text=True,
                  timeout=20, cwd=str(Path(path).parent), close_fds=True, pass_fds=())
    except (OSError, subprocess.SubprocessError) as exc:
        raise DependencyError('otool unavailable or failed for ' + str(path)) from exc
    if out.returncode != 0 or not isinstance(out.stdout, str):
        raise DependencyError('otool failed for ' + str(path))
    return out.stdout


def _libraries(output):
    if not any(line.strip().endswith(':') for line in output.splitlines()):
        raise DependencyError('otool -L returned no Mach-O header')
    result = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.endswith(':'):
            continue  # file and architecture headers
        match = re.fullmatch(r'(.+?)\s+\((?:compatibility|current) version .+\)', line)
        if not match:
            raise DependencyError('unrecognized otool -L dependency row: ' + line)
        result.append(match.group(1))
    return result


def _rpaths(output):
    result = []
    expecting = False
    for line in output.splitlines():
        line = line.strip()
        if line.startswith('cmd '):
            if expecting:
                raise DependencyError('LC_RPATH has no readable path')
            expecting = line == 'cmd LC_RPATH'
        elif expecting and line.startswith('path '):
            match = re.fullmatch(r'path (.+) \(offset \d+\)', line)
            if not match:
                raise DependencyError('unrecognized LC_RPATH path')
            result.append(match.group(1)); expecting = False
    if expecting:
        raise DependencyError('LC_RPATH has no readable path')
    return result


def _install_names(output):
    """LC_ID_DYLIB is the file's identity, not a dependent library load."""
    result = set()
    expecting = False
    for line in output.splitlines():
        line = line.strip()
        if line.startswith('cmd '):
            if expecting:
                raise DependencyError('LC_ID_DYLIB has no readable name')
            expecting = line == 'cmd LC_ID_DYLIB'
        elif expecting and line.startswith('name '):
            match = re.fullmatch(r'name (.+) \(offset \d+\)', line)
            if not match:
                raise DependencyError('unrecognized LC_ID_DYLIB name')
            result.add(match.group(1)); expecting = False
    if expecting:
        raise DependencyError('LC_ID_DYLIB has no readable name')
    return result


def dependency_closure(executables, *, extra_roots=(), run=subprocess.run, max_depth=32):
    """Return exact real files and symlink-metadata paths, cycle/depth bounded.

    extra_roots contains {path, executable} records for dlopen modules such as
    _sqlite3.so or node addons. The executable is the loading process's binary,
    needed to resolve @executable_path correctly. Plain paths are also accepted
    as independent roots, with themselves as the executable anchor.
    """
    if max_depth < 0:
        raise DependencyError('negative dependency depth limit')
    metadata, files, inspected, visited = set(), {}, {}, set()
    roots = []
    for executable in executables:
        path = _resolve(executable, metadata)
        roots.append((path, path))
    for item in extra_roots:
        if isinstance(item, dict):
            path = _resolve(item['path'], metadata)
            anchor = _resolve(item['executable'], metadata)
        else:
            path = anchor = _resolve(item, metadata)
        if (anchor, anchor) not in roots:
            roots.append((anchor, anchor))
        roots.append((path, anchor))

    def expanded(reference, loader, anchor, inherited):
        if reference == '@loader_path' or reference.startswith('@loader_path/'):
            return [str(loader.parent) + reference[len('@loader_path'):]]
        if reference == '@executable_path' or reference.startswith('@executable_path/'):
            return [str(anchor.parent) + reference[len('@executable_path'):]]
        if reference.startswith('@rpath/'):
            return [str(Path(base) / reference[len('@rpath/'):]) for base in inherited]
        if reference.startswith('@') or not Path(reference).is_absolute():
            raise DependencyError('unresolved Mach-O token or relative dependency: ' + reference)
        return [reference]

    def visit(path, anchor, inherited, depth, chain):
        key = (str(path), str(anchor), tuple(inherited))
        # A cycle is not additional depth. The active chain is anchor-scoped.
        if str(path) in chain or key in visited:
            return
        if depth > max_depth:
            raise DependencyError('dependency closure depth exceeded at ' + str(path))
        visited.add(key)
        data = _read(path)
        files[str(path)] = hashlib.sha256(data).hexdigest()
        if str(path) not in inspected:
            loads = _otool(path, '-l', run)
            own_ids = _install_names(loads)
            libraries = [item for item in _libraries(_otool(path, '-L', run)) if item not in own_ids]
            inspected[str(path)] = (libraries, _rpaths(loads))
        libraries, raw_rpaths = inspected[str(path)]
        search = []
        for raw in raw_rpaths:
            for candidate in expanded(raw, path, anchor, inherited):
                # A search directory may be absent when another LC_RPATH is
                # valid. Never grant the directory; final files must resolve.
                # Preserve lexical opt/<formula> components until resolving
                # the final file so their symlink metadata is recorded.
                value = str(Path(candidate))
                if value not in search:
                    search.append(value)
        search += [p for p in inherited if p not in search]
        for reference in libraries:
            candidates = expanded(reference, path, anchor, search)
            resolved = None
            for candidate in candidates:
                if Path(candidate).exists():
                    resolved = _resolve(candidate, metadata)
                    break
            if resolved is None:
                raise DependencyError('unresolved dependency ' + reference + ' loaded by ' + str(path))
            visit(resolved, anchor, tuple(search), depth + 1, chain | {str(path)})
    for path, anchor in roots:
        inherited = []
        if path != anchor:
            # dlopen modules inherit the main executable's runpath stack.
            # Visit the anchor first even when only extra_roots named it.
            visit(anchor, anchor, (), 0, set())
            for raw in inspected[str(anchor)][1]:
                for candidate in expanded(raw, anchor, anchor, ()):
                    if candidate not in inherited:
                        inherited.append(candidate)
        visit(path, anchor, tuple(inherited), 0, set())
    # Package-manager updates during inspection cannot silently change the
    # inputs represented by the closure receipt.
    for path, sha in files.items():
        if hashlib.sha256(_read(path)).hexdigest() != sha:
            raise DependencyError('dependency changed during inspection: ' + path)
    return {'files': sorted(files), 'metadata_paths': sorted(metadata),
            'binaries': [{'path': p, 'sha256': files[p]} for p in sorted(files)],
            'roots': [{'path': str(p), 'executable': str(a)} for p, a in roots]}


def _tokens(source):
    """Small Scheme lexer: imports are literal strings, never evaluated code."""
    index = 0
    while index < len(source):
        char = source[index]
        if char.isspace(): index += 1; continue
        if char == ';':
            stop = source.find('\n', index); index = len(source) if stop < 0 else stop + 1; continue
        if source.startswith('#|', index):
            depth = 1; index += 2
            while depth and index < len(source):
                if source.startswith('#|', index): depth += 1; index += 2
                elif source.startswith('|#', index): depth -= 1; index += 2
                else: index += 1
            if depth: raise DependencyError('unterminated SBPL block comment')
            continue
        if char in '()': yield (char, char); index += 1; continue
        if char == '"':
            index += 1; value = ''
            while index < len(source) and source[index] != '"':
                if source[index] == '\\':
                    index += 1
                    if index == len(source): raise DependencyError('unterminated SBPL escape')
                    escape = source[index]
                    value += escape if escape in ('"', '\\') else '\\' + escape
                else: value += source[index]
                index += 1
            if index == len(source): raise DependencyError('unterminated SBPL string')
            index += 1; yield ('string', value); continue
        start = index
        while index < len(source) and not source[index].isspace() and source[index] not in '();"': index += 1
        yield ('symbol', source[start:index])


def import_closure(paths, *, max_depth=32):
    """Hash every literal transitive SBPL import, refusing unknown forms."""
    paths = list(paths)
    allowed_directories = {Path(path).resolve().parent for path in paths}
    records = {}
    def visit(path, depth):
        metadata = set(); path = _resolve(path, metadata)
        if path.suffix != '.sb' or not any(parent == path.parent or parent in path.parents for parent in allowed_directories):
            raise DependencyError('SBPL import outside declared profile directories: ' + str(path))
        if str(path) in records: return
        if depth > max_depth: raise DependencyError('SBPL import depth exceeded')
        data = _read(path)
        try: tokens = list(_tokens(data.decode('utf-8')))
        except UnicodeError as exc: raise DependencyError('SBPL import is not UTF-8') from exc
        records[str(path)] = {'path': str(path), 'sha256': hashlib.sha256(data).hexdigest()}
        for i, token in enumerate(tokens[:-1]):
            if token != ('(', '(') or tokens[i+1] != ('symbol', 'import'): continue
            j = i + 2; imports = []
            while j < len(tokens) and tokens[j] != (')', ')'):
                if tokens[j][0] != 'string': raise DependencyError('nonliteral SBPL import cannot be resolved')
                imports.append(tokens[j][1]); j += 1
            if not imports or j == len(tokens): raise DependencyError('malformed SBPL import')
            for child in imports:
                visit(Path(child) if Path(child).is_absolute() else path.parent / child, depth + 1)
    for path in paths: visit(path, 0)
    return [records[key] for key in sorted(records)]
