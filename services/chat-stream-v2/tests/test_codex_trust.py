"""Persisted trust is locked, realpath-keyed and byte-preserving."""
import concurrent.futures
import fcntl
import threading
import tomllib

import pytest
from codex_trust import ensure_codex_trust
from tmux_transport import Tmux


def test_writer_realpath_idempotence_preserves_other_tables(tmp_path):
    home=tmp_path/'home';home.mkdir()
    target=tmp_path/'target';target.mkdir()
    alias=tmp_path/'alias';alias.symlink_to(target,target_is_directory=True)
    config=home/'config.toml'
    original=b'# keep comment\nmodel = "example"\n\n[unrelated]\nvalue = 17 # keep bytes\n'
    config.write_bytes(original)
    ensure_codex_trust(str(alias),code_home=str(home))
    first=config.read_bytes()
    assert first.startswith(original)
    assert tomllib.loads(first.decode())['projects'][str(target.resolve())]['trust_level']=='trusted'
    ensure_codex_trust(str(target),code_home=str(home))
    assert config.read_bytes()==first


@pytest.mark.parametrize('entry', ['trust_level = "untrusted" # keep comment\n', 'description = "existing"\n'])
def test_existing_project_changes_only_its_trust(tmp_path,entry):
    cwd=tmp_path/'cwd';cwd.mkdir();home=tmp_path/'home';home.mkdir()
    config=home/'config.toml'
    tail='\n[unrelated]\nvalue = "unchanged" # verbatim\n'
    config.write_text('[projects."'+str(cwd.resolve())+'"]\n'+entry+tail)
    ensure_codex_trust(str(cwd),code_home=str(home))
    assert config.read_text().endswith(tail)
    assert tomllib.loads(config.read_text())['projects'][str(cwd.resolve())]['trust_level']=='trusted'


def test_concurrent_writers_do_not_drop_or_duplicate_tables(tmp_path):
    home=tmp_path/'home'
    cwds=[tmp_path/('cwd'+str(i)) for i in range(8)]
    for p in cwds: p.mkdir()
    with concurrent.futures.ThreadPoolExecutor() as pool:
        list(pool.map(lambda cwd:ensure_codex_trust(str(cwd),code_home=str(home)), cwds*3))
    config=(home/'config.toml').read_text()
    assert set(tomllib.loads(config)['projects'])=={str(c.resolve()) for c in cwds}
    assert config.count('trust_level = "trusted"')==8


def test_writer_waits_for_lock_before_read_or_write(tmp_path):
    home=tmp_path/'home';home.mkdir();cwd=tmp_path/'cwd';cwd.mkdir()
    done=threading.Event()
    with (home/'config.toml.lock').open('a+b') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        thread=threading.Thread(target=lambda:(ensure_codex_trust(str(cwd),code_home=str(home)),done.set()))
        thread.start()
        try:
            assert not done.wait(.05)
            assert not (home/'config.toml').exists()
        finally: fcntl.flock(lock,fcntl.LOCK_UN)
    thread.join(2)
    assert done.is_set()


def test_transport_seeds_temporary_codex_home(tmp_path,monkeypatch):
    import asyncio
    monkeypatch.setenv('CODEX_HOME',str(tmp_path/'home'))
    cwd=tmp_path/'cwd';cwd.mkdir()
    asyncio.run(Tmux().pretrust_codex_cwd(str(cwd)))
    assert tomllib.loads((tmp_path/'home/config.toml').read_text())['projects'][str(cwd.resolve())]['trust_level']=='trusted'


def test_remote_writer_runs_on_target_with_quoted_cwd(monkeypatch):
    import asyncio
    import tmux_transport
    calls=[]
    class Process:
        returncode=0
        async def communicate(self, data):
            assert b'def ensure_codex_trust' in data
            return b'',b''
    async def create(*args,**kw):
        calls.append(args)
        return Process()
    monkeypatch.setattr(tmux_transport.asyncio,'create_subprocess_exec',create)
    monkeypatch.setattr(tmux_transport,'ssh_command',lambda target,command,**kw:['ssh',target,command])
    asyncio.run(Tmux(ssh_target='fixture-user@fixture-peer').pretrust_codex_cwd('/tmp/folder with spaces'))
    assert calls and calls[0][-1]=="python3 - '/tmp/folder with spaces'"


def test_spawn_pretrusts_effective_cwd_before_creating_pane(tmp_path,monkeypatch):
    import asyncio
    import launch
    from sessions import Sessions
    from spawnctl import SpawnCtl
    from store import Store
    from test_codex_ssh_free_readiness import CodexTui, NativeUserProof
    monkeypatch.setenv('CODEX_HOME',str(tmp_path/'home'))
    cwd=tmp_path/'launch-cwd';cwd.mkdir()
    class Pane(CodexTui):
        async def cwd_exists(self,path):
            return __import__('pathlib').Path(path).is_dir()
        async def pretrust_codex_cwd(self, path):
            await Tmux().pretrust_codex_cwd(path)
        async def new_session(self,*args,**kw):
            data=tomllib.loads((tmp_path/'home/config.toml').read_text())
            assert data['projects'][str(cwd.resolve())]['trust_level']=='trusted'
            await super().new_session(*args,**kw)
    async def run():
        store=Store(':memory:');store.start()
        try:
            pane=Pane();sessions=Sessions(store,tmux=pane,local_host='fixture-node')
            machine=launch.local_machine('fixture-node',cwd=str(tmp_path),codex_bin='/bin/echo',
                claude_bin='/bin/echo',projects_root=str(tmp_path/'projects'),agent_orch_bin_dir='/bin')
            ctl=SpawnCtl(store,sessions,tmux=pane,machine=machine,submission_proof=NativeUserProof())
            result=await ctl.spawn({'provider':'codex','cwd':str(cwd),'initial_prompt':'native user prompt',
                'objective':'Probe persisted cwd trust','session_name':'child','request_id':'trust-key'},'fixture-node')
            assert result['state']=='ready'
        finally: store.stop()
    asyncio.run(run())
