"""Bounded local proof of immutable native files; never opens account credentials."""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
import platform
import stat
import subprocess

from provider_accounts import AccountError
from provider_account_native import verify_binary
from provider_cli_pin import digest, version


def platform_key():
    system={"Darwin":"darwin","Linux":"linux"}.get(platform.system())
    arch={"arm64":"arm64","aarch64":"arm64","x86_64":"x64","AMD64":"x64"}.get(platform.machine())
    if not system or not arch: raise AccountError("provider_cli_verification_unavailable",reason="platform_unsupported")
    return system+"-"+arch


def _immutable(path, *, directory=False):
    info=path.lstat()
    if (path!=path.resolve() or info.st_uid not in {0,os.getuid()} or info.st_mode & 0o022
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
        raise AccountError("provider_cli_pin_mismatch",reason="artifact_path_insecure")


def resource_digest(root):
    """Canonical relative-path/hash inventory; a standalone file hashes its bytes."""
    root=Path(root)
    try:
        if not root.is_absolute(): raise OSError()
        _immutable(root,directory=root.is_dir())
        if root.is_file(): return hashlib.sha256(root.read_bytes()).hexdigest()
        rows=[]; total=0
        for path in sorted(root.rglob("*")):
            _immutable(path,directory=path.is_dir())
            if path.is_file():
                total+=path.stat().st_size
                if len(rows)>=10000 or total>2_000_000_000: raise OSError()
                rows.append((path.relative_to(root).as_posix(),hashlib.sha256(path.read_bytes()).hexdigest()))
        if not rows: raise OSError()
        return digest(rows)
    except (OSError,RuntimeError):
        raise AccountError("provider_cli_verification_unavailable",reason="resources_unavailable") from None


def code_identity():
    root=Path(__file__).parent
    files=("provider_cli_artifact.py","provider_account_host.py","provider_account_native.py","provider_accounts.py")
    inputs={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in files}
    inputs["provider_cli_release.py"]=hashlib.sha256((root.parent/"_shared/provider_cli_release.py").read_bytes()).hexdigest()
    return digest(inputs)


def verify(binding,cell,pin,*,run=subprocess.run,native_platform=None):
    key=native_platform or platform_key()
    entry=pin["providers"][binding.provider]
    artifact=entry["artifacts"].get(key)
    family="macos" if key.startswith("darwin-") else "wsl"
    if not artifact or family!=binding.platform or version(binding.provider,binding.version)!=entry["version"]:
        raise AccountError("provider_cli_pin_mismatch",reason="platform_or_version_mismatch")
    try: _immutable(Path(binding.executable))
    except OSError: raise AccountError("provider_cli_verification_unavailable") from None
    verify_binary(binding,run=run)
    resource_root=cell.get("resource_roots",{}).get(binding.executable)
    if resource_root is None and binding.executable==cell.get("executable"):
        resource_root=cell.get("resource_root")
    if not resource_root: raise AccountError("provider_cli_verification_unavailable",reason="resource_root_missing")
    if (artifact["native_sha256"]!=binding.executable_sha256 or
            artifact["resource_sha256"]!=resource_digest(resource_root)):
        raise AccountError("provider_cli_pin_mismatch",reason="artifact_hash_mismatch")
    return {"outcome":"ok","binding_key":binding.key,"platform":key,"artifact":artifact,
            "pin_revision":pin["revision"],"pin_digest":digest(pin),"code_identity":code_identity()}


def assert_drained(binding,*,run=subprocess.run):
    """Same-uid native processes are ambiguous without reading their credential-bearing environment."""
    try:
        result=run(["ps","-U",str(os.getuid()),"-o","pid=,comm=,args="],capture_output=True,text=True,timeout=10)
        if result.returncode: raise OSError()
        for line in result.stdout.splitlines():
            parts=line.strip().split(None,2)
            if len(parts)<2 or not parts[0].isdigit(): raise OSError()
            name=Path(parts[1]).name
            if name in {binding.provider,Path(binding.executable).name,Path(binding.observer).name}:
                raise AccountError("account_repin_home_busy")
    except (OSError,subprocess.SubprocessError):
        raise AccountError("account_repin_occupancy_unavailable") from None
