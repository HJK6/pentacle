"""Local and SSH tmux transport primitives."""

from __future__ import annotations

import asyncio
import base64
from contextvars import ContextVar
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from machines import ssh_command, ssh_tmux_command
from prockill import terminate_and_reap
from sessions import VerbError


log = logging.getLogger("chat_streamd_v2.tmux_transport")
RECEIPT_TIMEOUT_S = 15.0
POLL_INTERVAL_S = 0.1
CAPTURE_LINES = "-200"
NEEDLE_MAX = 24

TRANSCRIPT_DIRS = ("/.claude/projects/", "/.codex/sessions/")
#: A delayed-restart adoption means the brief was among the LAST things
#: submitted before the crash, so it lives near the tail of the log. Read only
#: the tail: bounds the boot-path file read without losing the relevant span.
MAX_TRANSCRIPT_BYTES = 4 * 1024 * 1024

# Provider TUIs become lossy once a paste is collapsed into a composer
# placeholder. Keep the wire pointer comfortably below the observed failure
# boundary and make the complete prompt durable on the target host instead.
INITIAL_PROMPT_STAGE_THRESHOLD_BYTES = 256
PROMPT_STAGE_ROOT = Path("/tmp")
PROMPT_STAGE_DIR_NAME = "pentacle-prompt-stage"


#: Everything a human keyboard cannot produce inside a paste. Tab, LF and CR
#: are the only control characters an injected message may legitimately carry.
_UNSAFE_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
PASTE_END = "\x1b[201~"
SPEC_ID_RE = re.compile(r"^[A-Za-z0-9_-]+__[A-Za-z0-9_-]+$")


def _prompt_stage_path(text: str) -> tuple[Path, str, bytes]:
    data = text.encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    return PROMPT_STAGE_ROOT / PROMPT_STAGE_DIR_NAME / f"pentacle-initial-prompt-{digest}.txt", digest, data


def _atomic_stage_local(path: Path, data: bytes) -> None:
    """Write one content-addressed prompt with no visible partial file."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Tighten only the daemon-owned staging directory. The configured root may
    # be a shared system directory such as /tmp and is never ours to chmod.
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex[:12]}")
    try:
        fd = os.open(str(temporary), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path.parent, 0o700)
        os.chmod(path, 0o600)
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


_ACTIVE_LAUNCH_TMUX: ContextVar[Any] = ContextVar(
    "active_launch_tmux", default=None
)


def assert_injectable(text: str, what: str = "message") -> None:
    """Reject text that would escape the bracketed paste instead of riding
    inside it (ledger req 1: "no stray control sequences").

    `ESC[201~` is the paste TERMINATOR: embedded in a brief it ends the paste
    early and the remainder lands in the pane as raw keystrokes — arbitrary
    injection dressed as a message. Bare ESC is just as bad (it is the lead
    byte of every CSI sequence, and B6's interrupt). There is no safe encoding
    that still delivers the author's text verbatim, so this REJECTS: an
    explicit `unsafe_payload` failure, never a mangled injection.
    """
    if PASTE_END in text:
        raise VerbError("unsafe_payload", f"{what} contains a bracketed-paste terminator (ESC[201~)")
    if "\x1b" in text:
        raise VerbError("unsafe_payload", f"{what} contains an ESC control sequence")
    found = _UNSAFE_CONTROL.search(text)
    if found:
        raise VerbError("unsafe_payload", f"{what} contains control character 0x{ord(found.group()):02x}")


#: ANSI/VT control sequences, for the opt-in tell sanitizer (QA #17). CSI is
#: `ESC [ params intermediates final`; OSC is `ESC ] ... (BEL | ESC \)`.
_ANSI_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def sanitize_injectable(text: str) -> str:
    """Strip ANSI/ESC control sequences so captured terminal output can be
    relayed as a message (QA #17 — opt-in only; the default stays reject).

    Removes CSI/OSC sequences, any remaining bare ESC (including the lead byte
    of the bracketed-paste terminator, whose tail is a CSI already stripped),
    and the other control characters a paste may not carry — leaving tab, LF,
    CR, and all printable text intact. The result is GUARANTEED to pass
    `assert_injectable`, so injection keeps its single safe chokepoint."""
    text = _ANSI_OSC.sub("", text)
    text = _ANSI_CSI.sub("", text)
    text = text.replace("\x1b", "")
    return _UNSAFE_CONTROL.sub("", text)


def _target(session: str) -> str:
    """`=<name>:` — the leading `=` forces an exact session-name match (a bare
    `-t <name>` is a prefix match and can hit another agent's pane); the
    trailing `:` makes it a session target resolving to the active pane, which
    pane-target commands (capture-pane/send-keys/paste-buffer) require. This
    form is the only one accepted by BOTH session- and pane-target commands.
    No shell quoting is needed here: v2 execs tmux directly, never via a shell.
    """
    return f"={session}:"


def _write_local_paste_file(data: bytes) -> str:
    """Write one private tmux input file without blocking the event loop."""
    fd, raw_path = tempfile.mkstemp(prefix="pentacle-paste-", suffix=".txt")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            os.unlink(raw_path)
        except OSError:
            pass
        raise
    return raw_path


def _remote_paste_path() -> str:
    root = f"/tmp/pentacle-paste-{uuid.uuid4().hex}"
    return f"{root}/payload.txt"


# Staging deadline scales with payload size. Small prompt briefs / launch tokens
# keep the historical 10s floor; multi-MB binary attachments (phone photos,
# base64 +33%) get proportionally longer so a slow Tailscale ssh transfer no
# longer trips a fixed 10s deadline (root cause of "failed to stage attachment").
STAGE_TIMEOUT_FLOOR_S = 10.0
STAGE_TIMEOUT_CEILING_S = 180.0
STAGE_TIMEOUT_PER_MB_S = 12.0
# Payloads at or below this stay at the floor: prompt briefs, launch tokens and
# secrets are all KB-scale, so their staging deadline is left exactly unchanged.
STAGE_TIMEOUT_SMALL_PAYLOAD_BYTES = 256 * 1024

def _stage_timeout(nbytes: int) -> float:
    """Payload-size-aware staging deadline in seconds.

    Returns STAGE_TIMEOUT_FLOOR_S for small prompt briefs and grows linearly
    with the raw (pre-base64) payload size above STAGE_TIMEOUT_SMALL_PAYLOAD_BYTES,
    up to STAGE_TIMEOUT_CEILING_S.
    """
    over = max(0, nbytes - STAGE_TIMEOUT_SMALL_PAYLOAD_BYTES)
    mb = over / (1024 * 1024)
    scaled = STAGE_TIMEOUT_FLOOR_S + mb * STAGE_TIMEOUT_PER_MB_S
    return min(STAGE_TIMEOUT_CEILING_S, scaled)


class Tmux:
    """Every tmux call is an `exec` off the event loop — never a shell string,
    never a blocking subprocess (event-loop rule 1).

    A `ssh_target` makes this instance REMOTE: each call is exec'd as
    `ssh <opts> <target> <tmux ...>` via the lifted v1 command construction
    (`machines.ssh_tmux_command` — multiplexed connection, ConnectTimeout,
    ServerAlive), so the peer's tmux is driven over the same interface as the
    local one (`hosts.tmux_for`). Stdin (the paste buffer) is forwarded by SSH
    verbatim, so injection stays byte-exact."""

    def __init__(
        self, bin_path: str = "tmux", *, ssh_bin: str = "ssh",
        ssh_target: str | None = None, connect_timeout: float = 5.0,
    ) -> None:
        self.bin = bin_path
        self.ssh_bin = ssh_bin
        self.ssh_target = ssh_target
        self.connect_timeout = connect_timeout

    def _argv(self, args: tuple[str, ...]) -> list[str]:
        if self.ssh_target is None:
            return [self.bin, *args]
        return ssh_tmux_command(
            self.ssh_target, self.bin, args,
            ssh_bin=self.ssh_bin, connect_timeout=self.connect_timeout,
        )

    async def run(self, *args: str, stdin: bytes | None = None, timeout: float = 10.0) -> tuple[int, str]:
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._argv(args),
                stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await asyncio.wait_for(proc.communicate(stdin), timeout=timeout)
        except asyncio.TimeoutError as exc:
            await terminate_and_reap(proc)
            raise VerbError("tmux_timeout", f"tmux {args[0]} timed out after {timeout}s") from exc
        except asyncio.CancelledError:
            await terminate_and_reap(proc)
            raise
        except OSError as exc:
            await terminate_and_reap(proc)
            raise VerbError("tmux_failed", str(exc)) from exc
        return proc.returncode or 0, (out or b"").decode("utf-8", "replace")

    async def has_session(self, name: str) -> bool:
        rc, _ = await self.run("has-session", "-t", _target(name))
        return rc == 0

    async def cwd_exists(self, cwd: str, *, timeout: float = 10.0) -> bool:
        """Check a spawn cwd on this tmux transport's target host.

        Local checks never invoke a shell. Remote checks run one bounded,
        quoted ``test -d`` through the same SSH target used for tmux, so the
        daemon cannot accidentally inspect its own host for a remote spawn.
        ``False`` is reserved for a missing/non-directory path; transport and
        timeout failures remain typed errors rather than false product proof.
        """
        if self.ssh_target is None:
            try:
                return await asyncio.to_thread(Path(cwd).is_dir)
            except (OSError, ValueError):
                return False

        command = f"test -d {shlex.quote(cwd)}"
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *ssh_command(
                    self.ssh_target,
                    command,
                    ssh_bin=self.ssh_bin,
                    connect_timeout=self.connect_timeout,
                ),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            await terminate_and_reap(proc)
            raise VerbError("cwd_validation_failed", f"remote cwd check timed out after {timeout}s") from exc
        except asyncio.CancelledError:
            await terminate_and_reap(proc)
            raise
        except OSError as exc:
            await terminate_and_reap(proc)
            raise VerbError("cwd_validation_failed", str(exc)) from exc

        rc = proc.returncode
        if rc == 0:
            return True
        if rc == 1:
            return False
        detail = (out or b"").decode("utf-8", "replace").strip()
        raise VerbError(
            "cwd_validation_failed",
            detail or f"remote cwd check failed with exit {rc}",
        )

    async def session_state(self, name: str) -> str:
        """Tri-state liveness: `alive` / `gone` / `unreachable`.

        Over SSH, `has_session`'s bool collapses a connection failure (ssh exits
        255) into "no session", which would false-close a live remote pane. tmux
        itself exits 0 when the session exists and 1 when it does not, and ssh
        relays that exit code only when it CONNECTED — so any other code (255 no
        route, 127 no remote tmux) is a transport failure, never proof of death.
        Local tmux only ever yields 0/1, so this is correct on both paths."""
        rc, _ = await self.run("has-session", "-t", _target(name))
        if rc == 0:
            return "alive"
        if rc == 1:
            return "gone"
        return "unreachable"

    async def new_session(
        self, name: str, command: str, cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        args = ["new-session", "-d", "-s", name]
        if cwd:
            args += ["-c", cwd]
        # `-e KEY=VAL` injects the creation nonce (spec §D1) into the session's
        # environment so the pane is born carrying its identity — readable off
        # the live pane's process environment from the instant it exists, the
        # only signal that survives the F1 crash window. tmux 3.0+ supports
        # `-e`; the deploy host is 3.4.
        for key, value in (env or {}).items():
            args += ["-e", f"{key}={value}"]
        args.append(command)
        rc, out = await self.run(*args)
        if rc != 0:
            raise VerbError("tmux_launch_failed", out.strip() or "tmux new-session failed")

    async def capture_checked(self, name: str, *, timeout: float = 10.0) -> str:
        """Capture a pane and preserve a transport failure for proof callers.

        Routing-integrity sampling needs to distinguish an empty successful
        capture from a failed capture before it can mint live-pane proof. The
        ordinary ``capture`` method below intentionally keeps its historical
        empty-string-on-failure contract for delivery/readiness callers.
        """
        rc, out = await self.run(
            "capture-pane", "-p", "-J", "-t", _target(name), "-S", CAPTURE_LINES,
            timeout=timeout,
        )
        if rc != 0:
            raise VerbError("tmux_capture_failed", out.strip() or "tmux capture-pane failed")
        return out

    async def capture(self, name: str, *, timeout: float = 10.0) -> str:
        """`-J` joins wrapped lines: without it tmux breaks a long echo at the
        pane width and any needle spanning the break is invisible.

        This is a compatibility surface used after paste and by readiness,
        receipt, and mirror probes. A failed post-paste capture is inconclusive,
        not a failed delivery; routing-integrity proof callers use
        ``capture_checked`` explicitly.
        """
        try:
            return await self.capture_checked(name, timeout=timeout)
        except VerbError as exc:
            if exc.code == "tmux_capture_failed":
                return ""
            raise

    async def pane_pid(self, name: str) -> str:
        rc, out = await self.run("list-panes", "-t", _target(name), "-F", "#{pane_pid}")
        return out.strip().splitlines()[0] if rc == 0 and out.strip() else ""

    async def pane_identity(self, name: str) -> dict[str, str] | None:
        """Read the pane binding used for process-instance reaping proofs."""
        rc, out = await self.run(
            "list-panes", "-t", _target(name),
            "-F", "#{pane_pid}|#{pane_id}|#{pane_tty}|#{socket_path}|#{session_name}",
        )
        records = [line.split('|') for line in out.splitlines() if line.strip()] if rc == 0 else []
        if not records or any(
            len(fields) != 5 or not all(fields) or fields[4] != name
            or not fields[0].isascii() or not fields[0].isdigit() or int(fields[0]) <= 0
            for fields in records
        ):
            return None
        fields = records[0]
        return {
            "pane_pid": fields[0], "pane_id": fields[1], "tty": fields[2],
            "tmux_socket": fields[3], "session_name": fields[4],
        }

    async def kill_session(self, name: str) -> None:
        await self.run("kill-session", "-t", _target(name))

    async def kill_pane(self, pane_id: str) -> None:
        """Kill one already-resolved tmux pane, never a reused session name."""
        await self.run("kill-pane", "-t", pane_id)

    async def _cancel_copy_mode(self, name: str) -> bool:
        """Leave tmux copy-mode before a write can be consumed as a mode key."""
        target = _target(name)
        rc, out = await self.run(
            "display-message", "-p", "-t", target, "#{pane_in_mode} #{pane_mode}",
        )
        if rc != 0:
            log.warning("pane_mode_query_failed stream=%s detail=%s", name, out.strip())
            return False
        fields = out.strip().split(maxsplit=1)
        if not fields or fields[0] != "1":
            return False
        mode = fields[1] if len(fields) > 1 else "unknown"
        rc, out = await self.run("send-keys", "-t", target, "-X", "cancel")
        if rc != 0:
            log.warning("pane_mode_cancel_failed stream=%s mode=%s detail=%s", name, mode, out.strip())
            return True
        rc, out = await self.run(
            "display-message", "-p", "-t", target, "#{pane_in_mode} #{pane_mode}",
        )
        if rc != 0 or out.strip().split(maxsplit=1)[:1] == ["1"]:
            log.warning("pane_mode_cancel_failed stream=%s mode=%s detail=%s", name, mode, out.strip())
            return True
        log.warning("pane_mode_cancelled stream=%s mode=%s", name, mode)
        return False

    async def paste(self, name: str, text: str) -> str | None:
        """Human-equivalent injection (ledger req 1): ONE atomic bracketed
        paste of the whole message, then Enter. No chunking, no readiness
        gating, no stray control sequences. tmux 3.4 does not treat `-` as a
        portable stdin path for `load-buffer`, so stage one private local or
        remote file and load that path instead."""
        assert_injectable(text)  # the one chokepoint every injection passes
        pane_in_mode = await self._cancel_copy_mode(name)
        buf = f"v2-{uuid.uuid4().hex[:8]}"
        data = text.encode("utf-8")
        paste_path = _remote_paste_path() if self.ssh_target is not None else None
        local_path: str | None = None
        phase = "not_started"
        try:
            if paste_path is not None:
                await self.stage_text(paste_path, data)
            else:
                local_path = await asyncio.to_thread(_write_local_paste_file, data)
                paste_path = local_path

            rc, out = await self.run("load-buffer", "-b", buf, paste_path)
            if rc != 0:
                raise VerbError("paste_failed", out.strip() or "load-buffer failed", phase=phase)
            phase = "body_maybe_pasted"
            rc, out = await self.run("paste-buffer", "-b", buf, "-t", _target(name), "-d", "-p")
            if rc != 0:
                raise VerbError("paste_failed", out.strip() or "paste-buffer failed", phase=phase)
            # Returning from paste-buffer proves the bytes reached the pty, not
            # that a provider TUI finished folding the bracketed paste into its
            # composer. Give that transition one normal poll tick before Enter.
            await asyncio.sleep(POLL_INTERVAL_S)
            phase = "enter_failed"
            rc, out = await self.run("send-keys", "-t", _target(name), "Enter")
            if rc != 0:
                raise VerbError("paste_failed", out.strip() or "send-keys Enter failed", phase=phase)
        finally:
            if local_path is not None:
                await asyncio.to_thread(Path(local_path).unlink, missing_ok=True)
            elif paste_path is not None and self.ssh_target is not None:
                cleanup = (
                    f"rm -f -- {shlex.quote(paste_path)}; "
                    f"rmdir -- {shlex.quote(str(Path(paste_path).parent))}"
                )
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *ssh_command(
                            self.ssh_target,
                            cleanup,
                            ssh_bin=self.ssh_bin,
                            connect_timeout=self.connect_timeout,
                        ),
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                    )
                    await asyncio.wait_for(proc.communicate(), timeout=10.0)
                except Exception:  # noqa: BLE001 - delivery result already settled
                    log.warning("remote paste cleanup failed for %s", paste_path)
        return "pane_in_mode" if pane_in_mode else None

    async def send_enter(self, name: str) -> str | None:
        """Enter-only retry seam; callers must never paste the body again."""
        pane_in_mode = await self._cancel_copy_mode(name)
        rc, out = await self.run("send-keys", "-t", _target(name), "Enter")
        if rc != 0:
            raise VerbError(
                "paste_failed", out.strip() or "send-keys Enter failed", phase="enter_failed",
            )
        return "pane_in_mode" if pane_in_mode else None

    async def stage_text(self, path: str, data: bytes) -> None:
        """Atomically stage a prompt on the provider's host.

        Local writes run in the executor. Remote writes stream the bytes over
        the existing bounded SSH transport and perform the same temp-file plus
        rename sequence in the recipient host's shell. The provider therefore
        never observes a partially written brief.
        """
        if self.ssh_target is None:
            await asyncio.to_thread(_atomic_stage_local, Path(path), data)
            return
        temporary = f"{path}.tmp-{uuid.uuid4().hex[:12]}"
        parent = str(Path(path).parent)
        cleanup_action = f"rm -f -- {shlex.quote(temporary)}"
        command = (
            f"trap {shlex.quote(cleanup_action)} EXIT HUP INT TERM; "
            f"umask 077 && mkdir -p -- {shlex.quote(parent)} "
            f"&& chmod 700 {shlex.quote(parent)} "
            f"&& base64 -d > {shlex.quote(temporary)} "
            f'&& test "$(wc -c < {shlex.quote(temporary)})" -eq {len(data)} '
            f"&& chmod 600 {shlex.quote(temporary)} "
            f"&& mv -f {shlex.quote(temporary)} {shlex.quote(path)}; "
            "rc=$?; if [ \"$rc\" -ne 0 ]; then exit \"$rc\"; fi; "
            "trap - EXIT HUP INT TERM; exit 0"
        )
        stage_timeout = _stage_timeout(len(data))
        encoded = await asyncio.to_thread(base64.b64encode, data)
        try:
            proc = await asyncio.create_subprocess_exec(
                *ssh_command(self.ssh_target, command, ssh_bin=self.ssh_bin, connect_timeout=self.connect_timeout),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as exc:
            raise VerbError("prompt_stage_failed", str(exc)) from exc
        try:
            out, _ = await asyncio.wait_for(proc.communicate(encoded), timeout=stage_timeout)
        except asyncio.CancelledError:
            await terminate_and_reap(proc)
            raise
        except asyncio.TimeoutError as exc:
            await terminate_and_reap(proc)
            raise VerbError(
                "prompt_stage_failed",
                f"remote staging timed out after {stage_timeout:.0f}s "
                f"(payload {len(data)} bytes)",
            ) from exc
        if proc.returncode != 0:
            detail = (out or b"").decode("utf-8", "replace").strip()
            if proc.returncode == 255:
                raise VerbError(
                    "prompt_stage_failed",
                    f"remote host unreachable: {detail}" if detail else "remote host unreachable",
                )
            raise VerbError("prompt_stage_failed", detail or "remote prompt staging failed")
        # The remote command enforces the private directory/file modes in the
        # same bounded transport round trip.



async def _exec(*args: str, timeout: float = 10.0) -> tuple[int, str]:
    """Run an arbitrary binary off the event loop, tolerating every failure.

    Used for the transcript probe's `ps`/`lsof` (QA #16) and the host probe's
    `ssh ... true` (hosts.py). `lsof` is resolved from PATH, then its standard
    system locations, so a restricted daemon PATH still discovers transcripts.
    A missing executable is logged and returns a distinct result; timeout and
    non-zero exit remain inconclusive without raising into the probe loop. A
    timed-out child is KILLED before returning so a hung `ssh` never leaks."""
    if args and args[0] == "lsof":
        executable = shutil.which("lsof")
        if executable is None:
            executable = next(
                (
                    candidate
                    for candidate in ("/usr/sbin/lsof", "/sbin/lsof")
                    if os.path.isfile(candidate) and os.access(candidate, os.X_OK)
                ),
                "lsof",
            )
        args = (executable, *args[1:])
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        log.error("subprocess executable not found: %s", args[0] if args else "<empty>")
        return 127, f"executable not found: {args[0] if args else '<empty>'}"
    except OSError:
        return 1, ""
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.CancelledError:
        await terminate_and_reap(proc)
        raise
    except asyncio.TimeoutError:
        await terminate_and_reap(proc)
        return 1, ""
    return proc.returncode or 0, (out or b"").decode("utf-8", "replace")


def _search_transcripts(paths: list[str], needle: str) -> bool:
    """True if `needle` (collapsed) appears in the tail of any transcript file.

    Runs in a worker thread (blocking file I/O off the loop). Every read is
    guarded — an unreadable or vanished log is simply not-found, never a raise."""
    for path in paths:
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as fh:
                if size > MAX_TRANSCRIPT_BYTES:
                    fh.seek(size - MAX_TRANSCRIPT_BYTES)
                data = fh.read()
        except OSError:
            continue
        if needle in collapse_ws(data.decode("utf-8", "replace")):
            return True
    return False


def collapse_ws(text: str) -> str:
    """Whitespace-collapsed view of pane text. Applied to BOTH sides of the
    receipt comparison so pane width cannot decide whether a message was
    delivered: tmux wraps, pads and re-indents, and a false negative here made
    spawn kill a session that HAD received its brief."""
    return " ".join(text.split())


#: The provider TUIs collapse a large paste into a ONE-LINE placeholder instead
#: of echoing the pasted text — lifted from v1 `session.py`
#: (`claude_active_draft_has_collapsed_paste` + the codex `[Pasted Content N
#: chars]` form). When this is on screen the receipt's echo needle can NEVER
#: repaint (the brief's tail is inside the collapsed block), so the fast pane
#: tier is provably blind and the receipt must defer to the transcript tier.
_PASTE_PLACEHOLDER = {
    "claude": re.compile(r"\[Pasted text #\d+(?: [^\]]*)?\]"),
    "codex": re.compile(r"\[Pasted Content \d+ chars\]"),
}


def has_collapsed_paste(provider: str, pane_text: str) -> bool:
    """True iff `pane_text` shows `provider`'s collapsed-paste placeholder."""
    pat = _PASTE_PLACEHOLDER.get(provider)
    return bool(pat and pat.search(pane_text))


#: How much of the pre-paste pane tail to anchor the receipt search on. Long
#: enough to be distinctive across a poll, bounded so the search stays O(n).
RECEIPT_ANCHOR_LEN = 120


def new_since(before: str, after: str) -> str:
    """The slice of `after` that appeared AFTER `before` was captured.

    The pane appends at the bottom and drops lines off the top, so `after` is
    `before`'s surviving tail followed by new text. Anchor on the tail of
    `before` (bounded length, shrunk only if tmux reflowed it away) and return
    everything past its LAST occurrence — so a receipt is observed off the echo
    THIS paste produced, never an identical earlier one still on screen (QA
    #15). Both strings are already whitespace-collapsed by the caller."""
    if not before:
        return after
    anchor = before[-RECEIPT_ANCHOR_LEN:]
    while anchor:
        idx = after.rfind(anchor)
        if idx != -1:
            return after[idx + len(anchor):]
        anchor = anchor[1:]
    return after


def receipt_needle(text: str) -> str:
    """The distinctive tail of a message, used to observe its echo in the pane.
    Normalized and short by construction — see NEEDLE_MAX."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return collapse_ws(lines[-1] if lines else text)[:NEEDLE_MAX].strip()


#: Columns a spawn asks `sessions.open` to persist. Named once so the crash-safe
#: intent (written before the pane exists) and the real open can never drift.
OPEN_FIELD_KEYS = (
    "parent_stream_id", "handoff_from_stream_id", "role", "provider",
    "phase", "spec_id", "spec_ids",
    # Lifecycle authorization is independent of whether the spawn carries an
    # initial prompt. The report --terminate close leg reads this durable bit.
    "self_close_on_completion", "no_watch",
    # Arm (a) of the removed Codex runtime guard: the receipt echoes the
    # effective tuple at launch. Unobserved stays explicit (req 2).
    "effective_model", "effective_effort", "opened_by_host_id", "title",
    "spec_resolution", "qualified_spec_ids",
    "spec_binding_provenance",
)


def _intent(payload: Any) -> dict[str, Any]:
    """A reservation's persisted spawn intent. Unreadable payload -> adopt with
    what the pane itself provides; losing the row is the worse failure."""
    if not payload:
        return {}
    try:
        loaded = json.loads(str(payload))
    except ValueError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def open_fields(msg: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {k: msg.get(k) for k in OPEN_FIELD_KEYS}
    raw_spec_ids = msg.get("spec_ids")
    if isinstance(raw_spec_ids, str):
        fields["spec_ids"] = [raw_spec_ids]
    elif isinstance(raw_spec_ids, (list, tuple, set)):
        fields["spec_ids"] = [str(item) for item in raw_spec_ids if str(item).strip()]
    elif msg.get("spec_id"):
        fields["spec_ids"] = [str(msg["spec_id"])]
    else:
        fields["spec_ids"] = None
    # `sessions.self_close_on_completion` is NOT NULL. Normalize the optional
    # wire field on every spawn shape so bare spawns do not lose the lifecycle
    # state that prompt-bearing flows happen to exercise first.
    fields["self_close_on_completion"] = msg.get("self_close_on_completion") is True
    fields["objective"] = msg.get("objective")
    fields["objective_source"] = msg.get("objective_source")
    fields["visibility"] = msg.get("visibility") or (
        "hidden" if msg.get("parent_stream_id") and msg.get("role") != "nexus"
        and not (msg.get("handoff") or msg.get("handoff_from_stream_id")) else "default"
    )
    fields["no_watch"] = msg.get("no_watch") is True
    fields["requested_model"] = msg.get("model")
    fields["requested_effort"] = msg.get("effort")
    return fields
