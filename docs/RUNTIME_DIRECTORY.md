# Desktop runtime marker directory (TH-H3)

PENTACLE_RUNTIME_DIR selects only the directory containing desktop-runtime.json. Unset or empty preserves exactly path.join(os.homedir(), '.pentacle'); a relative override remains relative to the process working directory. Other state, token and cache paths are unchanged.

main/runtime_paths.js resolves at use time so an in-process host can set isolation after importing the client. The handshake writer preserves recursive mkdir and the exact JSON.stringify({ sha, pid, connected_at }) + newline bytes. Override write failures identify PENTACLE_RUNTIME_DIR, the target, and the original OS code/cause. There is no fallback to the real home. Default-path errors retain their original behavior.

At base c56afde5eb4c49f6f021f9e67f5691221999a221, tracked source has one production writer and no production reader. The runner's synthetic marker fixture uses the shared path resolver for writing and reading; the parent test's hard-coded historic-path sentinel is deliberately retained as an independent oracle. No unused production reader was invented.

## Test isolation

The root runner pins PENTACLE_RUNTIME_DIR to its owned temporary home's .pentacle directory, so an inherited operator override cannot escape HOME isolation. The existing home-isolation assertions remain, with additional inherited-override sentinel assertions.

The web gate owns a separate temporary runtime directory across its initial in-process host and every replacement host. Replacement processes receive the same explicit env value. It restores the exact prior env state after cleanup and removes the runtime directory even with --keep; other debug evidence keeps its existing retention behavior. Setup/scenario errors also clean up. A removal error marks the gate verdict FAIL and yields nonzero. Host teardown uses the existing bounded owned-process TERM/KILL verifier.

A surviving host remains a failed-cleanup condition. Removing the runtime directory cannot prevent such a survivor from recreating it later; do not interpret a failed cleanup as proof that all files/processes are gone.

## Evidence and gates

New synthetic tests execute the real handshake and gate source through injected filesystems, fake server/process/network/CDP boundaries. No Chrome, fleet host or daemon is started. The gate wiring cases cover initial host, restart, success, setup failure, scenario failure, cleanup failure, and --keep. Writer tests pin byte output, late override resolution, EACCES on mkdir/write, and deterministic disposable file-as-directory failure. Inline synthetic fixture hashes are listed in test/fixtures/runtime_paths/MANIFEST.json.

The baseline writer from c56afde5 was replayed through the same injected test harness: 8 passed, 4 failed, proving it ignored the override and lacked the requested diagnostics. Candidate focused tests pass. Final-head install, root-subset and syntax receipts are in the PR.

Dot does not run the full Chrome/daemon web gate. Fleet owns that exact-head E2E receipt. The root local gate explicitly leaves three unchanged files unrun: web_server.test.js (default tmux/unowned loopback), web_auth.test.js (unowned loopback), and pre_push_hook.test.js (real ps fallback). They remain in default discovery; no skip or assertion weakening was introduced.
