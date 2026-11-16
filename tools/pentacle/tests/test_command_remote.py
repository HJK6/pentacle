"""Run public commands behind a recording Git executable, without network."""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[3]
OFFICIAL = "https://github.com/HJK6/pentacle.git"


class RemoteCommandTest(unittest.TestCase):
    def test_commands_contact_only_the_official_url_and_never_forward_git_output(self):
        real_git = shutil.which("git")
        with tempfile.TemporaryDirectory(prefix="remote-command-") as folder:
            root = Path(folder)
            checkout, home, tools = root / "checkout", root / "reader", root / "tools"
            for path in (checkout, home, tools):
                path.mkdir()
            def git(*args):
                return subprocess.run([real_git, "-C", str(checkout), *args], check=True, capture_output=True, text=True)
            git("init", "-q")
            git("config", "user.name", "Example Agent")
            git("config", "user.email", "agent@example.invalid")
            git("remote", "add", "origin", "https://user:ORIGIN_CREDENTIAL_MARKER@example.invalid/repo.git")
            (checkout / "README.md").write_text("Fixture\n")
            git("add", "README.md")
            git("commit", "-qm", "Fixture")
            recorded = root / "git.jsonl"
            wrapper = tools / "git"
            wrapper.write_text("#!" + sys.executable + "\n" + f'''import json,os,sys
with open({str(recorded)!r},'a') as output: output.write(json.dumps(sys.argv[1:])+'\\n')
args=sys.argv[1:]
verb=args[2] if args[:1]==['-C'] else args[0]
if verb=='fetch':
 sys.stderr.write('GIT_STDERR_SECRET_MARKER');sys.exit(1)
os.execv({real_git!r},[{real_git!r},*args])
''')
            wrapper.chmod(0o700)
            env = {**os.environ, "HOME": str(home), "PATH": str(tools) + os.pathsep + os.environ.get("PATH", os.defpath)}
            # Owned rejectors answer immediately without touching another service.
            with socket.socket() as daemon, socket.socket() as web:
                daemon.bind(("127.0.0.1", 0)); web.bind(("127.0.0.1", 0))
                stopped = threading.Event()
                def reject(server):
                    server.listen()
                    server.settimeout(0.1)
                    while not stopped.is_set():
                        try:
                            connection, _ = server.accept()
                            connection.close()
                        except socket.timeout:
                            continue
                threads = [threading.Thread(target=reject, args=(server,)) for server in (daemon, web)]
                for thread in threads:
                    thread.start()
                common = ["--install", str(checkout), "--config", str(home / "absent.js"), "--json",
                          "--daemon-url", f"ws://127.0.0.1:{daemon.getsockname()[1]}/?QUERY_SECRET_MARKER",
                          "--web-url", f"http://127.0.0.1:{web.getsockname()[1]}"]
                try:
                    for command, extra in [("version", []), ("doctor", []), ("version", ["--release-remote", OFFICIAL])]:
                        result = subprocess.run([sys.executable, str(SOURCE / "bin/pentacle"), command, *common, *extra],
                                                env=env, capture_output=True, text=True, timeout=8)
                        self.assertIn(result.returncode, [0, 1])
                        value = json.loads(result.stdout)
                        identity = value.get("identity", value)
                        self.assertEqual(identity["install"]["release_remote"], "official")
                        for marker in ("ORIGIN_CREDENTIAL_MARKER", "GIT_STDERR_SECRET_MARKER", "QUERY_SECRET_MARKER"):
                            self.assertNotIn(marker, result.stdout + result.stderr)
                finally:
                    stopped.set()
                    for thread in threads:
                        thread.join(timeout=1)
            calls = [json.loads(line) for line in recorded.read_text().splitlines()]
            fetches = [args[2:] for args in calls if args[:1] == ["-C"] and args[2] == "fetch"]
            self.assertEqual(fetches, [["fetch", OFFICIAL, "main"]] * 3)


if __name__ == "__main__":
    unittest.main()
