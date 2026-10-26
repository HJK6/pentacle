"""Package-wide egress and credential ownership inventory, with mutation proof."""
from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path
import shutil

import pytest

PACKAGE = Path(__file__).parents[1] / "agent_orch"
BOUNDARY = "send_boundary"
KEYS = {"stream_token", "identity_token", "token"}
ENV = {"AGENT_ORCH_TOKEN", "AGENT_ORCH_STREAM_TOKEN", "AGENT_ORCH_STREAM_TOKEN_FILE", "PENTACLE_WORK_REPORT_READER_TOKEN"}
READS = {"stream_token_from_env", "_stream_token_from_env", "_stream_token_from_file", "_file_token", "_token_path", "decode_envelope"}
CONNECTS = {("wsclient", name) for name in ("WebsocketClient._connect_and_read", "_fetch_snapshot_async", "_connect_ready", "_connect_rpc_ready", "_catalog_operator_rpc")}
CONNECTS.add(("work_report_cli", "get_report"))
UNCREDED = {
    ("wsclient", "WebsocketClient._handle_message", "pong"),
    ("wsclient", "_fetch_snapshot_async", "pong"), ("wsclient", "_connect_ready", "pong"),
    ("wsclient", "_read_rpc_frame", "pong"), ("wsclient", "_catalog_operator_rpc", "pong"),
    ("wsclient", "upload_blob_once", "upload_blob_chunk"),
    ("wsclient", "upload_prompt_blob_once", "upload_prompt_blob_chunk"),
    ("wsclient", "_fetch_blob_once_attempt", "fetch_blob"),
}
# Each tuple stands for one of the 30 base send sites. The path is a real
# fake-socket journey, not a unit invocation of the new boundary.
BASE_ROUTING = [
    ("wsclient", "WebsocketClient._connect_and_read", "hello", "persistent"),
    ("wsclient", "WebsocketClient._handle_message", "pong", "persistent"),
    ("wsclient", "WebsocketClient._send_pending_payload", "request", "persistent-request"),
    ("wsclient", "_fetch_snapshot_async", "hello", "snapshot"),
    ("wsclient", "_fetch_snapshot_async", "pong", "snapshot"),
    ("wsclient", "_connect_ready", "hello", "receipt"),
    ("wsclient", "_connect_ready", "pong", "receipt"),
    ("wsclient", "_connect_rpc_ready", "hello", "rpc-identity"),
    ("wsclient", "_read_rpc_frame", "pong", "rpc"),
    ("wsclient", "_one_shot_rpc", "request", "rpc"),
    ("wsclient", "notification_create_once", "notification.create", "notification"),
    ("wsclient", "_catalog_operator_rpc", "hello", "envelope"),
    ("wsclient", "_catalog_operator_rpc", "pong", "envelope"),
    ("wsclient", "_catalog_operator_rpc", "request", "envelope"),
    ("wsclient", "notification_await_once", "notification.await", "notification-await"),
    ("wsclient", "_inbox_once_attempt", "inbox", "inbox"),
    ("wsclient", "send_receipt_once", "send.receipt.get", "receipt"),
    ("wsclient", "rename_once", "rename", "rename"),
    ("wsclient", "close_once", "close", "close"),
    ("wsclient", "reparent_once", "reparent", "reparent"),
    ("wsclient", "assistant_lifecycle_once", "assistant.lifecycle", "lifecycle"),
    ("wsclient", "consent_once", "request", "consent"),
    ("wsclient", "grant_token_once", "grant_token", "grant"),
    ("wsclient", "upload_blob_once", "upload_blob_init", "upload"),
    ("wsclient", "upload_blob_once", "upload_blob_chunk", "upload"),
    ("wsclient", "upload_prompt_blob_once", "upload_prompt_blob_init", "upload-prompt"),
    ("wsclient", "upload_prompt_blob_once", "upload_prompt_blob_chunk", "upload-prompt"),
    ("wsclient", "_fetch_blob_once_attempt", "fetch_blob", "fetch"),
    ("work_report_cli", "get_report", "hello", "reader"),
    ("work_report_cli", "get_report", "work.report.get", "reader"),
]


def inventory(package):
    errors, sends, connects, routes = [], [], [], []
    for path in sorted(package.rglob("*.py")):
        module = ".".join(path.relative_to(package).with_suffix("").parts)
        tree = ast.parse(path.read_text())
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        def qualified(node):
            parts = []
            while node is not None:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    parts.append(node.name)
                node = parents.get(node)
            return ".".join(reversed(parts))
        def fail(node, reason):
            errors.append(f"{module}:{node.lineno}:{qualified(node)}: {reason}")
        def literal(node):
            return node.value if isinstance(node, ast.Constant) else None
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = ast.unparse(node.func).split(".")[-1]
                if isinstance(node.func, ast.Attribute) and name == "send":
                    sends.append((module, qualified(node)))
                    if module != BOUNDARY or qualified(node) not in {"send_frame", "send_uncredentialed"}:
                        fail(node, "direct send outside boundary")
                if isinstance(node.func, ast.Attribute) and name == "connect" and ast.unparse(node.func.value).endswith("websockets"):
                    connects.append((module, qualified(node)))
                    if (module, qualified(node)) not in CONNECTS:
                        fail(node, "unlisted connect")
                if name in {"send_frame", "send_uncredentialed"}:
                    routes.append((module, qualified(node), name))
                    if name == "send_uncredentialed":
                        frame = node.args[1] if len(node.args) > 1 else None
                        kind = next((literal(v) for k, v in zip(frame.keys, frame.values) if literal(k) == "type"), None) if isinstance(frame, ast.Dict) else None
                        if (module, qualified(node), kind) not in UNCREDED:
                            fail(node, "unlisted credential-free frame tuple")
                if module != BOUNDARY:
                    if name in READS:
                        fail(node, "credential loader outside boundary")
                    if isinstance(node.func, ast.Attribute) and name in {"get", "pop", "setdefault"} and ast.unparse(node.func.value) in {"os.environ", "environ"} and node.args and literal(node.args[0]) in ENV:
                        fail(node, "credential environment read outside boundary")
                    for keyword in node.keywords:
                        if keyword.arg in KEYS and name in {"dict", "Frame", "update", "replace", "Config"}:
                            # Config(token='') is a compatibility field, not a frame.
                            if not (module == "config" and name == "Config" and keyword.arg == "token" and literal(keyword.value) == ""):
                                fail(node, "credential keyword outside boundary")
            if module == BOUNDARY:
                continue
            # This path-name literal also catches dynamic comprehensions such
            # as os.environ.get(k) for k in ('AGENT_ORCH_STREAM_TOKEN_FILE',).
            if isinstance(node, ast.Constant) and node.value == "AGENT_ORCH_STREAM_TOKEN_FILE":
                # ssh's static SendEnv name list is routing metadata, without
                # a value lookup. It does not load a credential or file path.
                ancestor = parents.get(node)
                while ancestor is not None and not isinstance(ancestor, ast.Assign):
                    ancestor = parents.get(ancestor)
                names = [ast.unparse(target) for target in ancestor.targets] if ancestor else []
                if module != "cli" or names != ["SSH_SEND_ENV_NAMES"]:
                    fail(node, "credential file-path lookup outside boundary")
            if isinstance(node, ast.Subscript) and literal(node.slice) in KEYS and isinstance(node.ctx, ast.Store):
                fail(node, "credential write outside boundary")
            if isinstance(node, ast.Subscript) and ast.unparse(node.value) in {"os.environ", "environ"} and literal(node.slice) in ENV:
                fail(node, "credential environment read outside boundary")
            if isinstance(node, ast.Attribute) and node.attr == "token" and ast.unparse(node.value) in {"config", "self.config", "cfg"}:
                fail(node, "Config.token read outside boundary")
            if isinstance(node, ast.Dict) and any(literal(k) in KEYS for k in node.keys):
                fail(node, "credential dict outside boundary")
    return errors, sends, connects, routes


def test_package_egress_inventory():
    errors, sends, connects, routes = inventory(PACKAGE)
    print({"direct_sends": sends, "connects": connects, "routes": routes})
    assert not errors, "\n".join(errors)
    assert Counter(sends) == Counter({(BOUNDARY, "send_frame"): 1, (BOUNDARY, "send_uncredentialed"): 1})
    assert Counter(connects) == Counter(CONNECTS)
    assert len(BASE_ROUTING) == len(set(row[:3] for row in BASE_ROUTING)) == 30
    expected = Counter((module, function, "send_uncredentialed" if (module, function, kind) in UNCREDED else "send_frame") for module, function, kind, _ in BASE_ROUTING)
    assert Counter(routes) == expected


@pytest.mark.parametrize("injection,reason", [
    ("await socket.send('{}')", "direct send"),
    ("return os.environ.get('AGENT_ORCH_TOKEN')", "credential environment read"),
    ("frame['stream_token'] = 'injected'", "credential write"),
    ("return websockets.connect('ws://fixture.invalid')", "unlisted connect"),
])
def test_guard_rejects_injected_bypass(tmp_path, injection, reason):
    package = tmp_path / "agent_orch"
    shutil.copytree(PACKAGE, package, ignore=shutil.ignore_patterns("__pycache__"))
    target = package / "wsclient.py"
    original = target.read_text()
    marker = "injected_boundary_bypass"
    target.write_text(original + f"\nasync def {marker}(socket, frame):\n    {injection}\n")
    assert target.read_text() != original
    assert marker in target.read_text()
    errors, *_ = inventory(package)
    assert any(marker in error and reason in error for error in errors), errors


def test_attachment_guard_uses_boundary_path_metadata():
    tree = ast.parse((PACKAGE / "file_delivery.py").read_text())
    guard = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "guarded_path")
    assert any(isinstance(node, ast.Call) and ast.unparse(node.func) == "configured_token_file_paths" for node in ast.walk(guard))
