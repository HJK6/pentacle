# Upgrade an installed Pentacle

Your agent follows this checklist from the top directory of the install. The
supported shell is Bash. Run each fenced block as written in its own Bash
process. The recorded plan carries state between steps; leave it in place until
the upgrade is verified. `pentacle update` alone changes nothing.

Use `bin/pentacle version` for installed and running identities and
`bin/pentacle update --check` for release notes, required actions and whether
your starting commit has been tested directly. An empty release manifest means
there is no stable release yet. Cached information is labelled and never proves
that an install is up to date. A custom release remote is labelled custom.

For non-default settings, export `PENTACLE_CONFIG`, `PENTACLE_DAEMON_URL` and
`PENTACLE_WEB_URL` in the environment of blocks 1 and 2. Forks and rehearsals also
set `PENTACLE_RELEASE_REMOTE`. Later blocks recover those values from the plan.
Set `PENTACLE_KEEP_ENV` to comma-separated extra environment variable names
needed by the existing processes. Launchers carry only those named values,
`PATH`, `HOME` and `PENTACLE_*`; the files are private. The helper modes neither
stop/start services nor check out, install or build code. Preparation records
open SQLite stores; backup verifies copies only after both processes stop.

1. Find the release and get the helper:

   ```bash
   set -eu
   test -f services/chat-stream-v2/main.py
   remote="${PENTACLE_RELEASE_REMOTE:-https://github.com/HJK6/pentacle.git}"
   git fetch "$remote" main
   rm -rf .pentacle-deploy/upgrade-helper
   mkdir -p .pentacle-deploy/upgrade-helper
   git show FETCH_HEAD:releases/manifest.json > .pentacle-deploy/upgrade-helper/manifest.json
   stable=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["releases"][-1]["commit"])' .pentacle-deploy/upgrade-helper/manifest.json)
   git archive --format=tar -o .pentacle-deploy/upgrade-helper/helper.tar "$stable" bin tools/pentacle
   tar -xf .pentacle-deploy/upgrade-helper/helper.tar -C .pentacle-deploy/upgrade-helper
   printf '%s\n' "$stable" > .pentacle-deploy/upgrade-helper/COMMIT
   ```

2. Prepare, with both processes still running. Exit status 2 means it refused and nothing was changed; when it lists required actions, do them and run the second form:

   ```bash
   set -eu
   test -f services/chat-stream-v2/main.py
   python3 .pentacle-deploy/upgrade-helper/bin/pentacle update --prepare --install "$PWD"
   ```

   ```bash
   set -eu
   test -f services/chat-stream-v2/main.py
   python3 .pentacle-deploy/upgrade-helper/bin/pentacle update --prepare --ack-required-actions --install "$PWD"
   ```

3. Stop the web host, then the daemon, the way they were started. Agent sessions keep running in tmux.

4. Copy the databases:

   ```bash
   set -eu
   test -f services/chat-stream-v2/main.py
   python3 .pentacle-deploy/upgrade-helper/bin/pentacle update --backup --install "$PWD"
   ```

5. Switch the code. This stops unless the copies are complete:

   ```bash
   set -eu
   test -f services/chat-stream-v2/main.py
   plan_dir=$(cat .pentacle-deploy/upgrade-current)
   . "$plan_dir/plan.env"
   test -f "$plan_dir/backup-complete"
   git checkout --detach "$target"
   ```

6. Install:

   ```bash
   set -eu
   test -f services/chat-stream-v2/main.py
   plan_dir=$(cat .pentacle-deploy/upgrade-current)
   . "$plan_dir/plan.env"
   npm ci
   "$py" -m pip install -r services/chat-stream-v2/requirements.txt -e services/agent-orch
   ```

7. Build:

   ```bash
   set -eu
   test -f services/chat-stream-v2/main.py
   npm run build:web
   ```

8. Start the web host with `start-web.sh` and then the daemon with `start-daemon.sh` from the plan directory, each kept running the way that process was kept running before (the same terminal, tmux window or supervisor).

9. Verify. It must exit 0; if it does not, send us `bin/pentacle doctor --json`:

   ```bash
   set -eu
   test -f services/chat-stream-v2/main.py
   plan_dir=$(cat .pentacle-deploy/upgrade-current)
   . "$plan_dir/plan.env"
   bin/pentacle doctor --live
   ```

Going back is its own block, not a repeat of the steps above. Stop the web host and then the daemon, run the block, start both with the same two launchers, and finish with block 9 when that commit has a doctor, otherwise with a short message in a new session that gets a reply. It selects no release, applies no ancestry check, and leaves the plan directory and its copies in place:

   ```bash
   set -eu
   test -f services/chat-stream-v2/main.py
   plan_dir=$(cat .pentacle-deploy/upgrade-current)
   . "$plan_dir/plan.env"
   git checkout --detach "$previous"
   npm ci
   "$py" -m pip install -r services/chat-stream-v2/requirements.txt -e services/agent-orch
   npm run build:web
   ```

Because both processes are stopped before the checkout changes and started together afterwards, an older web host never runs against a newer daemon.

## If the previous version cannot start

Only when the previous version will not start on the current databases, stop
both processes and explicitly run `restore-stores.sh` from the plan directory.
It replaces every recorded database with its first verified copy and removes
its WAL and shared-memory sidecars. **Everything stored since those copies is
lost.** No upgrade or rollback step restores databases automatically. Start
both processes with the saved launchers and verify a new reply afterwards.

## Doctor output

Run `bin/pentacle doctor` for read-only checks. Add `--live` to create one short
provider conversation and close it afterwards. The groups check prerequisites,
source identity, dependencies, daemon, web build, private configuration,
provider executables and workspace paths. A failed prerequisite skips dependent
checks and names it in `blocked_by`. A hand-started daemon needs no service unit.

Exit codes: 0 means no failed check, 1 means a check failed, 2 means the command
could not run or a checklist helper refused. Unexpected internal errors exit
70. Follow each finding's fix, then run the doctor again. Send
`bin/pentacle doctor --json` when requesting help; its fixed findings omit
credentials, environment values, URLs and personal names. Keep raw config,
tokens, plan files, launchers and provider transcripts private.
