# Qualified integration seams at release

Selection is an opt-in supplement to existing release checks. The mandatory v2
merge gate still runs once per qualifying final candidate; deploy reuses its
passing `--gate-evidence` or runs the existing inline fallback. CI, hooks and
activation readbacks remain required.

**Qualified scope:** `deploy/qualified_seams.json` records one finite accepted
client producer-wire/web-render/mobile-model tuple and one finite installed
provider-startup tuple. Source references, dependency/harness scopes, actual
configuration inputs, independent QA and evidence are pinned by digests.
Provider reuse also binds the actual activation endpoint and host; changed
startup or dependency inputs refuse. Satellite and deploy surfaces remain
unqualified. Synthetic tests exercise admission separately. A larger
suite cannot cure a known coverage gap. Legacy deployment
reports `selective_qualification: not_enabled`; it cannot certify selective
acceptance or waive required gaps. Final integration QA and an authorized
representative component release remain prerequisites to activation. If the
complete baseline union selects any other unqualified boundary, it still blocks.

## Actual inputs

The release owner supplies private `pentacle.release-inputs.v1` JSON with:

| Field | Meaning |
| --- | --- |
| `candidate` | Full immutable Pentacle SHA, checked out clean. |
| `installed` | Every independently installed instance, with all four roles: daemon, web, mobile, satellite. Multiple instances have unique component names. |
| `external_inputs` | Exactly `client_versions`, `provider_config`, `satellite_config`, `deploy_config`, each with `installed` and `candidate` tuples. |
| `activation` | `{ "url": "ws://127.0.0.1:7791", "host": "primary" }`, using the installed local machine name and existing Codex promptless canary. Other scopes require their own proved runner contract. |

Installed rows are `{component, role, repository, sha, receipt}` with optional
`receipt_format`. The finite client tuple supports exactly one observed web
consumer: its `client_inventory` source and complete artifact manifest must match
`client_versions.installed.identity.web`, and every artifact hash must have a bound
file. Metadata-only web observations, contradictions and multiple-web shapes refuse.
File/receipt bindings are `{path, sha256}` with absolute paths.
Repositories are `https://github.com/HJK6/pentacle.git`, except mobile uses
`https://github.com/HJK6/pentacle-mobile.git`. Mobile source is an external input,
never a Pentacle Git baseline. All required same-repository commits must exist
locally. Reuse existing receipts:

- `client_inventory`: existing census `installed_web.source`/`active_pid`/`files`
  and `installed_mobile.source`/`build`. Its nested receipt hashes are checked.
- `daemon_stamp`: existing observed `daemon_runtime_readback` with matching
  `sha`/`target_sha`, positive `pid`, and `outcome: observed`. Requested deploy
  SHA and `runtime_manifest` are insufficient.
- `satellite_participants`: pin preflight's exact candidate, fresh
  `observed_at_epoch` (maximum 900 seconds), and online participant whose `host`
  matches the component. Require actual running `installed_source_sha` plus
  dependency, consumer payload, configuration and installed CLI digests. Helper
  SHA or checkout HEAD cannot substitute for emitter source; offline stays
  non-green. `installed_helpers_sha256` is the exact retained per-host helper
  manifest byte digest, with a matching `installed_helpers_manifest` file
  binding. Manifest and referenced immutable files must also be bound in both
  satellite external-input file sets. The legacy 40-character helper source is
  historical metadata only. Configured mixed roots/releases/absence and unknown
  loaded-module limits remain explicit; a digest does not qualify recovery.
- `observed_component` (default): matching `component`, `repository`, `sha` and
  `observed: true` from an actual bounded installed readback where no existing
  format applies. Web consumers require the inventory format above.

External tuples are `{identity: {...nonsecret exact tuple...}, files: [...]}`.
Bind actual executable/config/client artifacts and retained readback manifests;
provider native freeze/result files can be reused directly. Installed and
candidate identities describe their distinct sources and accounts. Client
`identity.mobile_source_shas` is a sorted installed source list, matching mobile
rows; the candidate tuple separately names proposed consumer sources.
`deploy_config.candidate.files` includes the actual installed machine file.
When the union selects `cli-pytest`, it also binds the frozen instruction
snapshot manifest and every manifest file. Set `AGENT_ORCH_MEMORY_REPO` to that
read-only snapshot and declare it with the existing `--pass-env` option for the
CLI target. The snapshot contains the complete `agents/*_baseline.md` membership,
`AGENTS.md`, and `docs/config/agent_orchestration.md`, with original provenance
and content hashes. Symlinks, writable inputs, missing files and drift refuse.
Canonical prepared/terminal evidence binds the actual path and contents;
environment names alone cannot establish this input. Ordinary CLI checks may
omit the input; selected release admission requires it and zero skipped tests.
Deploy checks that every configured remote host has its satellite baseline;
omitting an older configured participant cannot yield a safe empty selection.
Unsupported or absent identities refuse. Keep credentials out of inputs.

The owner establishes a complete, current installed census with process/artifact
and configuration observations. Hashes prevent drift; historical observations
do not become current merely because their files remain present.

## Commands and receipts

Prepare the conservative union from every actual installed Pentacle baseline:

```sh
python services/chat-stream-v2/deploy/release_selection.py \
  --candidate "$CANDIDATE_SHA" --release-inputs "$RELEASE_INPUTS" --out "$SELECTION_PLAN"
```

Unqualified coverage returns 2 and retains the plan for diagnosis. Missing
baselines, invalid selection and deleted targets also refuse. The plan binds
selector/map/config/source/toolchain bytes, complete input tuples and target
scope. Unknown/shared changes select broad coverage; known unresolved gaps
remain non-green after additional tests.

After accepted qualification for the affected scopes, run the selected targets
through existing `gate_env.py` and supplement an existing passing merge receipt:

```sh
python services/chat-stream-v2/deploy/release_selection.py \
  --candidate "$CANDIDATE_SHA" --release-inputs "$RELEASE_INPUTS" \
  --gate-evidence "$MERGE_RECEIPT" --workspace "$OWNED_GATE_WORKSPACE" \
  --evidence-dir "$SEAM_EVIDENCE" --out "$SUPPLEMENTED_RECEIPT"
python services/chat-stream-v2/deploy/deploy.py chat-streamd-v2 \
  --release-checkout "$RELEASE_CHECKOUT" --ref "$CANDIDATE_SHA" \
  --gate-evidence "$SUPPLEMENTED_RECEIPT" --release-inputs "$RELEASE_INPUTS" \
  --deployer-stream-id "$DEPLOYER_STREAM"
```

The original merge receipt is preserved. `seam_results` adds the complete input
identity and one result per selected target, binding existing started/prepared/
terminal events and subject artifacts. Seam execution omits the independently
required merge target. Duplicate, skipped, failed, untested, contradictory,
dirty-source or stale identity rows refuse. Admission and bindings are checked
again before activation. Missing/stale seam proof refuses; a green merge alone
cannot replace it. There is no arbitrary command execution or new cache.
For mapped client changes, the existing web runner receives the named
`PENTACLE_WEB_GATE_SCENARIOS=installed-client-seam` filter through its supported
`--pass-env` mechanism. Admission also requires that exact scenario in the
hash-bound browser log and binds its printed PASS verdict, assertions and cleanup;
the environment name alone is insufficient. Broad fallback runs the default full web target. This
does not remove any ordinary mandatory web/CI checks. Installed web/mobile
proof is separately reused only within the registered exact source/input bounds;
the named candidate-web scenario alone cannot establish installed/mobile proof.

| Concrete case | Selection and initial disposition |
| --- | --- |
| Client-only renderer/producer change | Union all installed web/daemon baselines and external mobile versions; client seams selected. The exact registered source/tuple can qualify; another tuple or any other selected unqualified boundary refuses. |
| Same candidate, provider config/executable/account drift | Select provider startup; old external-tuple evidence is stale. Run required checks or refuse. Native-only or installed-old evidence cannot certify changed candidate send/reply. |
| Satellite source/config drift | Actual emitter baseline and config select satellite transition and affected client wire checks. Missing source/config/dependency/recovery proof refuses. |
| Docs-only from every actual baseline, unchanged external tuples | No new seam targets; mandatory merge and local activation floor remain. Latest-PR docs-only does not erase older installed changes. |
| Shared tooling/dependency or unknown runtime change | Broad existing gate families plus every required seam. Exact complete qualified broad proof can pass; a bigger suite with the same known gap cannot. |

## Qualification and runtime boundaries

Qualification entries are reviewed data, never caller input. Public data holds
only scope digests, immutable source reference/patterns, finite assertions,
remaining omissions and QA/evidence references; private tuples and paths stay
in release inputs. Source equivalence compares actual Git bytes for the entire
declared producer/harness/dependency scope with the tested source reference.
Changed bytes or tuples invalidate this reuse instead of relabelling a capture
as a later candidate. Result receipts separately bind the entire candidate,
admission code and registry. Real entries require the surface owner's accepted evidence
and actual map/test files at final HEAD. One provider/account or model/transport
result never certifies other tuples, native rendering or changed startup code.
The client entry retains native rendering, mobile authentication/network timing
and arbitrary historical-version omissions. The private release packet records
the exact accepted client input fragment and its retained artifact bindings.

Qualified deploy invokes `spawn_fleet_smoke.py URL HOST` and requires exactly
`(HOST, codex, promptless)`. Stamps label other cells `not_selected: UNTESTED`,
set `scope: qualified_affected`, and never claim full-fleet acceptance from that
canary. Missing/quota/cleanup/offline results stay non-green. The separately
governed scheduled full matrix is unchanged.

Existing boot/source, slow-consumer and useful send/reply readbacks remain.
Preactivation refusal restores transaction preimages. Postactivation recovery
and classifications remain unchanged: harness/quota/cleanup errors are not
automatic product rollback. Satellite pin/equality recovery stays with its pin
tool. Publication, live windows, activation and representative-release proof
remain owned by the release owner and deployment relay.
