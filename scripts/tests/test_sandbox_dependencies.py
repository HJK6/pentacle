"""Synthetic Mach-O and SBPL fixtures only; no native otool proof."""
import os
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
    assert set(result)=={'files','metadata_paths','binaries','roots','shared_cache_provided'}
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

SYSTEM_NAME='/usr/lib/libExample.dylib'
CACHE_DIR='System/Library/dyld'
def cache_fixture(tmp_path):
    root=tmp_path/'system'; cache=root/CACHE_DIR/'dyld_shared_cache_arm64e'
    file(cache); return root,cache

def test_shared_cache_receipt_and_unchanged_grants(tmp_path):
    import hashlib
    m=module();exe=file(tmp_path/'main');root,cache=cache_fixture(tmp_path)
    baseline=m.dependency_closure([exe],run=runner({}))
    result=m.dependency_closure([exe],run=runner({exe:[SYSTEM_NAME]}),_system_root=root)
    assert result['shared_cache_provided']==[{'name':SYSTEM_NAME,'caches':[{'path':str(cache),'sha256':hashlib.sha256(cache.read_bytes()).hexdigest()}]}]
    for key in ('files','metadata_paths','binaries','roots'):assert result[key]==baseline[key]

@pytest.mark.parametrize('name',[SYSTEM_NAME,'/System/Library/Frameworks/Test.framework/Test','/Library/Apple/System/Library/libX.dylib'])
def test_shared_cache_all_system_prefixes(tmp_path,name):
    m=module();exe=file(tmp_path/'main');root,cache=cache_fixture(tmp_path)
    assert m.dependency_closure([exe],run=runner({exe:[name]}),_system_root=root)['shared_cache_provided'][0]['name']==name

@pytest.mark.parametrize('name',['/opt/homebrew/opt/foo/lib/libfoo.dylib','/usr/local/lib/x.dylib',os.path.join(os.path.dirname(os.path.expanduser('~')), 'x', 'y.dylib'),'/usr/lib/../../etc/x','@rpath/missing','@loader_path/missing','@executable_path/missing'])
def test_cache_never_accepts_other_names(tmp_path,name):
    m=module();exe=file(tmp_path/'main');root,cache=cache_fixture(tmp_path)
    with pytest.raises(m.DependencyError):m.dependency_closure([exe],run=runner({exe:[name]}),_system_root=root)

def test_cache_absent_and_unreadable(tmp_path,monkeypatch):
    m=module();exe=file(tmp_path/'main');root,cache=cache_fixture(tmp_path)
    original=Path.open
    def denied(path,*args,**kwargs):
        if path==cache:raise PermissionError('synthetic denial')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'open',denied)
    with pytest.raises(m.DependencyError):m.dependency_closure([exe],run=runner({exe:[SYSTEM_NAME]}),_system_root=root)
    monkeypatch.setattr(Path,'open',original);cache.unlink()
    with pytest.raises(m.DependencyError):m.dependency_closure([exe],run=runner({exe:[SYSTEM_NAME]}),_system_root=root)

def test_system_file_resolves_normally_and_symlink_miss_refuses(tmp_path):
    m=module();exe=file(tmp_path/'main');root,cache=cache_fixture(tmp_path)
    disk=root/SYSTEM_NAME.lstrip('/');file(disk)
    result=m.dependency_closure([exe],run=runner({exe:[SYSTEM_NAME]}),_system_root=root)
    assert str(disk) in result['files'] and result['shared_cache_provided']==[]
    disk.unlink();disk.symlink_to(tmp_path/'missing')
    with pytest.raises(m.DependencyError):m.dependency_closure([exe],run=runner({exe:[SYSTEM_NAME]}),_system_root=root)

def test_system_parent_symlink_escape_refuses(tmp_path):
    m=module();exe=file(tmp_path/'main');root,cache=cache_fixture(tmp_path)
    (root/'usr').mkdir();(root/'usr/lib').symlink_to(tmp_path/'outside')
    with pytest.raises(m.DependencyError):m.dependency_closure([exe],run=runner({exe:[SYSTEM_NAME]}),_system_root=root)

@pytest.mark.parametrize('mode',['drift','overflow','dangling'])
def test_cache_bad_inventory_or_hash_fails(tmp_path,monkeypatch,mode):
    m=module();exe=file(tmp_path/'main');root,cache=cache_fixture(tmp_path)
    if mode=='overflow':
        for i in range(17):file(cache.parent/('dyld_shared_cache_arch'+str(i)))
    elif mode=='dangling':
        (cache.parent/'dyld_shared_cache_other').symlink_to(tmp_path/'absent')
    else:
        original=m._cache_hash;calls=[]
        def changed(path):
            result=original(path);calls.append(path)
            return result if len(calls)==1 else 'different'
        monkeypatch.setattr(m,'_cache_hash',changed)
    with pytest.raises(m.DependencyError):m.dependency_closure([exe],run=runner({exe:[SYSTEM_NAME]}),_system_root=root)

def test_multiple_main_caches_and_not_subcache(tmp_path):
    m=module();exe=file(tmp_path/'main');root,cache=cache_fixture(tmp_path)
    second=cache.parent/'dyld_shared_cache_x86_64';file(second)
    file(cache.parent/'dyld_shared_cache_arm64e.01');file(cache.parent/'dyld_shared_cache_arm64e.map')
    result=m.dependency_closure([exe],run=runner({exe:[SYSTEM_NAME,SYSTEM_NAME]}),_system_root=root)
    assert len(result['shared_cache_provided'])==1
    assert {r['path'] for r in result['shared_cache_provided'][0]['caches']}=={str(cache),str(second)}

def test_system_existing_unreadable_never_cache(tmp_path,monkeypatch):
    m=module();exe=file(tmp_path/'main');root,cache=cache_fixture(tmp_path)
    disk=root/SYSTEM_NAME.lstrip('/');file(disk);original=m._read
    def denied(path):
        if Path(path)==disk:raise m.DependencyError('unreadable fixture')
        return original(path)
    monkeypatch.setattr(m,'_read',denied)
    with pytest.raises(m.DependencyError,match='unreadable'):m.dependency_closure([exe],run=runner({exe:[SYSTEM_NAME]}),_system_root=root)

def test_shared_cache_does_not_change_profile_and_is_in_receipt(tmp_path):
    import json
    spec=importlib.util.spec_from_file_location('sandbox_gate_receipt',P.with_name('sandbox_gate.py'))
    gate=importlib.util.module_from_spec(spec);sys.modules[spec.name]=gate;spec.loader.exec_module(gate)
    m=module();exe=file(tmp_path/'main');root,cache=cache_fixture(tmp_path)
    baseline=m.dependency_closure([exe],run=runner({}))
    closure=m.dependency_closure([exe],run=runner({exe:[SYSTEM_NAME]}),_system_root=root)
    assert gate.profile_text([tmp_path],[tmp_path],tmp_path,dependencies=baseline)==gate.profile_text([tmp_path],[tmp_path],tmp_path,dependencies=closure)
    # Construct the context without running native launch/probe initialization.
    context=object.__new__(gate.Context)
    for key,value in dict(root=tmp_path,evidence=tmp_path,profile_path=tmp_path/'profile',profile_sha256='fixture',read_paths=[],write_paths=[],socket_dir=tmp_path,probes=[],controls=[],execution_mode='not-run',imports=[],dependencies=closure).items():setattr(context,key,value)
    body=context.write_receipt('INCOMPLETE','synthetic only')
    assert body['shared_cache_provided']==closure['shared_cache_provided']
    assert json.loads(context.receipt_path.read_text())['shared_cache_provided']==closure['shared_cache_provided']

@pytest.mark.parametrize('kind',['file','directory','external'])
def test_cache_symlinks_cannot_read_outside_fixed_os_locations(tmp_path,kind):
    m=module();exe=file(tmp_path/'main');root,cache=cache_fixture(tmp_path)
    target=(tmp_path if kind=='external' else root)/'Users/user/not-a-cache';file(target)
    if kind=='directory':
        cache.unlink();cache.parent.rmdir();cache.parent.symlink_to(target.parent)
        target.rename(target.parent/cache.name)
    else:
        cache.unlink();cache.symlink_to(target)
    with pytest.raises(m.DependencyError):m.dependency_closure([exe],run=runner({exe:[SYSTEM_NAME]}),_system_root=root)
