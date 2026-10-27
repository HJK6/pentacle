"""Pinned native subscription adapters; never decode or export credentials.

All subprocess output stays in memory and is discarded on failure. Callers
receive safe codes and allowlisted metadata only. Native authorization is
invoked solely by the account recovery coordinator, after daemon admission.
"""
from __future__ import annotations
import contextlib
import json
import math
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time
import re

from provider_accounts import AccountError, Binding, environment


def _stop(process, *, interrupt=False):
    if interrupt and process.poll() is None:
        # The Python /usage adapter must run its owned-tmux finally block.
        with contextlib.suppress(ProcessLookupError, PermissionError): os.killpg(process.pid, signal.SIGINT)
        with contextlib.suppress(subprocess.TimeoutExpired): process.wait(timeout=10)
    with contextlib.suppress(ProcessLookupError, PermissionError): os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired): process.wait(timeout=5)
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream: stream.close()


def classify_error(value) -> str:
    """Map provider output in memory to safe codes; never return the output."""
    text = json.dumps(value) if isinstance(value, dict) else str(value or "")
    lowered = text.lower()
    if re.search(r'\b401\b|authentication_error|not logged in|please (?:run /login|log in)|invalid authentication|login expired', lowered):
        return "provider_auth_failed"
    if "server_overloaded" in lowered or "overloaded_error" in lowered or "capacity" in lowered:
        return "provider_capacity"
    if "rate_limit" in lowered or "quota" in lowered or re.search(r'\b429\b', lowered):
        return "provider_quota_blocked"
    if re.search(r'\b403\b|permission_denied', lowered): return "provider_permission_denied"
    if re.search(r'connection|transport|network|timed out|timeout', lowered): return "provider_transport_failed"
    return "provider_rpc_failed"


def native_run(argv, *, input=None, capture_output=True, text=True, timeout=90, interrupt_cleanup=False, **kwargs):
    """Own and reap every native/probe descendant on all exits, including alarm."""
    process = subprocess.Popen(argv, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=text, **kwargs)
    try:
        stdout, stderr = process.communicate(input=input, timeout=timeout)
        if len(stdout) + len(stderr) > 2_000_000: raise AccountError("provider_response_invalid")
        return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
    finally: _stop(process, interrupt=interrupt_cleanup)


class CodexRPC:
    def __init__(self, binding: Binding, *, timeout=60, popen=subprocess.Popen):
        self.timeout, self.pending, self.sequence = timeout, bytearray(), 0
        self.account_status = None
        self.process = popen([binding.executable, "-c", 'cli_auth_credentials_store="file"',
                             "-c", 'forced_login_method="chatgpt"', "-c", 'model_provider="openai"', "app-server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=environment(binding), start_new_session=True, umask=0o077)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)

    def __enter__(self):
        try:
            self.call("initialize", {"clientInfo": {"name": "pentacle-account-observer", "version": "1"}})
            self.send({"method": "initialized"})
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        self.selector.close(); _stop(self.process)

    def send(self, value):
        try:
            self.process.stdin.write((json.dumps(value) + "\n").encode()); self.process.stdin.flush()
        except (BrokenPipeError, OSError): raise AccountError("provider_transport_failed") from None

    def read(self, deadline):
        while b"\n" not in self.pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self.selector.select(remaining):
                raise AccountError("provider_timeout")
            data = os.read(self.process.stdout.fileno(), 65536)
            if not data: raise AccountError("provider_transport_failed")
            self.pending.extend(data)
            if len(self.pending) > 2_000_000: raise AccountError("provider_response_invalid")
        line, _, rest = self.pending.partition(b"\n"); self.pending = bytearray(rest)
        try:
            value = json.loads(line)
            if not isinstance(value, dict): raise ValueError()
            return value
        except ValueError: raise AccountError("provider_response_invalid") from None

    def call(self, method, params=None):
        self.sequence += 1
        identifier = self.sequence
        self.send({"id": identifier, "method": method, "params": params or {}})
        deadline = time.monotonic() + self.timeout
        startup_status = None
        while True:
            value = self.read(deadline)
            if value.get("method") == "account/updated":
                if method not in {"initialize", "account/login/start"}:
                    status = value.get("params")
                    # Announcements may arrive before or after the first read.
                    # Only that authoritative read establishes the status;
                    # announcements never establish the quota identity.
                    if ((self.account_status is None and method != "account/read")
                            or not isinstance(status, dict) or set(status) != {"authMode", "planType"}
                            or status["authMode"] != "chatgpt"
                            or not isinstance(status["planType"], str) or not status["planType"]
                            or (self.account_status is not None and status != self.account_status)
                            or (startup_status is not None and status != startup_status)):
                        raise AccountError("provider_identity_changed")
                    startup_status = status
            if value.get("id") == identifier:
                if "error" in value: raise AccountError(classify_error(value["error"]))
                result = value.get("result")
                if not isinstance(result, dict): raise AccountError("provider_response_invalid")
                if method == "account/read":
                    account = result.get("account")
                    if startup_status is not None and (not isinstance(account, dict)
                            or account.get("type") != startup_status["authMode"]
                            or account.get("planType") != startup_status["planType"]):
                        raise AccountError("provider_identity_changed")
                    if (self.account_status is None and isinstance(account, dict)
                            and account.get("type") == "chatgpt"
                            and isinstance(account.get("planType"), str) and account["planType"]):
                        self.account_status = {"authMode": account["type"], "planType": account["planType"]}
                return result


def codex_observation(binding: Binding, *, rpc_factory=CodexRPC, now=time.time, expected_email=None) -> dict:
    with rpc_factory(binding) as rpc:
        account = rpc.call("account/read", {"refreshToken": False}).get("account") or {}
        if not account: raise AccountError("provider_auth_failed")
        if account.get("type") != "chatgpt" or account.get("planType") in {
                None, "unknown", "free", "self_serve_business_usage_based", "enterprise_cbp_usage_based"}:
            raise AccountError("subscription_identity_unverified")
        if expected_email is not None and (not isinstance(account.get("email"), str)
                or account["email"].casefold() != expected_email.casefold()):
            raise AccountError("subscription_identity_unverified")
        result = rpc.call("account/rateLimits/read")
        observed = now()
        identity = result.get("accountId")
        if not isinstance(identity, str) or not identity:
            raise AccountError("provider_identity_unavailable")
        after = rpc.call("account/read", {"refreshToken": False}).get("account") or {}
        if account != after: raise AccountError("provider_identity_changed")
        windows = {}
        buckets = result.get("rateLimitsByLimitId") or {"codex": result.get("rateLimits")}
        if not isinstance(buckets, dict): raise AccountError("provider_windows_invalid")
        for bucket_id, bucket in buckets.items():
            if not isinstance(bucket, dict): raise AccountError("provider_windows_invalid")
            for kind in ("primary", "secondary"):
                window = bucket.get(kind)
                if window is None: continue
                duration = window.get("windowDurationMins")
                if not isinstance(duration, int) or isinstance(duration, bool) or duration <= 0:
                    raise AccountError("provider_windows_invalid")
                key = f"{bucket_id}:{duration}"
                if key in windows: raise AccountError("provider_windows_conflicting")
                windows[key] = {"pct": window.get("usedPercent"), "reset_at": window.get("resetsAt"),
                                "reset_id": str(window.get("resetsAt"))}
        return {"provider_id": identity, "pool_id": identity, "auth_mode": "subscription",
                "observed_at": observed, "windows": windows, "collection": "ok",
                "ordinary_usage_allowed": (result.get("ordinaryUsageAllowed")
                    if type(result.get("ordinaryUsageAllowed")) is bool else None),
                "email_verified": expected_email is not None}


def _run(argv, *, binding, run=subprocess.run, cwd=None, timeout=90):
    try:
        if run is subprocess.run: run = native_run
        result = run(argv, env=environment(binding), capture_output=True, text=True,
                     cwd=cwd, timeout=timeout, start_new_session=True, umask=0o077)
    except (OSError, subprocess.SubprocessError): raise AccountError("provider_probe_failed") from None
    if result.returncode: raise AccountError(classify_error((result.stdout or "") + (result.stderr or "")))
    return result.stdout


def claude_identity(binding: Binding, *, run=subprocess.run, expected_email=None) -> dict:
    try:
        status = json.loads(_run([binding.executable, "auth", "status", "--json"], binding=binding, run=run, timeout=10))
    except ValueError: raise AccountError("provider_response_invalid") from None
    if status.get("loggedIn") is False: raise AccountError("provider_auth_failed")
    if (status.get("loggedIn") is not True or status.get("authMethod") != "claude.ai"
            or status.get("apiProvider") != "firstParty" or status.get("configDirectory") != binding.home
            or status.get("subscriptionType") not in {"pro", "max", "team", "enterprise"}
            or not isinstance(status.get("orgId"), str) or not status["orgId"]):
        raise AccountError("subscription_identity_unverified")
    try:
        login = json.loads((Path(binding.home) / ".claude.json").read_text()).get("oauthAccount") or {}
    except (OSError, ValueError): raise AccountError("subscription_identity_unverified") from None
    # Organization identifies the shared quota pool, not the individual login.
    # Match the native status to its named-home login cache before using UUIDs.
    if (not isinstance(login.get("accountUuid"), str) or not login["accountUuid"]
            or login.get("organizationUuid") != status["orgId"]
            or not status.get("email") or login.get("emailAddress") != status["email"]):
        raise AccountError("subscription_identity_unverified")
    if expected_email is not None and status["email"].casefold() != expected_email.casefold():
        raise AccountError("subscription_identity_unverified")
    return {"provider_id": login["accountUuid"], "pool_id": status["orgId"], "auth_mode": "subscription",
            "email_verified": expected_email is not None}


def _claude_cache(binding):
    try:
        data = json.loads((Path(binding.home) / ".claude.json").read_text())
    except (OSError, ValueError): raise AccountError("provider_cache_unavailable") from None
    if not isinstance(data, dict): raise AccountError("provider_cache_stale")
    cache, login = data.get("cachedUsageUtilization"), data.get("oauthAccount")
    return (cache if isinstance(cache, dict) else {}, login if isinstance(login, dict) else {})


def _finite_time(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool): return False
    try: return math.isfinite(value)
    except OverflowError: return False


def _wait_claude_refresh(binding, identity, started, now):
    # The pinned native writer throttles cache writes for 60s. The daemon's
    # existing host slot excludes its collector; other native writers may still
    # provide a valid after-fence fetch. Never extend the monotonic wait budget.
    deadline = time.monotonic() + 61
    previous = started
    while True:
        cache, login = _claude_cache(binding)
        account = cache.get("accountUuid")
        if ((isinstance(account, str) and account and account != identity["provider_id"])
                or login.get("accountUuid") != identity["provider_id"]
                or login.get("organizationUuid") != identity["pool_id"]):
            raise AccountError("provider_identity_or_cache_unverified")
        current, stamp = now(), cache.get("fetchedAtMs")
        if not _finite_time(current) or current < previous:
            raise AccountError("provider_cache_stale")
        previous = current
        if not isinstance(account, str) or not account or not _finite_time(stamp):
            return current  # One collection can repair missing cache metadata.
        fetched = stamp / 1000
        if fetched > current: raise AccountError("provider_cache_stale")
        if fetched >= started: return current
        delay = fetched + 60.5 - current
        if delay <= 0: return current
        remaining = deadline - time.monotonic()
        if remaining <= 0: raise AccountError("provider_cache_stale")
        time.sleep(min(delay, remaining))


def claude_observation(binding: Binding, *, collect_usage, run=subprocess.run, now=time.time, expected_email=None,
                       wait_for_refresh=False) -> dict:
    started = now()
    if not _finite_time(started): raise AccountError("provider_cache_stale")
    identity = claude_identity(binding, run=run, expected_email=expected_email)
    last_wall = _wait_claude_refresh(binding, identity, started, now) if wait_for_refresh else started
    # collect_usage drives the existing bounded /usage adapter with THIS env,
    # pinned executable and a configured trusted cwd. Never use the default cache.
    collect_usage(binding, environment(binding))
    cache, login = _claude_cache(binding)
    account = cache.get("accountUuid")
    if (login.get("organizationUuid") != identity["pool_id"]
            or login.get("accountUuid") != identity["provider_id"]
            or (isinstance(account, str) and account and account != identity["provider_id"])
            or claude_identity(binding, run=run, expected_email=expected_email) != identity):
        raise AccountError("provider_identity_or_cache_unverified")
    ended, stamp = now(), cache.get("fetchedAtMs")
    if (not _finite_time(ended) or ended < last_wall or not _finite_time(stamp)
            or not started <= stamp / 1000 <= ended
            or not isinstance(account, str) or not account):
        raise AccountError("provider_cache_stale")
    usage = cache.get("utilization")
    if not isinstance(usage, dict): raise AccountError("provider_windows_invalid")
    windows = {}
    from datetime import datetime
    for key, window in usage.items():
        # Billing/breakdown metadata shares this cache with timed quotas.
        if not isinstance(window, dict) or "resets_at" not in window: continue
        if not isinstance(window["resets_at"], str): raise AccountError("provider_windows_invalid")
        try:
            reset = datetime.fromisoformat(window["resets_at"].replace("Z", "+00:00")).timestamp()
        except (KeyError, ValueError, TypeError): raise AccountError("provider_windows_invalid") from None
        windows[key] = {"pct": window.get("utilization"), "reset_at": reset, "reset_id": window["resets_at"]}
    return identity | {"observed_at": stamp / 1000, "windows": windows, "collection": "ok"}


def verify_binary(binding: Binding, *, run=subprocess.run):
    import hashlib
    try: digest = hashlib.sha256(Path(binding.executable).read_bytes()).hexdigest()
    except OSError: raise AccountError("provider_executable_missing") from None
    if not binding.executable_sha256 or digest != binding.executable_sha256:
        raise AccountError("provider_executable_changed")
    if binding.observer_executable:
        try: observed_digest = hashlib.sha256(Path(binding.observer_executable).read_bytes()).hexdigest()
        except OSError: raise AccountError("provider_executable_missing") from None
        if not binding.observer_executable_sha256 or observed_digest != binding.observer_executable_sha256:
            raise AccountError("provider_executable_changed")
    actual = _run([binding.observer, "--version"], binding=binding, run=run, timeout=10).strip()
    if actual != binding.version: raise AccountError("provider_version_changed")


def native_login(binding: Binding, browser_authorize, *, rpc_factory=CodexRPC):
    """Native CLI writes its own store at the final home; no credential copy."""
    if binding.provider == "codex":
        with rpc_factory(binding, timeout=180) as rpc:
            result = rpc.call("account/login/start", {"type": "chatgpt"})
            if result.get("type") != "chatgpt" or not result.get("authUrl"):
                raise AccountError("provider_login_failed")
            outcome = browser_authorize(binding, result["authUrl"])
            if outcome.get("outcome") != "ok": return "human_reseed_required"
            deadline = time.monotonic() + 120
            while True:
                event = rpc.read(deadline)
                if event.get("method") == "account/login/completed":
                    params = event.get("params") or {}
                    if params.get("loginId") != result.get("loginId"): continue
                    return "ok" if params.get("success") is True else "provider_login_failed"
    # Claude's native auth command uses a PTY; URL and callback code remain in
    # memory. The helper runs at the FINAL explicitly set config directory on
    # both platforms, including the directory-keyed macOS Keychain.
    import pty
    import select
    import re
    master, slave = pty.openpty()
    env = environment(binding) | {"BROWSER": "/usr/bin/true"}
    process = None
    try:
        process = subprocess.Popen([binding.executable, "auth", "login", "--claudeai"],
            env=env, stdin=slave, stdout=slave, stderr=slave, start_new_session=True, umask=0o077)
        os.close(slave); slave = -1
        deadline, output, sent = time.monotonic() + 180, "", False
        while time.monotonic() < deadline:
            if not select.select([master], [], [], min(1, max(0, deadline - time.monotonic())))[0]:
                if process.poll() is not None: break
                continue
            try: data = os.read(master, 16384)
            except OSError: break
            if not data: break  # macOS PTYs return EOF instead of EIO.
            output += data.decode(errors="replace")
            if len(output) > 262144: raise AccountError("provider_response_invalid")
            compact = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]|\s+", "", output)
            if not sent:
                match = re.search(r"https://(?:claude\.ai|claude\.com)/[^\s]+", compact)
                if match and "state=" in match[0]:
                    url = re.split(r"Pastecode|Paste|If", match[0])[0]
                    outcome = browser_authorize(binding, url)
                    if outcome.get("outcome") != "ok": return "human_reseed_required"
                    code = outcome.get("code")
                    if code:
                        os.write(master, (code + "\r").encode())
                    sent = True
        try:
            code = process.wait(timeout=min(2, max(0.01, deadline - time.monotonic())))
        except subprocess.TimeoutExpired:
            return "human_reseed_required"
        return "ok" if sent and code == 0 else "provider_login_failed"
    finally:
        if process is not None: _stop(process)
        os.close(master)
        if slave >= 0: os.close(slave)


def minimal_turn(binding: Binding, *, model: str, effort: str, cwd: str, run=subprocess.run):
    if not model or not effort or not Path(cwd).is_absolute() or Path(cwd).is_relative_to(Path(binding.home)):
        raise AccountError("provider_probe_config_invalid")
    command = ([binding.executable, "-c", 'cli_auth_credentials_store="file"', "-c", 'forced_login_method="chatgpt"', "-c", 'model_provider="openai"',
                "exec", "--skip-git-repo-check", "--sandbox", "read-only", "--model", model,
                "-c", "model_reasoning_effort=" + json.dumps(effort), "Reply exactly OK"]
               if binding.provider == "codex" else
               [binding.executable, "--setting-sources", "user", "--settings", '{"forceLoginMethod":"claudeai","apiKeyHelper":""}',
                "-p", "Reply exactly OK", "--model", model, "--effort", effort, "--tools", ""])
    if _run(command, binding=binding, run=run, cwd=cwd).strip() != "OK":
        raise AccountError("provider_minimal_turn_failed")


def enroll(registry, draft: Binding, *, expected_provider_id: str, browser_authorize,
           login, identity, model: str, effort: str, cwd: str, manifest=None, run=subprocess.run):
    """Execute native login in the FINAL home and atomically activate on proof.

    ``login`` is the pinned provider's native authorization driver; the browser
    callback resolves the secure profile reference locally. It cannot change
    home, destination or expected identity. Exceptions expose safe codes only.
    No failed generation is renamed, copied or installed over the old login.
    """
    with registry.lock():
        verify_binary(draft, run=run)
        registry.provision(draft, manifest or {})
        try:
            outcome = login(draft, browser_authorize)
            if outcome != "ok":
                raise AccountError(outcome if outcome in {"human_reseed_required", "cancelled", "locked_keychain"}
                                   else "provider_login_failed")
            if draft.provider == "claude" and draft.platform == "macos":
                fallback = Path(draft.home) / ".credentials.json"
                if fallback.exists() or fallback.is_symlink(): raise AccountError("locked_keychain")
            receipt = identity(draft)
            intended_id = expected_provider_id
            if intended_id is None and receipt.get("email_verified") is True:
                intended_id = receipt.get("provider_id")
            if (not intended_id or receipt.get("provider_id") != intended_id or receipt.get("auth_mode") != "subscription"):
                raise AccountError("account_enrollment_unverified")
            minimal_turn(draft, model=model, effort=effort, cwd=cwd, run=run)
            if identity(draft) != receipt: raise AccountError("provider_identity_changed")
            return registry.activate(draft, receipt | {
                "store": "keychain" if draft.provider == "claude" and draft.platform == "macos" else "file",
                "minimal_turn": True, "independent_login": True}, expected_provider_id=intended_id)
        except AccountError: raise
        except Exception: raise AccountError("provider_login_failed") from None


def adopt(registry, draft: Binding, *, expected_provider_id: str, identity, model: str, effort: str, cwd: str,
          manifest=None, run=subprocess.run):
    """Register a host-configured, already signed-in Codex home in place.

    No login runs and no credential file is created, copied, moved or renamed;
    provisioning writes only absent bootstrap files. Activation requires the
    expected subscription identity, a real minimal turn and an unchanged reread.
    """
    if draft.provider != "codex" or draft.home not in registry.adopted:
        raise AccountError("account_adopt_unsupported")
    with registry.lock():
        registry.provision(draft, manifest or {})
        verify_binary(draft, run=run)
        try:
            receipt = identity(draft)
            if (not expected_provider_id or receipt.get("provider_id") != expected_provider_id
                    or receipt.get("auth_mode") != "subscription"):
                raise AccountError("account_enrollment_unverified")
            minimal_turn(draft, model=model, effort=effort, cwd=cwd, run=run)
            if identity(draft) != receipt: raise AccountError("provider_identity_changed")
            return registry.activate(draft, receipt | {"store": "file", "minimal_turn": True, "adopted_home": True},
                expected_provider_id=expected_provider_id)
        except AccountError: raise
        except Exception: raise AccountError("account_enrollment_unverified") from None
