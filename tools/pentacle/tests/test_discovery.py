"""Read actual owned listeners, exact arguments and open SQLite descriptors."""
import base64
import hashlib
import json
import os
import plistlib
import socket
import subprocess
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pentacle import discovery as d


class DiscoveryTest(unittest.TestCase):
    def test_macos_environment_stops_before_kernel_auxiliary_strings(self):
        import ctypes
        from types import SimpleNamespace
        raw = (struct.pack("i", 3) + b"/bin/python\0\0/bin/python\0arg with spaces\0\0"
               b"PATH=/bin\0__PYVENV_LAUNCHER__=/install space/.venv/bin/python\0\0"
               b"executable_cdhash=NOT_AN_ENVIRONMENT_VALUE\0ptr_munge=NOT_AN_ENVIRONMENT_VALUE\0")
        def sysctl(mib, count, buffer, size, new, length):
            ctypes.memmove(buffer, raw, len(raw))
            size._obj.value = len(raw)
            return 0
        with patch.object(d.ctypes, "CDLL", return_value=SimpleNamespace(sysctl=sysctl)):
            argv, env = d._mac_args(123)
        self.assertEqual(argv, ["/bin/python", "arg with spaces", ""])
        self.assertEqual(env, {"PATH": "/bin", "__PYVENV_LAUNCHER__": "/install space/.venv/bin/python"})

    def test_installed_daemon_unit_is_checked_even_with_hand_started_process(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            local = home / "install"
            other = home / "other"
            proc = d.Process(123456789, [sys.executable, str(local / "services/chat-stream-v2/main.py")], local)
            with patch.object(Path, "home", return_value=home):
                if sys.platform == "darwin":
                    unit = home / "Library/LaunchAgents/com.pentacle.chat-streamd-v2.plist"
                    unit.parent.mkdir(parents=True)
                    unit.write_bytes(plistlib.dumps({"Label": "com.pentacle.chat-streamd-v2", "WorkingDirectory": str(other),
                        "ProgramArguments": [sys.executable, str(other / "services/chat-stream-v2/main.py")]}))
                else:
                    unit = home / ".config/systemd/user/pentacle-chat-streamd-v2.service"
                    unit.parent.mkdir(parents=True)
                    unit.write_text("WorkingDirectory=" + str(other) + "\nExecStart=" + sys.executable + " " + str(other / "services/chat-stream-v2/main.py") + "\n")
                found = d.unit_for(proc, "daemon")
            self.assertIsNotNone(found)
            self.assertEqual(d.Process(0, found["argv"], Path(found["cwd"])).checkout("daemon"), other.resolve())

    def test_framework_reexec_retains_the_virtual_environment_invocation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            framework = root / "Python.framework/Versions/3.13"
            app = framework / "Resources/Python.app/Contents/MacOS/Python"
            binary = framework / "bin/python3.13"
            for file in (app, binary):
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text("fixture")
                file.chmod(0o700)
            launcher = root / ".venv/bin/python"
            launcher.parent.mkdir(parents=True)
            launcher.symlink_to(binary)
            argv = [str(app), str(root / "main.py"), "--example", ""]
            self.assertEqual(d._mac_invocation(argv, {"__PYVENV_LAUNCHER__": str(launcher)}), [str(launcher), *argv[1:]])
            self.assertEqual(d._mac_invocation(argv, {}), argv)
    def test_real_owned_process_arguments_cwd_and_open_store(self):
        with tempfile.TemporaryDirectory(prefix="install-process-") as folder:
            root = Path(folder)
            script = root / "server with spaces.py"
            db = root / "store with spaces.db"
            script.write_text('''import http.server,json,sqlite3,sys
db=sqlite3.connect(sys.argv[1]);db.execute('create table if not exists example(value text)');db.commit()
class Handler(http.server.BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200);self.end_headers();self.wfile.write(b'{"ok":true,"build_commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","build_id":"0123456789abcdef"}')
 def log_message(self,*args): pass
server=http.server.HTTPServer(('127.0.0.1',0),Handler)
print(server.server_port,flush=True);server.serve_forever()
''')
            child = subprocess.Popen([sys.executable, str(script), str(db), "argument with spaces"], cwd=root,
                                     env={"PATH": os.environ.get("PATH", os.defpath), "HOME": str(root), "FIXTURE_ENV_MARKER": "present"},
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                port = int(child.stdout.readline())
                url = f"http://127.0.0.1:{port}"
                found = d.listener(url)
                self.assertIsNotNone(found, "Owned listener inspection is an environment prerequisite.")
                self.assertEqual(found.pid, child.pid)
                self.assertEqual(found.argv[1:], [str(script), str(db), "argument with spaces"])
                self.assertEqual(found.cwd.resolve(), root.resolve())
                self.assertEqual(found.env["FIXTURE_ENV_MARKER"], "present")
                self.assertTrue(set(found.env) <= {"PATH", "HOME", "FIXTURE_ENV_MARKER", "LC_CTYPE", "__CF_USER_TEXT_ENCODING", "__PYVENV_LAUNCHER__"})
                self.assertEqual(d.open_stores(found), [db.resolve()])
                self.assertEqual(d.health(url)["build_commit"], "a" * 40)
            finally:
                child.terminate()
                child.communicate(timeout=10)

    def test_real_websocket_welcome_validates_upgrade_and_parses_frame(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            errors = []
            def serve():
                try:
                    with server.accept()[0] as client:
                        request = b""
                        while b"\r\n\r\n" not in request:
                            request += client.recv(4096)
                        key = next(line.split(b":", 1)[1].strip() for line in request.split(b"\r\n") if line.lower().startswith(b"sec-websocket-key:"))
                        accept = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
                        payload = json.dumps({"type": "welcome", "runtime_sha": "b" * 40}).encode()
                        client.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n" + bytes([0x81, len(payload)]) + payload)
                except Exception as error:
                    errors.append(type(error).__name__)
            thread = threading.Thread(target=serve)
            thread.start()
            value = d.welcome(f"ws://127.0.0.1:{server.getsockname()[1]}/?PRIVATE_QUERY")
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertFalse(errors)
            self.assertEqual(value, {"type": "welcome", "runtime_sha": "b" * 40})


if __name__ == "__main__":
    unittest.main()
