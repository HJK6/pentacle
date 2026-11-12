# One release path

Releases use the ordinary deploy path or the sealed coordinated path when helper
identity changes. The mandatory merge gate, saved gate-evidence validation and
boot/runtime identity checks remain required. Selective qualification, its
registry and the `--release-inputs` activation option are retired.

After activation is committed, both paths run the same full-fleet smoke,
slow-consumer log and Front Desk checks. Failures produce a BLOCKER while leaving
the newly serving release running; rollback remains explicit. The nightly job
uses these checks against the deployed checkout in its existing window. It does
not deploy, restart, or create another admission path.

`deploy/release_selection.py` now retains only the exact digest/file/manifest
validation helpers consumed by satellite pinning. These helpers do not provide
selective admission or certify installed fleet behavior. Satellite pinning and
its independent tests are unchanged.

Local synthetic tests and repository CI are not production deployment proof.
Release authority, installed readbacks and operational windows remain with the
release owner. See [public release](public_release.md) and the
[daemon deployment guide](../services/chat-stream-v2/deploy/daemon/README.md).
