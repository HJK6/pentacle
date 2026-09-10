"""launch.py - provider CLI launch-command + env construction (B3 contract).

LIFTED from v1 per the v1 code-reuse map (2026-08-05, `_artifacts/
v1_code_reuse_map.md`): the launch-command + env construction is healthy v1
code, so it is MIGRATED here in shape rather than reinvented. Sources:
the retired v1 session launcher (claude launch string, `stream_env_assignments`,
`agent_orch_path_export`, `slugify_cwd`, `jsonl_path_for`, `mint_stream_token`,
`CLAUDE_DISALLOWED_TOOLS`) and `codex_provider.py` (`_codex_launch_prefix`,
`_codex_path_export`, `CODEX_OPERATOR_QUESTION_INSTRUCTION`).

What changes vs v1 (the cross-cutting lift rule: logic ports, I/O placement does
not): this module is PURE string construction with no syscalls — the one blocking
step, `tmux new-session`, stays in `spawnctl.Tmux` off the event loop. The
provider/model/effort TUPLE is resolved by the SHARED `_shared.spawn_profiles`
module (imported by `spawnctl.py`, never copied); this module only turns an
already-resolved tuple into the exact shell command + env v1 emits.

B3 contract (spawnctl docstring req 3): the launch envelope carries the declared
env (`PENTACLE_STREAM_ID` / `AGENT_ORCH_STREAM_ID` /
`AGENT_ORCH_STREAM_TOKEN_FILE`) +
caller identity, agent-orch on PATH, and the resolved model/effort flags in v1's
exact form. The `*_launch_tuple` fields in `spawn.ok` are byte-parity with v1.

Machine profile: v2 spawn is localhost-only (spec Phase C), so only the LOCAL
machine's paths are needed. `LocalMachine` mirrors v1 `machines.MachineConfig`'s
field NAMES so the eventual swap to Lane 5's real machines.json subsystem is a
drop-in — Lane 5 owns that subsystem; this is the lane-7 stopgap, resolved from
daemon args/env with the same defaults as v1's `_default_local_machine`.
"""

from __future__ import annotations

import os
import secrets
import shlex
import shutil
import uuid
import hashlib
from typing import Any
from dataclasses import dataclass

#: v1 session.py:1348 — the one tool a spawned Claude seat must not expose
#: (operator questions route through `agent-orch prompt ask`, not AskUserQuestion).
CLAUDE_DISALLOWED_TOOLS = "AskUserQuestion"

#: v1 codex_provider.py:864 — flags every orchestrated Codex seat needs.
CODEX_REQUIRED_FLAGS = ("--dangerously-bypass-approvals-and-sandbox", "--no-alt-screen")

#: v1 codex_provider.py:842 — the operator-question mandate, injected as a
#: launch-level developer_instructions override (ranks above AGENTS.md). Lifted
#: verbatim; no apostrophes/quotes so `shlex.quote` of the `key=value` argv
#: element stays clean.
CODEX_OPERATOR_QUESTION_INSTRUCTION = (
    "Orchestrated Pentacle seat: never ask the operator a question by emitting a "
    "prose question and ending your turn, and never via a native approval or "
    "request_user_input surface. When you need operator input, run agent-orch "
    "prompt ask (asynchronous and durable) then continue or report; if that is "
    "impossible, report blocked to your lead. Do not stop your turn waiting for an "
    "operator reply in the pane."
)

# The marker is deliberately stable: spawnctl uses it to distinguish a real
# tuple launch, which can use Codex's positional initial prompt, from an
# explicit command (usually a test/smoke stub) whose input contract is unknown.
NATIVE_INITIAL_PROMPT_LAUNCHER = "pentacle-native-initial-prompt"


@dataclass(frozen=True)
class LocalMachine:
    """The local host's launch paths. Field names mirror v1 `MachineConfig` so a
    later swap to the real machines.json record is a drop-in (Lane 5)."""

    name: str
    cwd: str
    codex_cwd: str
    claude_bin: str
    codex_bin: str
    projects_root: str
    agent_orch_bin_dir: str | None = None


def local_machine(
    name: str,
    *,
    cwd: str | None = None,
    claude_bin: str | None = None,
    codex_bin: str | None = None,
    projects_root: str | None = None,
    agent_orch_bin_dir: str | None = None,
) -> LocalMachine:
    """Resolve the local machine profile from daemon args/env, defaulting exactly
    as v1's `_default_local_machine` (session.py). Every field is overridable so a
    test can point `claude_bin`/`codex_bin` at a stub and `cwd` at a tmp dir."""
    home = os.path.expanduser("~")
    resolved_cwd = cwd or os.path.join(home, "agent-workspace")
    resolved_claude = claude_bin or os.path.join(home, ".local/bin/claude")
    return LocalMachine(
        name=name,
        cwd=resolved_cwd,
        codex_cwd=resolved_cwd,
        claude_bin=resolved_claude,
        codex_bin=codex_bin or "codex",
        projects_root=projects_root or os.path.join(home, ".claude/projects"),
        # Keep an explicit profile/CLI value here. When it is absent, the
        # provider-specific command builder derives the fallback after the
        # active provider is known; resolving from Claude here misroutes a
        # local Codex launch when the two installs live in different dirs.
        agent_orch_bin_dir=agent_orch_bin_dir,
    )


def machine_from_config(cfg: Any, *, provider: str = "claude") -> LocalMachine:
    """A launch profile for a PEER host from its parsed machines.json record
    (`machines.MachineConfig` — same field names as v1). Every path must be
    remote-absolute; missing essentials raise so a remote tuple spawn fails
    with a configuration error instead of running local paths on the peer
    (the cutover-day remote-canary failure mode). The active provider is
    validated independently so a Codex-only legacy profile does not need a
    Claude path merely to launch Codex."""
    if provider not in ("claude", "codex"):
        raise ValueError(f"provider_unknown: {provider}")
    provider_bin = getattr(cfg, f"{provider}_bin", "") or ""
    if not (provider_bin and cfg.cwd and cfg.projects_root):
        raise ValueError(
            f"machines.json entry for {cfg.name} lacks {provider}_bin/cwd/projects_root"
        )
    ssh_target = getattr(cfg, "ssh_target", None)
    if ssh_target is not None and not str(ssh_target).strip():
        raise ValueError(f"machines.json entry for {cfg.name} has a blank ssh_target")
    explicit_agent_orch = getattr(cfg, "agent_orch_bin_dir", None)
    if explicit_agent_orch and not os.path.isabs(explicit_agent_orch):
        raise ValueError(
            f"machines.json entry for {cfg.name} needs an absolute agent_orch_bin_dir"
        )
    provider_executable = _shell_executable(provider_bin)
    if ssh_target is not None and (
        not os.path.isabs(provider_executable)
        or not os.path.isabs(cfg.cwd)
        or not os.path.isabs(cfg.projects_root)
    ):
        raise ValueError(
            f"machines.json entry for {cfg.name} needs absolute {provider}_bin/cwd/projects_root "
            "for remote tuple launch"
        )
    return LocalMachine(
        name=cfg.name,
        cwd=cfg.cwd,
        codex_cwd=getattr(cfg, "codex_cwd", None) or cfg.cwd,
        claude_bin=cfg.claude_bin or "",
        codex_bin=cfg.codex_bin or "",
        projects_root=cfg.projects_root,
        agent_orch_bin_dir=(
            explicit_agent_orch
            or _agent_orch_bin_dir_default(
                provider_bin=provider_bin, tmux_bin=cfg.tmux_bin or "tmux",
                allow_ambient=False,
            )
        ),
    )


def raw_command_machine_from_config(cfg: Any) -> LocalMachine:
    """Build the target profile needed to envelope a raw command launch.

    Raw commands have no provider tuple to select a binary from, but they still
    run on a concrete host. Prefer the explicit target-host orchestration path;
    when it is omitted, derive it from one of that host's absolute provider
    paths. Never fall back to the daemon's ambient PATH for a remote target.
    """
    explicit_agent_orch = getattr(cfg, "agent_orch_bin_dir", None)
    provider_bin = getattr(cfg, "claude_bin", "") or getattr(cfg, "codex_bin", "") or ""
    provider_executable = _shell_executable(provider_bin) if provider_bin else ""
    is_remote = getattr(cfg, "ssh_target", None) is not None
    if is_remote and not explicit_agent_orch and not os.path.isabs(provider_executable):
        raise ValueError(
            f"machines.json entry for {cfg.name} needs an absolute provider path "
            "or explicit agent_orch_bin_dir for raw command launch"
        )
    if explicit_agent_orch and not os.path.isabs(explicit_agent_orch):
        raise ValueError(
            f"machines.json entry for {cfg.name} needs an absolute agent_orch_bin_dir"
        )
    agent_orch_bin_dir = explicit_agent_orch
    if not agent_orch_bin_dir and provider_bin:
        agent_orch_bin_dir = _agent_orch_bin_dir_default(
            provider_bin=provider_bin, tmux_bin=getattr(cfg, "tmux_bin", "tmux") or "tmux",
            allow_ambient=False,
        )
    if is_remote and not agent_orch_bin_dir:
        raise ValueError(
            f"machines.json entry for {cfg.name} has no target-host agent-orch path"
        )
    return LocalMachine(
        name=cfg.name,
        cwd=getattr(cfg, "cwd", "") or "",
        codex_cwd=getattr(cfg, "codex_cwd", None) or getattr(cfg, "cwd", "") or "",
        claude_bin=getattr(cfg, "claude_bin", "") or "",
        codex_bin=getattr(cfg, "codex_bin", "") or "",
        projects_root=getattr(cfg, "projects_root", "") or "",
        agent_orch_bin_dir=agent_orch_bin_dir,
    )


def _shell_executable(command: str) -> str:
    """Return the executable portion of a shell-style configured command."""
    parts = _command_parts(command)
    return parts[0] if parts else ""


def _command_parts(command: str) -> list[str]:
    """Split a configured command while preserving absolute paths with spaces."""
    if not os.path.isabs(command):
        return shlex.split(command)
    # Keep the raw executable substring intact. `shlex.split` cannot round-trip
    # a literal apostrophe in an unquoted path, while provider arguments in the
    # supported command form begin with an option flag.
    for idx, char in enumerate(command):
        if char.isspace():
            remainder = command[idx:].lstrip()
            if remainder.startswith("-"):
                return [command[:idx], *shlex.split(remainder)]
    return [command]


def _resolve_local_executable(executable: str) -> str:
    """Make a bare local provider executable survive a stripped child PATH."""
    if os.path.isabs(executable):
        return executable
    resolved = shutil.which(executable)
    if not resolved:
        raise ValueError(
            f"local provider executable {executable!r} is not discoverable on the daemon PATH"
        )
    return os.path.realpath(os.path.abspath(resolved))


def _agent_orch_bin_dir_default(
    *, provider_bin: str, tmux_bin: str, allow_ambient: bool = True,
) -> str:
    """Infer the target host's install directory without coupling it to tmux.

    `tmux` is often installed by Homebrew while the orchestration CLI is a
    user-local script (coordinator's production daemon is exactly this shape). Provider
    paths are the useful profile signal; explicit ``agent_orch_bin_dir`` remains
    authoritative for hosts whose CLI lives elsewhere. Ambient PATH lookup is
    retained only for local fallback resolution; remote profiles must never use
    the daemon host's PATH to infer a peer install directory.
    """
    del tmux_bin  # retained in the signature for callers carrying v1 profiles
    provider_executable = _shell_executable(provider_bin)
    provider_dir = os.path.dirname(provider_executable)
    if provider_dir:
        return provider_dir
    if allow_ambient:
        provider_path = shutil.which(provider_executable)
        if provider_path:
            return os.path.dirname(os.path.realpath(os.path.abspath(provider_path)))
        bin_path = os.environ.get("AGENT_ORCH_BIN") or shutil.which("agent-orch")
        if bin_path:
            return os.path.dirname(os.path.realpath(bin_path))
    return os.path.join(os.path.expanduser("~"), ".local/bin")


def local_machine_from_config(
    cfg: Any,
    *,
    cwd: str | None = None,
    claude_bin: str | None = None,
    codex_bin: str | None = None,
    projects_root: str | None = None,
    agent_orch_bin_dir: str | None = None,
) -> LocalMachine:
    """Build the local launch profile from the daemon's host config.

    CLI overrides remain higher priority, but the configured local profile is
    the source of truth when the daemon itself runs with a minimal PATH.
    """
    return local_machine(
        cfg.name,
        cwd=cwd or cfg.cwd or None,
        claude_bin=claude_bin or cfg.claude_bin or None,
        codex_bin=codex_bin or cfg.codex_bin or None,
        projects_root=projects_root or cfg.projects_root or None,
        agent_orch_bin_dir=(
            agent_orch_bin_dir or cfg.agent_orch_bin_dir or None
        ),
    )


# -- env construction (lifted verbatim from v1 session.py) --------------------

def mint_stream_token() -> str:
    return secrets.token_urlsafe(32)


def slugify_cwd(cwd: str) -> str:
    """v1 session.py:225 — mirror Claude Code's project-dir naming so the derived
    jsonl path agrees with where the CLI actually writes."""
    return cwd.replace("/", "-").replace("_", "-").replace(".", "-")


def jsonl_path_for(machine: LocalMachine, cwd: str, session_id: str) -> str:
    resolved_cwd = os.path.realpath(cwd)
    return f"{machine.projects_root}/{slugify_cwd(resolved_cwd)}/{session_id}.jsonl"


def stream_token_file_for(machine: LocalMachine, tmux_session: str) -> str:
    """Return a non-secret, deterministic path for a seat's private token file."""
    seat_key = hashlib.sha256(
        f"{machine.name}:{tmux_session}".encode("utf-8")
    ).hexdigest()[:24]
    return f"{machine.cwd}/.pentacle-stream-tokens/{seat_key}.token"


def stream_env_assignments(
    machine: LocalMachine,
    tmux_session: str,
    stream_token_file: str | None = None,
) -> str:
    """The caller-identity env every spawned pane carries.

    Only the private token-file path is put in the launch command. The token is
    read by the CLI from that file when a request is built.
    """
    stream_id = f"{machine.name}:{tmux_session}"
    assignments = (
        f"PENTACLE_STREAM_ID={shlex.quote(stream_id)} "
        f"AGENT_ORCH_STREAM_ID={shlex.quote(stream_id)}"
    )
    if stream_token_file:
        assignments = (
            f"{assignments} AGENT_ORCH_STREAM_TOKEN_FILE="
            f"{shlex.quote(stream_token_file)}"
        )
    return assignments


def agent_orch_path_export(
    machine: LocalMachine | None = None, *, provider_bin: str | None = None,
) -> str:
    """Make the target profile's agent-orch CLI authoritative in the pane.

    A spawned pane runs a non-login shell that never sources the profile which
    prepends the configured bin directory. Always putting that directory first
    also prevents an ambient local agent-orch installation from silently
    replacing the target host's resolved CLI. Returns a `PATH` envelope, or
    `""` if the directory cannot be resolved.
    """
    bin_dir = machine.agent_orch_bin_dir if machine else None
    if not bin_dir and provider_bin:
        bin_dir = _agent_orch_bin_dir_default(
            provider_bin=provider_bin, tmux_bin="tmux",
        )
    if not bin_dir and machine:
        machine_provider_bin = machine.claude_bin or machine.codex_bin
        if machine_provider_bin:
            bin_dir = _agent_orch_bin_dir_default(
                provider_bin=machine_provider_bin, tmux_bin="tmux",
            )
    if not bin_dir:
        bin_dir = os.environ.get("AGENT_ORCH_BIN_DIR")
    if not bin_dir:
        bin_path = os.environ.get("AGENT_ORCH_BIN") or shutil.which("agent-orch")
        if not bin_path:
            return ""
        bin_dir = os.path.dirname(os.path.realpath(bin_path))
    if not bin_dir:
        return ""
    return f"export PATH={shlex.quote(bin_dir)}:$PATH && "


def _codex_path_export(codex_bin: str) -> str:
    """Keep the user's PATH precedence while exposing Codex sibling tools."""
    bin_dir = os.path.dirname(codex_bin)
    if not bin_dir:
        return ""
    return f"export PATH=$PATH:{shlex.quote(bin_dir)} && "


# -- command construction (lifted from v1, resolved tuple in) -----------------

def _claude_command(
    machine: LocalMachine, tmux_session: str, session_id: str,
    stream_token_file: str,
    *, launch_model: str | None, launch_effort: str | None,
) -> str:
    """The claude launch shell command (v1 session.py:1417). `launch_model`/
    `launch_effort` are the flags v1 passes ONLY when the client explicitly asked
    for them; a profile-default spawn passes none and the CLI uses its own default
    (which the profile is defined to match)."""
    claude_bin = _resolve_local_executable(_shell_executable(machine.claude_bin))
    model_flag = f"--model {shlex.quote(launch_model)} " if launch_model else ""
    effort_flag = f"--effort {shlex.quote(launch_effort)} " if launch_effort else ""
    return (
        f"cd {shlex.quote(machine.cwd)} && "
        f"{agent_orch_path_export(machine, provider_bin=claude_bin)}"
        f"{stream_env_assignments(machine, tmux_session, stream_token_file)} "
        f"exec {shlex.quote(claude_bin)} "
        f"--dangerously-skip-permissions "
        f"--permission-mode bypassPermissions "
        f"--disallowed-tools {CLAUDE_DISALLOWED_TOOLS} "
        f"{model_flag}{effort_flag}--session-id {shlex.quote(session_id)}"
    )


def _codex_command(
    machine: LocalMachine, tmux_session: str, stream_token_file: str,
    *, launch_model: str | None, launch_effort: str | None,
    initial_prompt_file: str | None = None,
) -> str:
    """The codex launch shell command (v1 codex_provider.py:_codex_launch_prefix).

    Required flags, resolved model/effort (explicit-only, as for claude), the
    apps-off guard (`features.apps=false`, opt back in with
    PENTACLE_CODEX_ENABLE_APPS=1), and the operator-question developer_instructions
    mandate merged last-wins."""
    parts = _command_parts(machine.codex_bin)
    executable = _resolve_local_executable(parts[0] if parts else "codex")
    args = parts[1:]
    for required in CODEX_REQUIRED_FLAGS:
        if required not in args:
            args.append(required)
    if launch_model:
        args.extend(["-m", launch_model])
    if launch_effort:
        args.extend(["-c", f"model_reasoning_effort={launch_effort}"])
    if os.environ.get("PENTACLE_CODEX_ENABLE_APPS") not in ("1", "true", "True"):
        if "features.apps=false" not in args:
            args.extend(["-c", "features.apps=false"])
    # developer_instructions is last-wins in codex: strip any preexisting one,
    # merge its text ahead of the mandate, append the merged value last (v1 QA
    # 2026-07-06 — a bare guard would let a custom value suppress the mandate).
    existing = ""
    stripped: list[str] = []
    skip_next = False
    for idx, arg in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if arg == "-c" and idx + 1 < len(args) and args[idx + 1].startswith("developer_instructions="):
            existing = args[idx + 1][len("developer_instructions="):]
            skip_next = True
            continue
        stripped.append(arg)
    args = stripped
    if CODEX_OPERATOR_QUESTION_INSTRUCTION in existing:
        merged = existing
    elif existing:
        merged = f"{existing}\n\n{CODEX_OPERATOR_QUESTION_INSTRUCTION}"
    else:
        merged = CODEX_OPERATOR_QUESTION_INSTRUCTION
    args.extend(["-c", f"developer_instructions={merged}"])
    argv = " ".join(shlex.quote(part) for part in [executable, *args])
    env_prefix = f"{stream_env_assignments(machine, tmux_session, stream_token_file)} "
    if initial_prompt_file:
        # Keep the prompt out of tmux's command line. The sentinel preserves
        # trailing newlines through command substitution, then Codex receives
        # the staged bytes as its one positional initial-prompt argument.
        launcher = "\n".join((
            "prompt_file=$1",
            "shift",
            "prompt=$(cat \"$prompt_file\"; printf '\\001')",
            "prompt=${prompt%?}",
            "unset CODEX_SESSION_ID CODEX_THREAD_ID CODEX_CI PENTACLE_SPAWN_NONCE",
            "exec \"$@\" \"$prompt\"",
        ))
        return (
            f"{agent_orch_path_export(machine, provider_bin=executable)}"
            f"{_codex_path_export(executable)}"
            f"{env_prefix}exec /bin/sh -c {shlex.quote(launcher)} "
            f"{shlex.quote(NATIVE_INITIAL_PROMPT_LAUNCHER)} "
            f"{shlex.quote(initial_prompt_file)} {argv}"
        )
    return (
        f"{agent_orch_path_export(machine, provider_bin=executable)}"
        f"{_codex_path_export(executable)}"
        f"{env_prefix}exec {argv}"
    )


@dataclass(frozen=True)
class LaunchPlan:
    """A fully-resolved launch: the shell command for `tmux new-session`, plus the
    identity v2 persists so the transcript-evidence probe (#16) and B10 adoption
    can find the pane's session log."""

    command: str
    session_id: str
    jsonl_path: str
    stream_token: str
    stream_token_file: str


def build_launch(
    machine: LocalMachine,
    *,
    provider: str,
    tmux_session: str,
    launch_model: str | None,
    launch_effort: str | None,
    initial_prompt_file: str | None = None,
) -> LaunchPlan:
    """Turn a RESOLVED (provider, model, effort) tuple into the exact launch v1
    emits. `launch_model`/`launch_effort` are None for a profile-default spawn
    (no flag passed, CLI default used) and the canonical value for an explicit
    request — matching v1's `launch_model = model if requested_model is not None`.
    """
    stream_token = mint_stream_token()
    stream_token_file = stream_token_file_for(machine, tmux_session)
    if provider == "claude":
        session_id = str(uuid.uuid4())
        command = _claude_command(
            machine, tmux_session, session_id, stream_token_file,
            launch_model=launch_model, launch_effort=launch_effort,
        )
        jsonl_path = jsonl_path_for(machine, machine.cwd, session_id)
        return LaunchPlan(
            command=command,
            session_id=session_id,
            jsonl_path=jsonl_path,
            stream_token=stream_token,
            stream_token_file=stream_token_file,
        )
    if provider == "codex":
        command = _codex_command(
            machine, tmux_session, stream_token_file,
            launch_model=launch_model, launch_effort=launch_effort,
            initial_prompt_file=initial_prompt_file,
        )
        return LaunchPlan(
            command=command,
            session_id="",
            jsonl_path="",
            stream_token=stream_token,
            stream_token_file=stream_token_file,
        )
    raise ValueError(f"provider_unknown: {provider}")
