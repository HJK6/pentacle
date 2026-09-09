---
id: config_private_configuration
title: Private configuration
type: config
status: stable
canonical: true
created_at: '2026-09-09'
updated_at: '2026-09-09'
source_path: docs/config/private_configuration.md
tags:
- pentacle
- process
summary: Private configuration.
related: []
---

# Private configuration

The public source and reusable process templates are separate from your private deployment. Keep real work records, machine identities, endpoints, provider credentials, signing material and runtime databases outside the public source and distribution packages.

## External fleet overlay

Use a private per-user directory such as `~/.config/pentacle-private/` for machine configuration you manage yourself. Restrict access to your account. This is a recommended storage layout, not a claim that every component automatically reads it.

If you separately install the Pentacle desktop application with its supported configuration loader and generic example, set `PENTACLE_CONFIG` to an absolute path to your configuration module. An explicitly configured missing file is an error; it must not silently select another host profile. Start from that installation's generic example; this process bundle does not include the desktop loader or application example. Do not commit your populated copy to the public tree.

Maintain an explicit mapping for each other consumer: daemon machine and bind settings, agent-orch spec workspace, mobile development/build configuration and optional integrations. Use only that component's supported configuration interface. Mobile configuration is build input; a desktop environment variable does not configure it, and client bundles cannot keep embedded credentials secret.

Store credentials in your platform credential manager, environment supplied at launch, or the application's documented private credential files. Keep secret values out of versioned overlay files and diagnostic output. Back up non-secret fleet configuration privately if desired; keep encryption keys and credentials under their own access controls.

## Private work records

Create your own spec workspace outside the public checkout when work items contain private data. Copy the reusable process kit there and configure each consumer to use it. Keep real specs, receipts, conversations, runtime state and local identity documents private. Public examples use synthetic projects and hosts.

If several machines share the workspace, explicitly choose a synchronization method, catalog writer and Git commit owner. A single-machine setup needs no fleet sync service. Do not copy an existing private memory repository or its Git history into a public export.

## Migration

Inventory each live consumer and its current input before moving configuration. Stage the external file privately, verify equivalent resolved settings without printing secrets, update the consumer within its authorized window and verify startup and readbacks. Keep a rollback path until acceptance. Moving or deleting the original file before all consumers switch can break scheduled jobs and long-lived processes.

Public packaging must use a positive file allowlist. An ignored configuration file can still be included by a broad package glob; inspect the actual archive. Do not publish old histories to distribute a sanitized configuration change.
