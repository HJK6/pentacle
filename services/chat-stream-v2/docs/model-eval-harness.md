# Model evaluation harness

`tools/model_eval/` compares two headless coding models on a fixed list of
retained tasks. It is a one-off measurement tool: it changes no routing, model
catalog, default or pricing, and the task list, briefs and raw run bundles are
data that stay outside the repository.

## Prerequisites and invocation

Git, Python 3.13 with `pytest` for the self-tests, and the two model CLIs
installed and logged in (`codex` and `claude`; the grader uses `claude`). Task
checkers need whatever their commands call. Every command below runs from
`services/chat-stream-v2/tools` of a checkout and has the form
`python3 -m model_eval.cli <command> ...`. `<bundle>` is a directory you create
for outputs and **must be an absolute path** (the model runs with its cwd set to a worktree, so a
relative bundle path would resolve under that worktree); task `repo` paths should be absolute too.
`<tasks>` is the frozen task file. Example: `python3 -m model_eval.cli run --tasks /abs/tasks.json
--bundle /abs/bundle`.

| Command | Required flags | Writes |
|---|---|---|
| `freeze <tasks>` | the file | prints its SHA-256 |
| `eligibility` | `--repo --pre-fix --fix --cmd --red-pattern --test-files F...` (optional `--setup-cmd`, `--out DIR`) | `--out DIR/{red,green}.txt` and a JSON receipt on stdout; exit 0 only if eligible |
| `run` | `--tasks <tasks> --bundle <bundle>` (optional repeatable `--task ID`, `--model luna|haiku`, `--timeout S`) | `<bundle>/runs.jsonl` (one scored row per cell, appended; on a re-run every cell that already has a row, failed ones included, is skipped, so only an interrupted cell with no row is retried) and `<bundle>/runs/<task>/<model>/` (the stand-in CLI call log `agent_orch.jsonl`, `stdout.jsonl`, `stderr.txt`, `last_message.txt`, `final.diff`, `record.json`; the brief is not stored, it stays inline in the task file) |
| `grade` | `--tasks <tasks> --bundle <bundle>` | `<bundle>/grades.json` |
| `report` | `--bundle <bundle>` | recommendation JSON on stdout |

## The task file

`tasks.json` is a JSON object with a top-level `tasks` array, plus optional
`pricing` (per-million-token USD rates `uncached_input`, `cache_read`, `output`
used to price the Codex run) and `grader_prompt` (defaults to the built-in
prompt). Each task has `id`, `class` (`qa`, `fix` or `typed`), `repo` (a local
git clone), `sha` (the commit the fresh worktree starts at), `brief` (the full
prompt text, inline) and `known` (the known outcome shown to the grader), and
optionally `setup_cmd` (run once in the worktree after creation). Per class:

- `qa`: `known.verdict` is `accept` or `reject`.
- `fix`: `fix_sha`, `test_files` (restored from the fix commit before scoring),
  `score_cmd`, and for eligibility `red_pattern`.
- `typed`: `check_cmd`, run in the worktree after the model finishes; exit 0 passes.

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
5. **Grade.** `cli grade` makes one blind grader call per not-yet-graded task
   that has at least one finished output: model and provider strings are
   scrubbed, the outputs are shown in a deterministic random order, and each
   gets a grade 0-2 against the known outcome plus a followed-scope flag. A
   failed run has no output and is graded 0 without a call; a task whose cells
   all failed makes no call. A reply with a missing or invalid grade raises and
   persists nothing.
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
