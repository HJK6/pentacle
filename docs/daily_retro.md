# Daily retrospective review

The launchd producer runs at 05:00 America/Chicago. It freezes eligible terminal
retros, commissions one Codex GPT-6 Sol/medium draft and one GPT-6 Astra/high
finalization, then sends a retained REPORT notice to the current assistant binding.
assistant reviews immediately, records dispositions, and uses normal work records for
decisions and observed outcomes. Quiet days produce no operator chat message.
Historical backfill is a separate bounded, explicitly approved scope.

The producer reuses `tools/live_window` operator authentication, as the scheduled
fleet-smoke tool does. Its allowlist permits only owned spawn/await/close and
current-binding reads plus exact retained REPORT send/receipt operations. Both
workers self-close on terminal reports; cleanup passes their recorded generations.
Questions and work review run only as the open current assistant generation with
seat credentials. The bound assistant may be hidden behind its operator composite.
The daemon admits its ordinary durable `prompt.ask` only when the authenticated
stream, session generation and current binding all agree. Other hidden seats
still ask their parent; this path grants no consent or execution authority.

Local JSON configuration (absolute paths; keep credentials outside source):

```json
{
  "timezone": "America/Chicago",
  "memory_root": "/absolute/memory",
  "state_root": "/absolute/local-state/daily-retro",
  "ws_url": "ws://127.0.0.1:7791",
  "token_path": "/absolute/private/operator-token",
  "host": "configured-host"
}
```

`state_root` must be outside memory. Preserve it across upgrades and rollback.
Collection manifests commit before their rebuildable enrollment index. Older
first-run history is explicitly **not reviewed**; unreadable sources and whole
originals exceeding 40 sources/64 KiB stay explicit pending coverage. Daily retry
reuses immutable inputs, worker/report identities and frozen delivery keys.

From the installed release, using its pinned interpreter:

```sh
python services/chat-stream-v2/tools/daily_retro.py run --config /absolute/config.json
python services/chat-stream-v2/tools/daily_retro.py run --config /absolute/config.json --on-demand
```

RunAtLoad catches missed executions after 05:00. Before 05:00 a timer invocation
does nothing; authorized activation uses `--on-demand`. Install only when
`/etc/localtime` resolves America/Chicago. Render the plist's interpreter, release,
config and log paths as absolute values; lint and read back its 05:00 calendar.
Use a durable service interpreter or the release's own virtual environment;
record its resolved path, executable hash and dependency versions. Scratch output
or temporary virtual environments must not appear in the production plist.

assistant reads the retained `astra.json`, verifies evidence and chooses one disposition
per candidate. A review result file contains `packet_hash` and `dispositions`, each
with `id`, `disposition` and `reason`. Allowed dispositions: resolved, duplicate,
no_change, investigate, authorized, propose, defer. Action/investigation/defer also
requires `work_id`, `proposal_id` and `version`, matching a normal work proposal.
Create or reuse that item with the existing triage process before recording review.

```sh
python services/chat-stream-v2/tools/daily_retro.py record-review --config C --run-id YYYY-MM-DD --result REVIEW_JSON_FILE
python services/chat-stream-v2/tools/daily_retro.py decision --config C --work-id spec_existing_item --proposal PROPOSAL_JSON_FILE
```

The proposal file contains stable `id`, exact `scope`, `citations`, concise `title`
and `body`, options (`label`/`value`/optional `description`), accepting `owner`,
`checkpoint` and `success_measure`. A non-question disposition uses `disposition`.
Include the authority reference when work is already authorized. The helper stores
the version hash, question attempts and answer provenance inside that work spec.
Approval remains authorization until an observed success or honest blocker is
recorded there through the normal delivery process.

Before asking, assistant reconciles all prior questions. Existing live questions remain
live across a hot rebind. After expiry, one current-generation question replaces
the old question. Answered old-generation rows are recovered; stale-version answers
never authorize changed scope. Unknown status or inability to retire an old live
question blocks a new ask. Intent commits before ask, making response-loss retries
idempotent. Silence never grants authority. Re-run `decision` for each still-open
normal work proposal after assistant replacement, using the stored proposal fields.
Before that daemon admission is deployed, a refused hidden ask retains the entire
proposal/version as `ask_blocked` and sends one durable REPORT to the bound assistant.
Repeated calls reconcile that REPORT without retrying the question. Once the daemon
is live, explicitly resume with `decision --retry-blocked`; silence never authorizes work.

Focused isolated validation:

```sh
python -m pytest services/chat-stream-v2/tests/test_daily_retro.py services/chat-stream-v2/tests/smoke/test_daily_retro_socket.py -q -rs
python -m pytest services/agent-orch/tests/test_close_generation.py services/agent-orch/tests/test_close_cli.py -q -rs
```

`daily_retro_surface` runs a real socket server and stores on a temporary port,
with separate credentials/tree/state and `fixture-chat:assistant`. Only provider/
tmux is a counterpart. Synthetic questions never touch the production daemon or
the production assistant surface. The fixture proves answer/version recovery, quiet delivery,
authorization allowlist, replacement preservation and observed fixture outcomes.

The separately gated real-worker rehearsal uses six source fixtures in
`tests/fixtures/daily_retro_sources.json`, with fixture files in its `files` object
and a normal active `spec_fixture_existing_owner` work record. Date each terminal
spec in the previous local day. Run a `daily_retro_surface` counterpart configured
to record the received packet review, supply TEST_C with `isolated:true` and its
explicit non-7791 endpoint/credentials, and WORKER_C with existing worker transport:

```sh
python services/chat-stream-v2/tools/daily_retro.py rehearse --config TEST_C --workers-config WORKER_C --evidence-dir E
```

This reads originals through real Sol/Astra reports. A declared fixture mutation
omits the serious shortlist entry and inserts a small evidence gap in the repeated
case; the original report and every disposition remain retained. Astra must recover
the serious insight, group the repeated issue, correct the gap and retain substantial
uncertainty. Delivery and assistant helper review stay on the isolated Codex/provider
counterpart. It is distinct from real production first-run delivery and review.

Release staging requires a fresh immutable SHA directory:

```bash
set -euo pipefail
test ! -e "$RETRO_RELEASE"
test ! -L "$RETRO_RELEASE"
install -d -m 0755 "$RETRO_RELEASE"
git archive "$RETRO_CANDIDATE" \
  services/chat-stream-v2/tools/daily_retro.py \
  services/chat-stream-v2/tools/live_window services/_shared/operator_auth.py \
  services/agent-orch/agent_orch \
  services/chat-stream-v2/deploy/com.pentacle.daily-retro.plist \
  docs/daily_retro.md | tar -x -C "$RETRO_RELEASE"
```

Hash extracted bytes, pin interpreter/dependencies, save previous label/plist/config
identities, and obtain the runtime-window owner's GATE before landing or activation.
Use one batched window: owned test label bootstrap/print/kickstart/bootout, isolated
installed rehearsal, initial real on-demand run and assistant review, then production
label bootstrap/calendar readback. Retain separate delivery/decision-ready latency
and cleanup receipts.
Do not restart the daemon. Reject moved preimages.

Rollback bootouts only `com.pentacle.daily-retro`, restores the exact previous
plist/config/release pointer, and bootstraps the old label only if it existed.
Otherwise verify it absent. Keep manifests/decisions intact. Public rollback is
a reviewed revert through ordinary gates, never a force push.
