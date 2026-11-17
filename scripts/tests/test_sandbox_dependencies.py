"""Synthetic Mach-O and SBPL fixtures only; no native otool proof."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import pytest

P=Path(__file__).resolve().parents[1]/'lib/sandbox_dependencies.py'
def module():
    spec=importlib.util.spec_from_file_location('sandbox_dependencies',P)
    m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=m;spec.loader.exec_module(m);return m

def runner(deps,rpaths=None):
    rpaths=rpaths or {}
    def run(argv,**kwargs):
        path=argv[-1]
        if argv[-2]=='-L': text=path+':\n'+''.join('\t'+x+' (compatibility version 1.0.0, current version 1.0.0)\n' for x in deps.get(path,[]))
        else: text=''.join('Load command 0\n cmd LC_RPATH\n cmdsize 48\n path '+x+' (offset 12)\n' for x in rpaths.get(path,[]))
        return subprocess.CompletedProcess(argv,0,text,'')
    return run

def file(path):
    path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'synthetic Mach-O');return str(path)

def test_recursive_rpath_loader_executable_cycle(tmp_path):
    m=module();exe=file(tmp_path/'bin/main');a=file(tmp_path/'lib/a.dylib');b=file(tmp_path/'lib/b.dylib')
    deps={exe:['@rpath/a.dylib'],a:['@loader_path/b.dylib'],b:['@executable_path/../lib/a.dylib']}
    result=m.dependency_closure([exe],run=runner(deps,{exe:['@executable_path/../lib']}))
    assert result['files']==sorted([exe,a,b])

def test_unresolved_fails(tmp_path):
    m=module();exe=file(tmp_path/'main')
    with pytest.raises(m.DependencyError):m.dependency_closure([exe],run=runner({exe:['@rpath/missing.dylib']}))

def test_import_hash_cycle(tmp_path):
    m=module();a=tmp_path/'bsd.sb';b=tmp_path/'system.sb'
    a.write_text('(version 1)\n(import "system.sb")\n');b.write_text('(import "bsd.sb")\n')
    result=m.import_closure([a])
    assert {row['path'] for row in result}=={str(a),str(b)}
    assert all(len(row['sha256'])==64 for row in result)

def test_symlink_rpath_and_binary_link_metadata(tmp_path):
    m=module();realexe=file(tmp_path/'Cellar/python/1/bin/python');realdep=file(tmp_path/'Cellar/sqlite/1/lib/libsqlite.dylib')
    link=tmp_path/'opt/sqlite';link.parent.mkdir();link.symlink_to(tmp_path/'Cellar/sqlite/1',target_is_directory=True)
    py=tmp_path/'venv/bin/python';py.parent.mkdir(parents=True);py.symlink_to(realexe)
    result=m.dependency_closure([py],run=runner({realexe:['@rpath/libsqlite.dylib']},{realexe:[str(link/'lib')]}))
    assert result['files']==sorted([realexe,realdep])
    assert result['metadata_paths']==sorted([str(py),str(link)])
    assert all(Path(p).is_file() for p in result['files'])

def test_extra_module_uses_loading_executable_anchor(tmp_path):
    m=module();exe=file(tmp_path/'bin/python');extension=file(tmp_path/'stdlib/_sqlite3.so');dep=file(tmp_path/'lib/sqlite.dylib')
    result=m.dependency_closure([exe],extra_roots=[{'path':extension,'executable':exe}],run=runner({extension:['@executable_path/../lib/sqlite.dylib']}))
    assert result['files']==sorted([exe,extension,dep])

def test_depth_bound_and_cycles(tmp_path):
    m=module();a=file(tmp_path/'a');b=file(tmp_path/'b');c=file(tmp_path/'c')
    with pytest.raises(m.DependencyError,match='depth exceeded'):
        m.dependency_closure([a],run=runner({a:[b],b:[c]}),max_depth=1)
    result=m.dependency_closure([a],run=runner({a:[b],b:[a]}),max_depth=1)
    assert result['files']==[a,b]

@pytest.mark.parametrize('reference',['@unknown/lib.dylib','relative.dylib','/definitely/missing/lib.dylib'])
def test_unresolved_reference_forms(tmp_path,reference):
    m=module();exe=file(tmp_path/'main')
    with pytest.raises(m.DependencyError):m.dependency_closure([exe],run=runner({exe:[reference]}))

def test_no_directory_or_write_grants_returned(tmp_path):
    m=module();exe=file(tmp_path/'main');dep=file(tmp_path/'lib/dylib')
    result=m.dependency_closure([exe],run=runner({exe:[dep]}))
    assert set(result)=={'files','metadata_paths','binaries','roots'}
    assert str(tmp_path/'lib') not in result['files']
    assert all(set(row)=={'path','sha256'} for row in result['binaries'])

def test_otool_failure_and_missing_executable(tmp_path):
    m=module();exe=file(tmp_path/'main')
    def bad(argv,**kwargs):return subprocess.CompletedProcess(argv,1,'','not Mach-O')
    with pytest.raises(m.DependencyError,match='otool failed'):m.dependency_closure([exe],run=bad)
    def missing(argv,**kwargs):raise FileNotFoundError('otool absent')
    with pytest.raises(m.DependencyError,match='otool unavailable'):m.dependency_closure([exe],run=missing)

def test_unreadable_file_fails_before_otool(tmp_path,monkeypatch):
    m=module();exe=file(tmp_path/'main');original=Path.read_bytes
    def read(path):
        if str(path)==exe:raise PermissionError('fixture unreadable')
        return original(path)
    monkeypatch.setattr(Path,'read_bytes',read)
    with pytest.raises(m.DependencyError,match='unreadable'):m.dependency_closure([exe],run=runner({}))

def test_import_missing_and_depth(tmp_path):
    m=module();a=tmp_path/'bsd.sb';b=tmp_path/'system.sb'
    a.write_text('(import "system.sb")');b.write_text('(version 1)')
    with pytest.raises(m.DependencyError,match='depth'):m.import_closure([a],max_depth=0)
    b.unlink()
    with pytest.raises(m.DependencyError,match='unresolved'):m.import_closure([a])

def test_import_comments_regex_strings_and_dynamic_failure(tmp_path):
    m=module();a=tmp_path/'a.sb';b=tmp_path/'b.sb'
    a.write_text('; (import "missing1.sb")\n#| (import "missing2.sb") |#\n(allow file-read* (regex #"/foo\\.bar"))\n(import "b.sb")')
    b.write_text('(version 1)')
    assert len(m.import_closure([a]))==2
    a.write_text('(import (string-append "system" ".sb"))')
    with pytest.raises(m.DependencyError,match='nonliteral'):m.import_closure([a])

def test_import_hash_changes_and_unreadable(tmp_path,monkeypatch):
    m=module();a=tmp_path/'a.sb';a.write_text('(version 1)')
    before=m.import_closure([a])[0]['sha256'];a.write_text('(version 1)\n;changed')
    assert m.import_closure([a])[0]['sha256']!=before
    monkeypatch.setattr(Path,'read_bytes',lambda path: (_ for _ in ()).throw(PermissionError('fixture')))
    with pytest.raises(m.DependencyError,match='unreadable'):m.import_closure([a])

def test_otool_runs_readonly_with_closed_descriptors(tmp_path):
    m=module();exe=file(tmp_path/'main');seen=[];base=runner({})
    def inspect(argv,**kw):seen.append((argv,kw));return base(argv,**kw)
    m.dependency_closure([exe],run=inspect)
    assert len(seen)==2
    for argv,kw in seen:
        assert argv[0]=='/usr/bin/otool' and argv[1] in ('-L','-l')
        assert kw['close_fds'] is True and kw['cwd']==str(tmp_path)

def test_import_cannot_escape_declared_profile_directory(tmp_path):
    m=module();a=tmp_path/'profiles/bsd.sb';a.parent.mkdir();outside=tmp_path/'outside.sb';outside.write_text('(version 1)')
    a.write_text('(import "../outside.sb")')
    with pytest.raises(m.DependencyError,match='outside declared'):m.import_closure([a])

def test_binary_drift_during_inspection_fails(tmp_path):
    m=module();exe=file(tmp_path/'main');base=runner({})
    def changed(argv,**kw):
        result=base(argv,**kw)
        if argv[1]=='-l':Path(exe).write_bytes(b'changed')
        return result
    with pytest.raises(m.DependencyError,match='changed during inspection'):m.dependency_closure([exe],run=changed)

def test_dlopen_module_inherits_anchor_rpaths(tmp_path):
    m=module();exe=file(tmp_path/'bin/python');extension=file(tmp_path/'stdlib/_sqlite3.so');dep=file(tmp_path/'lib/sqlite.dylib')
    result=m.dependency_closure([exe],extra_roots=[{'path':extension,'executable':exe}],
        run=runner({extension:['@rpath/sqlite.dylib']},{exe:['@executable_path/../lib']}))
    assert result['files']==sorted([exe,extension,dep])

def test_dylib_install_name_not_dependency(tmp_path):
    m=module();lib=file(tmp_path/'actual.dylib');base=runner({lib:['@rpath/other-name.dylib']})
    def own_id(argv,**kwargs):
        if argv[1]=='-l':return subprocess.CompletedProcess(argv,0,'Load command 0\n cmd LC_ID_DYLIB\n name @rpath/other-name.dylib (offset 24)\n','')
        return base(argv,**kwargs)
    assert m.dependency_closure([lib],run=own_id)['files']==[lib]
