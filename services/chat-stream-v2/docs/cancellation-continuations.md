# Durable session continuations across request cancellation

This finite audit follows an externally reachable producer through persistence,
its in-memory consumer and the resulting client decision. Line references below
describe base `4311538448b26927fcbafb6423c4c88371387523`; method names survive rebases.
An adjacent Store write and cache update alone do not establish a defect.

The Server cancels ordinary requests during connection teardown
(`server.py:1758–1834`). Its detached send/answer verbs are listed by
`_is_send_frame`; spawn separately owns `_background_spawns` and shields its
admission future (`spawnctl.py:1582–1654`). Store.submit cancellation does not
remove an already queued operation (`store.py:1989`).

| Producer → durable boundary | Consumer → action | Cancellation disposition |
| --- | --- | --- |
| `spawn` → `Sessions.open` → `Store.open_session` (`sessions.py:810`) | `_cache` → hello/list inventory | Request-disconnect immune: the ordinary spawn caller detaches its effect task. A real Store/socket/restart control pauses after this commit and proves one creation and no input. Startup/recurring adoption also calls `open`; cancellation of those daemon tasks is distinct from request disconnect. |
| `rename` → `Store.update_session` (`844`) | `_cache` and immediate emitter → public title | **Defect:** authenticated socket teardown after commit leaves the old title in fresh RPC inventory. Repair queued behind the shared-file handoff. |
| Ordinary `reparent` → `_reparent_locked` → `Store.update_session` (`1041`, including watch custody) | `_cache` and emitter → parentage/roster | **Defect:** ordinary parent-authorized caller lacks the manager caller's protection. Durable parent changes while public inventory retains the old parent. |
| Manager `reparent` → `_manager_reparent_locked` (`server.py:4141`) | Sessions cache plus outcome audit | Protected control: existing `_finish_despite_cancel` encloses the applied outcome while authority/lifecycle fences remain held. |
| `role.set` → `_set_role_locked` → `Store.update_session` and `set_session_role_source` (`1152–1159`) | `_cache` and emitter → role/source projection | **Defect:** cancellation after the first commit loses role-source/cache continuation. Baseline text delivery in the Server is a separate best-effort network action. |
| Detached spawn handoff → `reparent_children` → `_reparent_children_locked` (`1182`) | Per-child `_cache`, count and emitter → successor's children | Request-disconnect immune through the same spawn-owned task as `open`; the helper is not a separately routed verb. A partial daemon shutdown is not claimed atomic across all children. |
| `set_visibility` → `Store.update_session` (`1197`) | `_cache` and emitter → public visibility | **Defect:** Store commit survives teardown while cache retains the previous visibility. The existing graph lock must remain owned through completion. |
| `status_card` → `Store.update_session` (`1209`) | `_cache` and emitter → persisted card in inventory | **Defect:** durable goal exists while fresh RPC returns the old card. Explicit retries are separate requests; setting a goal creates no duplicate update history. |
| `close` / `mark_closed` → `_mark_closed_locked` (`1251`) | Eviction, emitter and cleanup record → closed generation disappears | Protected control: existing finish-then-cancel boundary. This audit does not change process termination or spawn cancellation cleanup. |
| Reconciler or detached resume → `mark_reconciled_dead`, protected-seat branch (`1322`) | `_cache(pane_dead, working=False)` → preserved assistant row with death evidence | No ordinary request owns/cancels this producer. Request-disconnect immunity is narrower than daemon-task cancellation during shutdown. |
| Same producers → `mark_reconciled_dead`, ordinary branch (`1329`) | Eviction, emitter, awaiter resolution → dead row disappears | Same request-disconnect boundary. Its generation CAS and restart reconstruction remain authoritative; no claim of an atomic shutdown-wide projection is made. |
| `restore_reconciled` → `Store.open_session` (`1412`) | `_cache` → restored generation | Outside the externally reachable support boundary: no production call site exists at the audited base (only tests and a comment in recovery). Retained explicitly rather than mislabeled protected. |
| `report` → `Ledger.ingest` → `Store.put_report` (`ledger.py:824`) | `Sessions.apply_report_state` (`860`, `sessions.py:625`) → parent roster state | **Defect repaired:** commit plus generation-matched cache projection finish before caller cancellation propagates. Real socket tests cover done/error/progress, inserted-false replay and restart. Notice delivery and report-triggered close remain cancellable outside this boundary. |
| `assistant.lifecycle` designate/transfer/revoke (`server.py:5931/5939`) | Authoritative Store reads → subsequent lifecycle decisions | Protected control: existing finish-then-cancel Store operation under authority lock; no Sessions cache continuation. |
| `consent.*` → `finish` (`server.py:5959–5976`) | Consent publication → notification projection | Protected control: existing wrapper retains authority lock through the durable operation/publication. |
| `notification.create`, `prompt.ask`, `prompt.cancel` → Notify's serialized NotificationStore executor | DB-backed serialization/list/snapshot → notification/question state | No separate inventory cache. Disconnect can omit a broadcast, but new connections and restart read persisted truth. Notification creation has a real socket/DB/retry/restart control; question generation/expiry rules retain their existing tests. |
| Answer verbs: `notification.resolve`, `notification.resolve_by_dedup`, `prompt.answer`, `voice_answer.answer` | `_after_resolution` → question state, waiters, durable answer queue and broadcast | Connection-disconnect immune via Server's explicit detached set. Existing answer-delivery/restart tests retain the uncertainty and no-repeat boundaries; no blanket shielding is added. |

Server's direct `store.submit` call sites in this cohort (`4410`, `5041`) and
Notify's (`1386`, `2003`) are reads. Their mutation handlers delegate to the
durable owners above. This is not an audit of every Store client or a new
runtime registry.

## Persisted report state

`Store.put_report` transactionally maintains `v2_agent_report_state` for the
current open generation (`store.py:5032`). `_session_row`
(`store_specs.py:62`) hydrates that state during `Sessions.refresh`, including
the timestamp preserved across equivalent terminal states. Progress is an
explicit state transition; report ID replay does not insert or project again.
No new schema, report ordering heuristic or recovery service is needed.

The finite boundary explicitly excludes shutdown-wide task atomicity. Main's
shutdown cancels background tasks before closing the Server listener
(`main.py:1213–1226`). That ordering is an **unqualified risk**, not a proved
lost outcome after restart. A demonstrated durable/projection loss after restart
is the trigger to return that separate scope for qualification; request immunity
does not certify shutdown behavior.

## Executable checks

Use the supported `scripts/gate_env.py run --target v2-pytest` entry, selecting:

- `tests/test_session_inventory_cancellation.py`: five ordinary handlers,
  independently varied local/peer target metadata, real commit barrier,
  authenticated teardown, fresh RPC and inventory push, disk restart,
  pre-admission/post-continuation cancellation and explicit retry controls.
- `tests/test_report_inventory_cancellation.py`: report commit/projection,
  inserted-false replay, done/error/progress restart and cancellable notice work.
- `tests/test_cancellation_continuation_controls.py`: DB-backed notification
  convergence/deduplication and detached spawn admission through the ordinary
  socket caller. Pane transport records actions; no real provider runs.
- Existing `test_close_inventory_consistency.py`, cancellation cases in
  `test_lifecycle_authority.py`/`test_consent.py`, `test_agents_thread_contract.py`
  and `test_notify_durable_answers.py` retain their original support boundaries.

The daemon union and independent final-candidate review remain required before
promotion. A peer-shaped fixture is metadata coverage, not a second-host receipt;
an offline host is NOT TESTED. Installed-runtime proof is a separate release step.
