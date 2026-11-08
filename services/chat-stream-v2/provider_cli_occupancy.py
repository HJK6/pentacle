"""Read-only managed launcher attestation; unknown provider processes block."""
from __future__ import annotations

import ctypes
from collections import deque
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import sys

from provider_accounts import AccountError, Binding, _component
from provider_cli_pin import digest
from prockill import SESSION_ID_PROBE, canonical_start, identity_proof, parse_process_records, boot_matches


async def descriptors(runtime, host, provider):
    result = []
    for row in await runtime.store.list_sessions():
        if row.get("status") != "open" or row.get("host") != host or row.get("provider") != provider:
            continue
        binding = row.get("account_binding")
        if not isinstance(binding, dict):
            continue  # It cannot attest any process; the host will call it unknown.
        observer = row.get("observer_binding") or {}
        result.append({"stream_id": row["stream_id"], "session_generation": row.get("session_generation"),
            "binding": binding, "pane_pid": str(row.get("pane_pid") or ""),
            "pane_started_at": observer.get("pane_started_at"),
            "observer_generation": observer.get("generation"),
            "observer_executable": observer.get("executable")})
    return sorted(result, key=lambda row: row["stream_id"])


def _run(host, argv):
    try:
        value = host.run(argv,capture_output=True,text=True,timeout=10)
        output = value.stdout or ""
        if not isinstance(output,str) or len(output)>2_000_000:
            raise ValueError()
        return value.returncode,output
    except (OSError,subprocess.SubprocessError,ValueError):
        raise AccountError("account_repin_occupancy_unavailable") from None


def _native_pids(host, binding):
    code,output = _run(host,["ps","-U",str(os.getuid()),"-o","pid=,comm="])
    if code:
        raise AccountError("account_repin_occupancy_unavailable")
    names={binding.provider,Path(binding.executable).name,Path(binding.observer).name}
    for key,cell in host.config.get("cells",{}).items():
        if key.startswith(binding.provider+":") and isinstance(cell.get("executable"),str):
            names.add(Path(cell["executable"]).name)
            try:names.add(Path(_resolve(cell['executable'])[0]).name)
            except (OSError,ValueError):pass
    for known in host.registry.bindings():
        if known.provider==binding.provider:
            names.update((Path(known.executable).name,Path(known.observer).name))
            for path in (known.executable,known.observer):
                try:names.add(Path(_resolve(path)[0]).name)
                except (OSError,ValueError):pass
    native=set()
    for line in output.splitlines():
        parts=line.strip().split(None,1)
        if len(parts)!=2 or not parts[0].isdecimal():
            raise AccountError("account_repin_occupancy_unavailable")
        command=parts[1]
        # The prefix also conservatively handles older bounded test/adapter ps
        # output that included args. Such a row is never a managed exclusion.
        if Path(command).name in names or command.split(None,1)[0] in names:
            native.add(int(parts[0]))
    return native


def _records(host):
    code,output=_run(host,["ps","-eo","pid=,ppid=,uid=,pgid=,sid=,lstart=,command="])
    if code:
        code,output=_run(host,[sys.executable,"-c",SESSION_ID_PROBE])
    if code:
        raise AccountError("account_repin_occupancy_unavailable")
    return parse_process_records(output)


def _boot(host):
    for argv in (["cat","/proc/sys/kernel/random/boot_id"],["sysctl","-n","kern.boottime"]):
        try:
            code,output=_run(host,argv)
        except AccountError:
            continue
        if not code and output.strip():
            return output.strip()
    raise AccountError("account_repin_occupancy_unavailable")


def _tree(records, root):
    result={root}
    while True:
        found={pid for pid,row in records.items() if row["ppid"] in result}
        if found<=result:
            return result
        result|=found


def _native_executable(pid):
    """Read the executed pathname, never the shell-like ps argument display."""
    try:
        if type(pid) is not int or pid <= 0:
            raise ValueError()
        if sys.platform == "darwin":
            library=ctypes.CDLL("/usr/lib/libproc.dylib",use_errno=True)
            probe=library.proc_pidpath
            probe.argtypes=(ctypes.c_int,ctypes.c_void_p,ctypes.c_uint32)
            probe.restype=ctypes.c_int
            buffer=ctypes.create_string_buffer(4096)
            length=probe(pid,buffer,len(buffer))
            raw=buffer.raw
            if (length<=0 or length>=len(buffer) or raw[length]!=0
                    or b"\0" in raw[:length]):
                raise ValueError()
            path=os.fsdecode(raw[:length])
        elif sys.platform == "linux":
            path=os.readlink(f"/proc/{pid}/exe")
            if path.endswith(" (deleted)"):
                raise ValueError()
        else:
            raise ValueError()
        if not path or not os.path.isabs(path) or "\0" in path:
            raise ValueError()
        return path
    except (OSError,AttributeError,TypeError,ValueError):
        raise AccountError("account_repin_occupancy_unavailable") from None


def _helper_os():
    return sys.platform


def _file_id(info):
    return (info.st_dev,info.st_ino,info.st_mode,info.st_uid,info.st_size,info.st_mtime_ns,info.st_ctime_ns)


def _resolve(path):
    """Strict bounded resolution, retaining every traversed symlink identity."""
    if not isinstance(path,str) or not path.startswith('/') or '\0' in path or len(os.fsencode(path))>4096:
        raise ValueError()
    pending=deque(Path(path).parts[1:]);current=Path('/');links=[]
    while pending:
        part=pending.popleft()
        if part=='.':continue
        if part=='..':current=current.parent;continue
        candidate=current/part;info=candidate.lstat()
        if stat.S_ISLNK(info.st_mode):
            if len(links)>=40:raise ValueError()
            target=os.readlink(candidate)
            if _file_id(candidate.lstat())!=_file_id(info):raise ValueError()
            links.append((str(candidate),_file_id(info),target))
            pieces=Path(target).parts
            if os.path.isabs(target):current=Path('/');pieces=pieces[1:]
            pending.extendleft(reversed(pieces))
        else:
            if pending and not stat.S_ISDIR(info.st_mode):raise ValueError()
            current=candidate
    if not stat.S_ISREG(current.stat().st_mode):raise ValueError()
    return str(current),tuple(links)


def _hashed_file(path,cache):
    with open(path,'rb') as stream:
        before=_file_id(os.fstat(stream.fileno()))
        if not stat.S_ISREG(before[2]):raise ValueError()
        if before not in cache:
            hasher=hashlib.sha256()
            while chunk:=stream.read(1024*1024):hasher.update(chunk)
            cache[before]=hasher.hexdigest()
        if _file_id(os.fstat(stream.fileno()))!=before:raise ValueError()
        return before,cache[before]


def _file(path,cache):
    canonical,links=_resolve(path)
    identity,sha=_hashed_file(canonical,cache)
    if _resolve(path)!=(canonical,links) or _file_id(os.stat(canonical))!=identity:raise ValueError()
    return {'path':canonical,'links':links,'identity':identity,'sha256':sha}


def _executed_file(pid,path,cache):
    target=_file(path,cache)
    if _helper_os()=='linux':
        identity,sha=_hashed_file(f'/proc/{pid}/exe',cache)
        if identity!=target['identity'] or sha!=target['sha256']:raise ValueError()
    elif _helper_os()!='darwin':raise ValueError()
    return target


def _kernel_argv(pid):
    if type(pid) is not int or pid<=0 or _helper_os()!='linux':raise ValueError()
    with open(f'/proc/{pid}/cmdline','rb') as stream:raw=stream.read(65537)
    if not raw or len(raw)>65536 or not raw.endswith(b'\0'):raise ValueError()
    return [os.fsdecode(arg) for arg in raw[:-1].split(b'\0')]


def _execution(source,root,records,native,pin,cache):
    """Binding-to-native evidence; interpreter identity is supporting only."""
    from _shared.provider_cli_release import SHA256,validate,version
    from provider_cli_artifact import platform_key
    target=_file(source.executable,cache)
    if not SHA256.fullmatch(source.executable_sha256 or '') or target['sha256']!=source.executable_sha256:
        raise ValueError()
    tree=_tree(records,root);candidates=native&tree
    if not candidates:raise ValueError()
    script=source.provider=='codex' and Path(target['path']).name=='codex.js'
    if not script:
        if Path(target['path']).name in {'node','nodejs','python','python3','bash','sh','zsh','env'}:
            raise ValueError()
        if len(candidates)!=1:raise ValueError()
        pid=next(iter(candidates));path=_native_executable(pid);image=_executed_file(pid,path,cache)
        if image['path']!=target['path'] or image['sha256']!=source.executable_sha256:raise ValueError()
        return pid,set(),{'target':target,'native_path':path,'image':image},(pid,root)
    if _helper_os()!='linux':raise ValueError()
    pin=validate(pin);platform=platform_key()
    if not platform.startswith('linux-'):raise ValueError()
    entry=pin['providers'][source.provider]
    if version(source.provider,source.version)!=entry['version']:raise ValueError()
    expected=entry['artifacts'][platform]['native_sha256']
    matches=[]
    for pid in sorted(candidates):
        path=_native_executable(pid);image=_executed_file(pid,path,cache)
        if image['sha256']!=expected:continue
        chain=[pid];parent=pid
        while parent!=root:
            parent=records[parent]['ppid']
            if parent not in tree or parent in chain:raise ValueError()
            chain.append(parent)
        if any(records[p]['uid']!=os.getuid() or not records[p]['start_id'] for p in chain):raise ValueError()
        wrappers=[]
        for ancestor in chain[1:]:
            interpreter_path=_native_executable(ancestor)
            # Discovery only: a Node filename alone never attests a provider.
            if Path(interpreter_path).name!='node':continue
            interpreter=_executed_file(ancestor,interpreter_path,cache)
            argv=tuple(_kernel_argv(ancestor))
            if len(argv)<2 or not argv[0] or not os.path.isabs(argv[1]):continue
            argument=_file(argv[1],cache)
            if argument['path']!=target['path'] or argument['sha256']!=source.executable_sha256:continue
            wrappers.append((ancestor,{'path':interpreter_path,'file':interpreter,'argv':argv,'script':argument}))
        if len(wrappers)!=1:continue
        wrapper,support=wrappers[0]
        if candidates-{wrapper}!={pid}:continue
        matches.append((pid,{wrapper},{'target':target,'native_path':path,'image':image,
            'wrapper':support,'wrapper_pid':wrapper,'pin_digest':digest(pin)},tuple(chain)))
    if len(matches)!=1:raise ValueError()
    return matches[0]


def status(host, binding, launchers, *, mutation_state=True,pin=None):
    """Attest a unique native process per immutable managed pane/birth/home."""
    from provider_cli_artifact import code_identity
    if not isinstance(launchers,list) or len(launchers)>256:
        raise AccountError("account_repin_occupancy_unavailable")
    native=_native_pids(host,binding)
    records=_records(host) if native and launchers else {}
    if records and not native<=records.keys():
        raise AccountError("account_repin_occupancy_unavailable")
    boot=_boot(host) if records else ""
    local={row.key:row for row in host.registry.bindings()}
    managed=[];missing=[];claimed=set();probed=[];reserved=set();cache={}
    pin_digest=digest(pin) if pin is not None else 'absent'
    for launcher in launchers:
        try:
            if not isinstance(launcher,dict):
                raise ValueError()
            source=Binding.from_public(launcher["binding"])
            registered=local[source.key]
            if (source.host!=host.config["host"] or source.provider!=binding.provider
                    or dict(source.public(),default_revision=0)!=dict(registered.public(),default_revision=0)):
                raise ValueError()
            host.registry._validate_home(registered)
            sid=launcher["stream_id"]
            hostname,name=sid.split(":",1)
            _component(name)
            if (hostname!=source.host or not launcher["session_generation"]
                    or launcher["observer_generation"]!=launcher["session_generation"]
                    or launcher["observer_executable"]!=source.executable):
                raise ValueError()
            code,output=_run(host,["tmux","list-panes","-t","="+name,"-F",
                "#{pane_pid}|#{pane_id}|#{pane_tty}|#{socket_path}|#{session_name}"])
            fields=output.strip().split("|")
            if code or len(fields)!=5 or not all(fields) or fields[4]!=name or fields[0]!=launcher["pane_pid"]:
                raise ValueError()
            root=int(fields[0]);record=records[root]
            if (record["uid"]!=os.getuid() or record["start_id"]!=canonical_start(launcher["pane_started_at"])):
                raise ValueError()
            if not boot:raise ValueError()
            pid,support,evidence,chain=_execution(source,root,records,native,pin,cache)
            actual=records[pid]
            if (reserved&({pid}|support) or actual["uid"]!=os.getuid()):
                raise ValueError()
            proof=identity_proof(host=source.host,boot=boot,record=actual,tmux_session=name,
                tmux_pane=fields[1],tty=fields[2],tmux_socket=fields[3])
            root_proof=identity_proof(host=source.host,boot=boot,record=record,tmux_session=name,
                tmux_pane=fields[1],tty=fields[2],tmux_socket=fields[3])
            if not proof or not root_proof:
                raise ValueError()
            reserved.update({pid}|support)
            probed.append((root,pid,name,fields,source,support,evidence,chain,{"stream_id":sid,
                "session_generation":launcher["session_generation"],"binding":source.public(),
                "process":proof,"pane_root":root_proof}))
        except (AccountError,KeyError,TypeError,ValueError,OSError):
            missing.append(launcher.get("stream_id") if isinstance(launcher,dict) else "unverified")
    # Share bounded full-host observations after every native path read. Admit
    # only unchanged process instances and the same unique native descendant.
    if probed:
        try:
            fresh_native=_native_pids(host,binding)
            fresh_records=_records(host)
            fresh_boot=_boot(host)
            native|=fresh_native  # Newly observed unknown processes stay visible.
        except AccountError:
            fresh_native=set();fresh_records={};fresh_boot=""
        for root,pid,name,fields,source,support,evidence,chain,row in probed:
            try:
                code,output=_run(host,["tmux","list-panes","-t","="+name,"-F",
                    "#{pane_pid}|#{pane_id}|#{pane_tty}|#{socket_path}|#{session_name}"])
                if (code or output.strip().split("|")!=fields or not boot_matches(fresh_boot,boot)
                        or not fresh_native<=fresh_records.keys()
                        or any(fresh_records.get(p)!=records[p] for p in chain)
                        or fresh_records.get(root)!=records[root]
                        or _execution(source,root,fresh_records,fresh_native,pin,cache)!=(pid,support,evidence,chain)):
                    raise ValueError()
                claimed.update({pid}|support)
                managed.append(row)
            except (AccountError,KeyError,TypeError,ValueError,OSError):
                missing.append(row["stream_id"])
    from provider_account_mutation import _cell_entries,read
    pending=[]
    for operation in (_cell_entries(host,"permits",binding.provider,binding.alias) if mutation_state else ()):
        journal=host._read_operation(operation)
        if not read(host,"seals",operation) and (not journal or journal.get("state")!="done"):
            pending.append(operation)
    return {"outcome":"ok","host":host.config["host"],"provider":binding.provider,
        "manifest_digest":digest(launchers),"managed":managed,"missing":missing,
        "unmanaged":[{"pid":pid} for pid in sorted(native-claimed)],"pending_mutations":sorted(pending),
        "code_identity":code_identity(),"pin_digest":pin_digest}


def assert_drained(host, binding, launchers):
    # B5 must be able to supersede untrusted old journals. Occupancy proves
    # only processes; the mutation protocol separately owns causes and seals.
    value=status(host,binding,launchers,mutation_state=False)
    if value["missing"] or value["unmanaged"] or any(row["binding"]["home"]==binding.home for row in value["managed"]):
        raise AccountError("account_repin_home_busy")
