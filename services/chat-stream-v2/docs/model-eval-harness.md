# Model evaluation harness

`tools/model_eval/` compares two headless coding models on a fixed list of
retained tasks. It is a one-off measurement tool: it changes no routing, model
catalog, default or pricing, and the task list, briefs and raw run bundles are
data that stay outside the repository.

## Flow

1. **Freeze.** A `tasks.json` lists the tasks (class `qa`, `fix` or `typed`),
   each with a repository, a start commit, a brief, a known outcome and, for
   fix tasks, the scoring command plus RED/GREEN receipts. `model_eval.cli
   freeze tasks.json` prints its SHA-256; nothing changes after the first
   scored run.
2. **Fix-task eligibility.** `cli eligibility` restores the fix commit's test
   files onto the pre-fix commit and runs the scoring command: it must fail
   with a product/assertion failure (dependency, import, collection and setup
   errors are rejected, as is an already-green baseline); the same command at
   the fix commit must pass.
3. **Run.** `cli run` runs every (task, model) cell once, sequentially under
   `nice`, in a fresh detached git worktree with the process cwd set to it. The
   argv binds model, effort and cwd explicitly (`runner.build_argv`). A shim
   named like the orchestration CLI is first on `PATH`; it appends every call to
   a JSONL log and validates `report --result` against the report payload schema.
4. **Score.** `correct` (QA verdict equals the known verdict; fix: the restored
   test passes; typed: the checker passes), `protocol` (START and END sent and a
   schema-valid report filed, QA with a verdict) and cost/time/tokens. A timeout,
   crash or run with no valid report is `failed`: it stays in every denominator
   with zero scores and its observed usage; an unavailable cost is recorded
   `unavailable`, never 0.
5. **Grade.** `cli grade` makes one blind grader call per task: model and
   provider strings are scrubbed, the two outputs are shown in a deterministic
   random order, and the grade is 0-2 against the known outcome.
6. **Recommend.** `cli report` applies `scoring.recommend`: a class is
   *challenger-ready* when its `correct` total and `protocol=yes` count are at
   least the incumbent's and its grade total is at least the incumbent's minus
   one. All classes ready is `swap`, some is `partial swap: <classes>`, none is
   `keep Luna`. A class with fewer than two tasks, or where either model
   finished fewer than two runs, is under-sampled and cannot be ready.

## Known limitations

Headless sessions differ from live seats (no incoming messages or peer
context). Both models can read the whole filesystem, including the accepted fix
and retained verdicts; Haiku's shell is not sandboxed while Luna's is, and the
grader is a Claude model grading a Claude model blind. Counts are small; the
output is counts, not a significance claim.

Tests: `pytest services/chat-stream-v2/tools/model_eval/tests`.
