---
id: meta_memory
title: Memory discovery and authoring
type: meta
status: stable
canonical: true
created_at: '2026-09-28'
updated_at: '2026-09-28'
source_path: MEMORY.md
tags: [memory, discovery, authoring]
summary: Discover and author private facts, preferences, decisions and work using this portable kit.
related: [meta_readme, config_private_configuration, config_development_process]
---

# Memory discovery and authoring

Start here in your private copy. Use [setup](README.md) for installation, [development process](docs/config/development_process.md) for specs and [private configuration](docs/config/private_configuration.md) for local configuration. Relative memory paths resolve from this workspace root, independently of a project's checkout.

Search `catalog/documents.json` for active documents; terminal work lives in `catalog/archive.json`. `scripts/search_memory_v2.py` searches the active catalog by default; `--archive` includes terminal work. Open the source named by `source_path` and check its current metadata/body. The working source wins when generated catalogs lag. Keep one authoritative source for each fact and link it from related documents rather than copying it.

## Identity and privacy

The assistant's chosen name is its identity. A physical execution host runs its processes; a provider/model supplies inference; a task role describes responsibility. These are distinct. Keep a local-only `soul.md` identifying your host, private paths and available capabilities, and point your provider bootstrap at this workspace and that soul. No public bootstrap should embed a real person's contact/profile records, host inventory or credentials. Secrets stay in an external secure store; memory may record a safe reference, never the value. Catalogs inherit the privacy of their source documents.

## Author, catalog, validate, search

Copy [the generic document template](templates/document.md) to `docs/personal/example_note.md` in your private kit. Make the ID unique, set `source_path` relative to this workspace, title/summary/date/status and related IDs. Use `type: personal` for a durable personal fact or preference, `decision` for a choice with rationale and revisit trigger, and `note` for provisional observations. State whether a preference is explicit or inferred and cite its source/date. Record uncertainty; do not turn a task transcript into a permanent fact. Do not use contact metadata without the schema's required identity/backlinks.

```sh
mkdir -p docs/personal
cp templates/document.md docs/personal/example_note.md
# Edit metadata and body before indexing.
.venv/bin/python scripts/generate_catalog.py
.venv/bin/python scripts/validate_memory_v2.py
.venv/bin/python scripts/validate_memory_v2.py --source-only
.venv/bin/python scripts/search_memory_v2.py 'Example note'
```

Full validation checks source plus generated catalogs; source-only still checks schema and cross-document integrity, so it cannot replace catalog validation. In a shared workspace one publisher generates catalogs and records accepted history. Preserve stable IDs across moves, update `source_path` and dates, and keep `related` references valid. Work items use the unchanged statuses in `work/statuses.json`; use the creation tool rather than inventing a new lifecycle.

Raw evidence stays outside shared/public memory. A small `_artifacts/evidence-pointer.json` may record `location`, `sha256`, `candidate`, `scope`, `captured_at` and limitations. Verify the digest when consuming it; a pointer is not the evidence. Keep actual private paths in the private kit only. Archive or supersede obsolete facts with status and successor metadata; do not silently replace historical decisions.
