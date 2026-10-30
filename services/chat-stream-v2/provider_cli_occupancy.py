"""Read-only managed launcher attestation; unknown provider processes block."""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess
import sys

from provider_accounts import AccountError, Binding, _component
from provider_cli_pin import digest
from prockill import SESSION_ID_PROBE, canonical_start, identity_proof, parse_process_records


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
    for known in host.registry.bindings():
        if known.provider==binding.provider:
            names.update((Path(known.executable).name,Path(known.observer).name))
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


def status(host, binding, launchers, *, mutation_state=True):
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
    managed=[];missing=[];claimed=set()
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
            candidates=native&_tree(records,root)
            # A nested/unrecorded provider child is not silently assigned its
            # parent's home. Multiple native instances are ambiguous.
            if len(candidates)!=1 or not boot:
                raise ValueError()
            pid=next(iter(candidates));actual=records[pid]
            argv=shlex.split(actual["command"])
            if (pid in claimed or actual["uid"]!=os.getuid() or not argv
                    or argv[0]!=source.executable):
                raise ValueError()
            proof=identity_proof(host=source.host,boot=boot,record=actual,tmux_session=name,
                tmux_pane=fields[1],tty=fields[2],tmux_socket=fields[3])
            root_proof=identity_proof(host=source.host,boot=boot,record=record,tmux_session=name,
                tmux_pane=fields[1],tty=fields[2],tmux_socket=fields[3])
            if not proof or not root_proof:
                raise ValueError()
            claimed.add(pid)
            managed.append({"stream_id":sid,"session_generation":launcher["session_generation"],
                "binding":source.public(),"process":proof,"pane_root":root_proof})
        except (AccountError,KeyError,TypeError,ValueError):
            missing.append(launcher.get("stream_id") if isinstance(launcher,dict) else "unverified")
    from provider_account_mutation import _cell_entries,read
    pending=[]
    for operation in (_cell_entries(host,"permits",binding.provider,binding.alias) if mutation_state else ()):
        journal=host._read_operation(operation)
        if not read(host,"seals",operation) and (not journal or journal.get("state")!="done"):
            pending.append(operation)
    return {"outcome":"ok","host":host.config["host"],"provider":binding.provider,
        "manifest_digest":digest(launchers),"managed":managed,"missing":missing,
        "unmanaged":[{"pid":pid} for pid in sorted(native-claimed)],"pending_mutations":sorted(pending),
        "code_identity":code_identity()}


def assert_drained(host, binding, launchers):
    # B5 must be able to supersede untrusted old journals. Occupancy proves
    # only processes; the mutation protocol separately owns causes and seals.
    value=status(host,binding,launchers,mutation_state=False)
    if value["unmanaged"] or any(row["binding"]["home"]==binding.home for row in value["managed"]):
        raise AccountError("account_repin_home_busy")
