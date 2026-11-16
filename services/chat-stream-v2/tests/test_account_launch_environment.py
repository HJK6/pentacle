"""Execute the real shell envelope against a fake provider, with fake secrets."""
from dataclasses import replace
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest
import launch
from provider_accounts import Binding


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_managed_launch_preserves_tuple_and_clears_all_auth_overrides(tmp_path, provider):
    output = tmp_path / "observed.json"
    fake = tmp_path / "fake provider"
    fake.write_text("#!" + sys.executable + "\nimport json,os,sys\n" +
        "with open(" + repr(str(output)) + ", 'w') as f: json.dump({'argv':sys.argv[1:], 'env':dict(os.environ)},f)\n")
    fake.chmod(0o700)
    home = tmp_path / "named account's home"; home.mkdir(mode=0o700)
    account = Binding(provider, "primary", "host-a", str(home), "login-a", "browser-a", str(fake), "fixture", "macos")
    machine = launch.local_machine("host-a", cwd=str(tmp_path), codex_bin=str(fake), claude_bin=str(fake))
    plan = launch.build_launch(machine, provider=provider, tmux_session="fake-provider", launch_model="fixture-model",
        launch_effort="high", account_binding=account)
    poison = {key: "fake-only" for key in (
        "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_IDENTITY_TOKEN_FILE", "OPENAI_FEDERATION_RULE_ID",
        "CODEX_AUTH_TOKEN", "ACCESS_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_PROFILE",
        "ANTHROPIC_BASE_URL", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "BASH_ENV", "ENV")}
    result = subprocess.run(["/bin/sh", "-c", plan.command], env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
        "USER": "fixture-user", **poison}, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    observed = json.loads(output.read_text())
    selected_home_key = "CODEX_HOME" if provider == "codex" else "CLAUDE_CONFIG_DIR"
    assert observed["env"][selected_home_key] == str(home)
    assert not (set(poison) - {selected_home_key}).intersection(observed["env"])
    assert observed["env"]["HOME"] == str(tmp_path)
    assert "fixture-model" in observed["argv"] and ("high" in observed["argv"] or "model_reasoning_effort=high" in observed["argv"])
    assert observed["env"]["PENTACLE_STREAM_ID"] == "host-a:fake-provider"
    assert "AGENT_ORCH_STREAM_TOKEN" not in observed["env"]
    if provider == "claude":
        assert observed["argv"][observed["argv"].index("--setting-sources") + 1] == "user"
        assert Path(plan.jsonl_path).is_relative_to(home / "projects")
    else:
        assert 'forced_login_method="chatgpt"' in observed["argv"]


def test_named_resume_rejects_other_home(tmp_path):
    machine = launch.local_machine("host-a", cwd=str(tmp_path), claude_bin="/bin/echo")
    account = Binding("claude", "primary", "host-a", str(tmp_path / "a"), "login-a", "browser", "/bin/echo", "fixture", "macos")
    with pytest.raises(ValueError, match="account_resume_cross_home_forbidden"):
        launch.build_launch(machine, provider="claude", tmux_session="fixture", launch_model="fixture-model", launch_effort="high",
            resume_session_id="fixture-id", resume_jsonl_path=str(tmp_path / "other" / "native.jsonl"), account_binding=account)


def test_named_wrapper_preserves_nonsecret_context_and_pins_native_binary(tmp_path):
    native=tmp_path/'native-provider';output=tmp_path/'wrapper.json'
    native.write_text('#!'+sys.executable+'\nimport json,os\nfrom pathlib import Path\nPath('+repr(str(output))+').write_text(json.dumps({k:os.environ.get(k) for k in ["CLAUDE_CONFIG_DIR","PENTACLE_AUTH_SSM_PARAMETER","PENTACLE_AUTH_LOGIN_KEYCHAIN","PENTACLE_AUTH_CLAUDE_BIN"]}))\n')
    native.chmod(0o700)
    wrapper=tmp_path/'context-wrapper';wrapper.write_text('#!/bin/sh\nexec "$PENTACLE_AUTH_CLAUDE_BIN" "$@"\n');wrapper.chmod(0o700)
    account=Binding('claude','primary','host-a',str(tmp_path/'named'),'login-a','browser',str(wrapper),'fixture','macos',
        observer_executable=str(native))
    machine=launch.local_machine('host-a',cwd=str(tmp_path),claude_bin=str(wrapper))
    plan=launch.build_launch(machine,provider='claude',tmux_session='wrapped',launch_model='fixture-model',launch_effort='high',account_binding=account)
    result=subprocess.run(['/bin/sh','-c',plan.command],env={'PATH':'/usr/bin:/bin','HOME':str(tmp_path),
        'PENTACLE_AUTH_SSM_PARAMETER':'/fixture/nonsecret-reference','PENTACLE_AUTH_LOGIN_KEYCHAIN':'/fixture/login.keychain-db',
        'PENTACLE_AUTH_CLAUDE_BIN':'/invalid/inherited'},capture_output=True,text=True,timeout=5)
    assert result.returncode==0,result.stderr
    data=json.loads(output.read_text())
    assert data['PENTACLE_AUTH_SSM_PARAMETER']=='/fixture/nonsecret-reference'
    assert data['PENTACLE_AUTH_CLAUDE_BIN']==str(native) and data['CLAUDE_CONFIG_DIR']==account.home
