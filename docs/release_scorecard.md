# Rolling release reliability

Run the installed CLI on the host that can read the release evidence.

```sh
agent-orch reliability score
agent-orch reliability score --as-of 2026-03-18T12:00:00Z --json
agent-orch reliability score --sources /path/to/dated-sources.json --json
```

The default source map is `~/.pentacle/reliability/sources.json`. Set
`PENTACLE_RELIABILITY_SOURCES` to use another map. Keep machine paths,
repository identities, ownership records and retained receipts on the evidence
host. Use absolute paths for live sources and `./` for files kept beside the
map. A historical receipt may pin a `retained` projection by path and SHA256.
It carries the original path, original SHA256 and the fields used for scoring.
The CLI reads files, local Git
objects and GitHub GET resources. Its exit status is 0 when all five conditions
pass, 1 for a measured miss with complete inputs, and 2 when any required input
or classification is incomplete. Both renderings use the same projection.
The command never starts a release, workflow, test, provider or work publisher.

## Reading a score

| Condition | Population and threshold |
| --- | --- |
| First-try releases | At least nine passes among the ten most recent distinct ordinary forward intents, including refused invocations. |
| Hosted false reds | At most one among the latest hundred Public checks workflow IDs ordered by creation time and numeric ID. Every blocking failure in that workflow must have a confirmed non-code cause to count it as a false red. |
| Scheduled nights | Seven passes among the latest seven due scheduled slots. Each slot requires the first scheduled receipt bound to the deployed SHA. |
| Open quarantine | Zero distinct tests pending disposition, with the effective selector and open decision items in agreement. |
| Open failure items | Zero open scorecard-originated causes across the entire work inventory, with every required occurrence accounted for. |

The human view starts with the five results. Each condition includes its reason,
missing inputs, source identities and sample bounds. The observations retain
first outcomes, individual causes and ownership. The release and nightly
conditions also report their original pass counts. To earn full coverage a
receipt must pass front desk, slow consumer, the fleet matrix for every required
host, and `client_delivery.outcome`. The delivery receipt must also record
`passed` for each of `publish`, `receive`, `reconnect` and `recover` under
`client_delivery.assertions`. Historical exclusions remain visible.

## Connecting sources

A source map has five sections and a nonempty `required_hosts` list.
`triage_owner` names the release triage owner for a miss awaiting an item.

- **Hosted.** Set `hosted.repository` to the GitHub owner/repository. The
  live reader uses the existing `gh` read access for `predeploy-tests.yml`,
  first-attempt jobs, check annotations and first-attempt gate artifacts.
  A failing aggregate is traced to the underlying partitions. A separate
  failing step keeps its own cause. An unresolved or nonterminal workflow
  retains its place. For replay, `hosted.snapshot` points to a saved array of
  first-attempt observations and `complete_through` gives its UTC cutoff.
  Each row has numeric `id`, full `sha`, `created`, attempt count `attempt`,
  `first`, `latest`, `source` and `failures`. A failed job entry carries
  `job`, `step`, `runner` labels and `source`. Optional `retained_csv` and
  `retained_runs` bind the existing reviewed first-hundred classification CSV
  and retained workflow inventory. A candidate classification by itself
  cannot prove a false red.
- **Classification evidence.** A package mirror timeout requires the failed
  Ubuntu System packages step, its timeout annotation, and mirror-specific
  evidence or a reviewed decision for that occurrence. A macOS sandbox setup
  failure on a Linux partition uses its bound terminal artifact. JUnit evidence
  preserves each test identity and its assertion digest. An optional
  `hosted.signatures` record matches exact `step`, `runner`, `test` and
  `assertion_digest`, with `kind`, `cause`, `cause_class`, `reviewer`,
  `reason` and source `revision`. For non-code rules set `hosted.checkout`
  and list all affected fixture and implementation `paths` to verify
  unchanged against that revision. A reviewed single-occurrence decision in
  `hosted.decisions` binds `run`, `sha`, `attempt` 1, `job` and `step` to
  its complete `causes` list and review `source`, `reviewer`, `reason` and
  `revision`. A passing retry never supplies that evidence.
- **Releases.** `releases.runtime` names the deployed checkout. Its HEAD
  reflog and latest deployment stamp reveal new activations, including ones
  missing a first invocation. `releases.intents` indexes original invocations
  and their attempt receipts, including pre-activation refusals. Each entry
  has a stable `id`, `created` timestamp, target `sha`, `prior_sha`, `kind`,
  `source` and `attempts`. Use kind `ordinary` for eligible releases.
  `rehearsal`, `same_ref_restart` and `historical_exclusion` require a reason.
  Each attempt has its original number, path and optional content digest.
  Type `stamp` binds the activation transaction. Type `journal` binds the
  coordinated transaction, sealed receipt, prior and target. An archived
  receipt can use `receipt_copy` and `receipt_digest`. Type `refusal` reads
  the first invocation's refusal receipt. For signed-off historical evidence,
  type `review` requires its content digest and a `decision` binding both SHAs,
  reviewer, revision, reason and historical result. A historical pass alone
  leaves full assertion coverage incomplete. Set `complete_through` to the
  latest reconciled invocation census. A newer activation or an unverified
  interval leaves the condition incomplete. Optional `attempt_sources`
  entries specify `root`, a relative `pattern`, and type `refusal` or
  `fd_result`. These read new structured refusals or FD RESULT deployment
  reports even when the runtime HEAD never moved.
- **Nightly.** Set `nightly.root`, `timezone` and `local_time` from the
  scheduler. With `plist` set, the reader checks its label, scheduled time and
  dedicated stdout start records. A start in the scheduled minute must match
  a single run directory and the runtime release identity. For older or delayed
  slots, `bindings` maps the UTC due timestamp to the original `directory`,
  `sha256`, `release_sha` and scheduler evidence `source`. A separate
  `occurrence_index` may supply these bindings. Ad-hoc directories and late
  replay results cannot fill missing slots.
- **Work.** Set `work.work_root` to the lifecycle tree and `work.register`
  to the scorecard cause inventory. A record in `records` names its canonical
  `cause`, `item`, `first_seen` timestamp and stable `occurrences`. A test
  decision also names its exact `test`. IDs use `release/<intent>/1`,
  `public-checks/<workflow>/1` and `nightly-soak/<UTC slot>`. Link occurrences
  with `client_delivery.classification=delivery_failure` to the existing
  client-delivery incident and omit the `test` field. A recorded
  terminal disposition names its `disposition_source`. The reader resolves
  the item and owner in the actual lifecycle, using the published Git revision
  at the cutoff when available. An unavailable item or unmatched required
  occurrence is unresolved. The register's `complete_through` covers all
  scorecard causes, including occurrences older than the rolling samples.
  `selector_checkout` reads the effective blocking selectors. A retained
  selector export uses `selector` pointing to an object with
  `nonblocking_tests`. Absence of a selector cannot establish zero.
- **Diagnostics.** `speed.samples` contains dated local or hosted timings and
  their receipt sources. Hosted first-attempt job exports use the earliest
  start to latest finish. Local and hosted targets are 600 and 720 seconds.
  The work register's `history.events` uses stable IDs, UTC timestamps
  (`at`), and `kind` of `quarantined`, `restored`, `deleted` or `class`.
  Use `test` for selection changes and `class` for cause observations.
  `class_aliases` merges names before computing first sightings.
  `from_inception` and `complete_through` state the verified history coverage.
  Compare the two completed UTC weeks. A positive flat or rising signal calls
  for one owner review in the existing item. The current partial week is
  labeled separately. These diagnostics do not add closure conditions.

## Reconcile work from durable results

`agent-orch reliability reconcile --config /absolute/path/to/writer.json`
reads the same first-attempt evidence and publishes work. It exits 0 when
the selected sources and required writes reconcile, or 2 with pending
occurrences and bounded issue categories. `--surface releases|hosted|nightly`
selects one source. A historical `--as-of` belongs in a disposable namespace
with its own register and work root. Use the score command for a read-only view.

Set `sources` to the scorecard source map and `memory_root` to its work
publisher's repository. The publisher checks current revisions and reads
back attachments. Set `start_at` to the earliest replay boundary still
covered by retained results, `owner` to front desk triage, and `epic` and
`machine` to the goal and host used for new items. `owners` maps a cause to
its component owner. `existing_items` maps known causes to existing item
IDs. The optional `agent_orch` path selects the installed notification CLI.
Set `delivery_item` to the existing proactive-note incident and
`requesting_stream_id` to the attributed lifecycle writer. A bound delivery
failure can reactivate that item after its recorded closure.

Use `scorecard_writer` in the existing quarantine worker configuration to
point to the writer file. Set this option only on the designated publisher host. The daemon
continues replaying after a score closes. The nightly report uses the same reconciler when its local launch
configuration exports `PENTACLE_RELIABILITY_WRITER_CONFIG`. Every attempt
uses its original scheduled slot. A missing slot waits for the receipt
deadline, which defaults to 75 minutes from its due time and can be set
with `nightly_receipt_grace_seconds`. A late or ad-hoc run cannot qualify
as the original scheduled start.

The release source map can name `coordinated_root`, the existing
controller transaction directory for the runtime checkout. The journal
records the first validated forward invocation and its result even when
a later invocation on that transaction succeeds. Ordinary forward
deploys retain their first invocation and outcome when the controller
sets `PENTACLE_RELEASE_ATTEMPT_RECORDS` to a private directory. Add that
directory to `releases.attempt_sources` with `pattern: "*.json"` and type
`ordinary_forward`. Only ordinary forward invocations targeting a different SHA produce
these receipts. The deployment controller keeps control of activation.

The cause register journals unresolved source occurrences before
requesting publisher changes. The same lock serializes it with quarantine
decisions. Stable publisher request IDs and occurrence markers make a
lost reply or concurrent duplicate safe to replay. Publisher failures
leave pending work and send one deduplicated triage alert. A verified
alias in `cause_aliases` names its canonical `cause`, `reviewer`,
`revision` and `evidence`. Superseding an existing provisional item also
requires a publisher-qualified `closure_report` for the superseded item.
The writer attaches prior observations to the canonical item before
moving the duplicate. A test quarantine continues to use its existing
decision and restoration contract. Repeated classes attach one preventive
action to an existing owner. The original reviewed history and coverage
declarations remain inputs to the score.
