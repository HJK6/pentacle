"""Attempt-owned SQLite checks use harmless sentinel files only."""
import json
from pathlib import Path
import subprocess
import sys
import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"

@pytest.mark.parametrize("case", ["plain", "uri", "readonly", "bytes", "relative", "outside", "symlink", "attach_literal", "attach_bound", "attach_cached", "vacuum", "backup"])
def test_attempt_refuses_preexisting(tmp_path, case):
    root = tmp_path / "attempt"
    root.mkdir()
    sentinel = root / "SENTINEL.db"
    sentinel.write_bytes(b"SENTINEL")
    outside = tmp_path / "outside.db"
    outside.write_bytes(b"SENTINEL")
    code = r'''import os, sqlite3, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
try:
    from sqlite_guard import _install_guard
except ImportError:
    # Baseline RED exercises the actual old policy, not a missing import.
    from read_timing import _install_guard as old_install
    def _install_guard(root, **kwargs): old_install(root)
root = Path(sys.argv[2]); case = sys.argv[3]
_install_guard(root, attempt=True)
s = root / "SENTINEL.db"
conn = sqlite3.connect(root / "fresh.db")
conn.execute("create table t(a)"); conn.close()
conn = sqlite3.connect(root / "fresh.db")
try:
    if case == "plain": sqlite3.connect(s)
    elif case == "uri": sqlite3.connect(s.as_uri(), uri=True)
    elif case == "readonly": sqlite3.connect(s.as_uri()+"?mode=ro", uri=True)
    elif case == "bytes": sqlite3.connect(os.fsencode(s))
    elif case == "relative":
        os.chdir(root); sqlite3.connect("SENTINEL.db")
    elif case == "outside": sqlite3.connect(root.parent / "outside.db")
    elif case == "symlink":
        (root / "link.db").symlink_to(root.parent / "outside.db"); sqlite3.connect(root / "link.db")
    elif case == "attach_literal": conn.execute("ATTACH DATABASE '%s' AS other" % s)
    elif case == "attach_bound": conn.execute("ATTACH DATABASE ? AS other", (str(s),))
    elif case == "attach_cached":
        conn.execute("ATTACH DATABASE ? AS other", (str(root / "fresh.db"),)); conn.execute("DETACH other")
        conn.execute("ATTACH DATABASE ? AS other", (str(s),))
    elif case == "vacuum": conn.execute("VACUUM INTO ?", (str(s),))
    elif case == "backup": conn.backup(sqlite3.connect(s))
except (sqlite3.Error, PermissionError):
    ledger = root / ".sqlite-refusals.jsonl"
    assert ledger.exists() and ledger.read_text(), "SQLite error without a guard refusal"
else:
    raise AssertionError("forbidden open accepted: " + case)
assert s.read_bytes() == b"SENTINEL"
'''
    result = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(TOOLS), str(root), case], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def run_code(root, code, *args, flags=("-I", "-B")):
    result = subprocess.run([sys.executable, *flags, "-c",
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "from pathlib import Path; import sqlite3, os; "
        "from sqlite_guard import _install_guard; root=Path(sys.argv[2]); "
        "_install_guard(root, attempt=True);\n" + code,
        str(TOOLS), str(root), *args], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("flags", [(), ("-I", "-B")])
def test_parent_child_create_reopen(tmp_path, flags):
    run_code(tmp_path, "c=sqlite3.connect(root/'parent.db'); c.execute('create table t(a)'); c.close()", flags=flags)
    run_code(tmp_path, """
os.environ.clear()
os.chdir('/')
c=sqlite3.connect(root/'parent.db'); c.execute('insert into t values (1)'); c.close()
c=sqlite3.connect(root/'child.db'); c.close()
""", flags=flags)
    run_code(tmp_path, "c=sqlite3.connect(root/'child.db'); c.close()", flags=flags)
    entries = [json.loads(line) for line in (tmp_path / '.sqlite-created.jsonl').read_text().splitlines()]
    assert len(entries) == 2
    for entry in entries:
        stat = Path(entry['path']).stat()
        assert (entry['device'], entry['inode']) == (stat.st_dev, stat.st_ino)


@pytest.mark.parametrize('operation', ['attach_literal', 'attach_bound', 'attach_many', 'attach_cached', 'vacuum', 'backup', 'symlink', 'memory'])
def test_registered_secondary_targets_allowed(tmp_path, operation):
    run_code(tmp_path, r'''
c=sqlite3.connect(root/'source.db'); c.execute('create table t(a)'); c.commit()
target=root/'target.db'
d=sqlite3.connect(target); d.close()
op=sys.argv[3]
if op == 'attach_literal': c.execute("ATTACH '%s' AS other" % target)
elif op == 'attach_bound': c.execute('ATTACH ? AS other', (str(target),))
elif op == 'attach_many': c.executemany('ATTACH ? AS other', [(str(target),)])
elif op == 'attach_cached':
    for _ in range(2):
        c.execute('ATTACH ? AS other', (str(target),)); c.execute('DETACH other')
elif op == 'vacuum': c.execute('VACUUM INTO ?', (str(target),))
elif op == 'backup':
    d=sqlite3.connect(target); c.backup(d); d.close()
elif op == 'symlink':
    (root/'link.db').symlink_to(target); sqlite3.connect(root/'link.db').close()
elif op == 'memory':
    for name, uri in [(':memory:', False), ('', False), ('file:one?mode=memory&cache=shared', True)]:
        sqlite3.connect(name, uri=uri).close()
c.close()
assert not (root/'.sqlite-refusals.jsonl').exists()
''', operation)


@pytest.mark.parametrize('operation', ['authorizer', 'extension', 'enable_extension', 'replace', 'hardlink', 'many', 'script', 'cached_literal', 'lowlevel'])
def test_escape_hatches_refused(tmp_path, operation):
    (tmp_path/'SENTINEL').write_bytes(b'SENTINEL')
    run_code(tmp_path, r'''
c=sqlite3.connect(root/'source.db'); c.execute('create table t(a)'); c.commit()
op=sys.argv[3]
try:
    if op == 'authorizer': c.set_authorizer(None)
    elif op == 'extension': c.load_extension(str(root/'SENTINEL'))
    elif op == 'enable_extension': c.enable_load_extension(True)
    elif op == 'replace':
        d=sqlite3.connect(root/'target.db'); d.close()
        (root/'replacement').write_bytes(b'SENTINEL'); (root/'replacement').replace(root/'target.db')
        sqlite3.connect(root/'target.db')
    elif op == 'hardlink':
        os.link(root/'source.db', root/'link.db'); sqlite3.connect(root/'link.db')
    elif op == 'many': c.executemany('ATTACH ? AS other', [(str(root/'SENTINEL'),)])
    elif op == 'script': c.executescript("ATTACH '%s' AS other;" % (root/'SENTINEL'))
    elif op == 'cached_literal':
        target=root/'target.db'; d=sqlite3.connect(target); d.close()
        sql="ATTACH '%s' AS other" % target
        c.execute(sql); c.execute('DETACH other')
        (root/'replacement').write_bytes(b'SENTINEL'); (root/'replacement').replace(target)
        c.execute(sql)
    elif op == 'lowlevel':
        import _sqlite3; _sqlite3.connect(root/'SENTINEL')
except sqlite3.Error:
    assert (root/'.sqlite-refusals.jsonl').read_text()
else: raise AssertionError('escape accepted')
''', operation)


def test_read_timing_guard_nested_under_attempt_guard(tmp_path):
    run_code(tmp_path, r'''
owned=root/'timing'; owned.mkdir()
sqlite3.connect(owned/'registered.db').close()
(owned/'SENTINEL').write_bytes(b'SENTINEL')
# Same legacy call that read_timing performs after startup installs attempt mode.
_install_guard(owned)
sqlite3.connect(owned/'registered.db').close()
c=sqlite3.connect(owned/'fresh.db')
try: c.execute('ATTACH ? AS other', (str(owned/'SENTINEL'),))
except sqlite3.Error: pass
else: raise AssertionError('legacy guard weakened attempt registry')
assert (root/'.sqlite-refusals.jsonl').read_text()
''')
