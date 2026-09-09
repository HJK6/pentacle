---
id: meta_readme
title: Set up the full process
type: meta
status: stable
canonical: true
created_at: '2026-09-09'
updated_at: '2026-09-09'
source_path: README.md
tags:
- pentacle
- process
summary: Set up the full process.
related: []
---

# Set up the full process

Use the bundled workspace for public project work, or copy this `process/` directory to a private location before creating real work items. The templates, schema and tools travel together. No existing team memory, private Git remote or fleet synchronization service is required.

## Install the workspace tools

Use Python 3.11 or later. From your copy of this directory, create a virtual environment and install the declared dependencies:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/generate_catalog.py
.venv/bin/python scripts/validate_memory_v2.py
```

The creation, validation and catalog tools use the workspace containing their `scripts/` directory. Keep `schema/`, `templates/` and `work/statuses.json` alongside it. Generated catalogs contain your workspace metadata and must receive the same privacy treatment as the work records.

## Create a spec

```sh
.venv/bin/python scripts/new_work_item.py example-app example_change \
  --title 'Example change' --summary 'Describe the intended observable behavior.' \
  --status analysis --machine local --owner maintainer
.venv/bin/python scripts/generate_catalog.py
.venv/bin/python scripts/validate_memory_v2.py
.venv/bin/python scripts/search_memory_v2.py 'Example change'
```

Open `work/analysis/example-app__example_change/spec.md`. Fill in the goal, current and target behavior, constraints, acceptance criteria and validation plan. The generated item is a scaffold, not an approved specification. Give an independent reviewer the spec and [QA guidelines](docs/config/qa_guidelines.md); resolve actionable findings before locking the plan and implementing.

Use the [development lifecycle](docs/config/development_process.md) to progress the item. When moving an item to a different status directory, update `status`, `source_path` and `updated_at` in both Markdown files, plus status-bearing body text and the current next action. Keep the stable IDs unchanged. Regenerate and validate the catalogs after changes. At terminal closure also record `completed_at`, the disposition, completed or explicitly waived acceptance criteria and a short retrospective.

## Give agents the process

Use the repository's root AGENTS.md as the common entry point. If this workspace lives separately, add a short local instruction telling your agent where it is; do not put a private absolute path into a public AGENTS.md. Load the appropriate file from `agents/` for a lead, QA reviewer, documentation agent or coordinator. One lead with an independent reviewer is enough for a single lane.

Follow [agent orchestration](docs/config/agent_orchestration.md) for ownership, reports, async questions and shared resources. Choose your own provider and model settings. The process can be followed without a running Pentacle daemon; those commands become relevant when using Pentacle's orchestration features.

## Optional Pentacle integration

The bundled workspace tools need only Python and the packages in requirements.txt. The following integration also requires a separately installed Pentacle daemon, agent-orch CLI and their public parser/configuration dependencies; this process directory does not provide those runtimes. Confirm the installed versions support these settings before configuring them.

For the daemon's spec catalog, set `PENTACLE_MEMORY_ROOT` to the absolute workspace path in the environment used to launch the daemon. For agent-orch memory and role-baseline discovery, set `AGENT_ORCH_MEMORY_REPO` to the same path in the CLI environment. Configure both explicitly; a variable in an interactive shell does not retroactively reach an already-running service.

The workspace's `catalog/documents.json` indexes active work and `catalog/archive.json` indexes terminal work. The spec parser and role baselines must come from the public installation. Validate a synthetic item first, then confirm the installed daemon and CLI resolve the intended workspace before using real work records. Follow your deployment's authorized restart and readback procedure when changing an existing service.

See [private configuration](docs/config/private_configuration.md) for external fleet settings, secrets and migration. Keep real receipts and private specifications outside the public distribution. The application startup, package and runtime-integration checks must also pass for a release; passing these workspace tools alone does not establish that the application is installed or active.
