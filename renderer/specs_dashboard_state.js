// ── Specs dashboard pure helpers ────────────────────────────────
// All client-side logic for the Specs dashboard that does not touch the DOM
// lives here so it can be tested with node:test. The render layer
// (dashboards/specs.js) wires these helpers to the DOM and the local daemon
// WS RPCs. Daemon response shapes (ParsedSpec, LeaderRef, capabilities) are
// the contract from public dashboard contract.

// Default statuses for first-render-before-capabilities and as a fallback when
// the daemon's specs.capabilities payload hasn't returned its `statuses` array
// yet. Mirrors the post-migration set from
// public status-set migration: adds `analysis`
// + `deprecated`, drops the transitional `active`, and adds `default_visible`
// per entry. The real source of truth is the daemon's statuses_payload(), which
// reads work/statuses.json. `default_visible: false` keeps a column hidden from
// the default kanban view (filtered by selectVisibleStatuses below).
const DEFAULT_STATUSES = Object.freeze([
  Object.freeze({ name: 'backlog',       order: 1, display_label: 'Backlog',       color: '#8aa097', is_terminal: false, default_visible: true  }),
  Object.freeze({ name: 'analysis',      order: 2, display_label: 'Analysis',      color: '#a890ff', is_terminal: false, default_visible: true  }),
  Object.freeze({ name: 'ready_for_dev', order: 3, display_label: 'Ready for Dev', color: '#a8b8ff', is_terminal: false, default_visible: true  }),
  Object.freeze({ name: 'in_progress',   order: 4, display_label: 'In Progress',   color: '#56d364', is_terminal: false, default_visible: true  }),
  Object.freeze({ name: 'needs_qa',      order: 5, display_label: 'Needs QA',      color: '#f5b78a', is_terminal: false, default_visible: true  }),
  Object.freeze({ name: 'blocked',       order: 6, display_label: 'Blocked',       color: '#d4a300', is_terminal: false, default_visible: true  }),
  Object.freeze({ name: 'completed',     order: 7, display_label: 'Completed',     color: '#2dd4bf', is_terminal: true,  default_visible: false }),
  Object.freeze({ name: 'deprecated',    order: 8, display_label: 'Deprecated',    color: '#7a7a7a', is_terminal: true,  default_visible: false }),
]);

// Filter the statuses array by `default_visible`. When `showAll` is true, returns
// every status (including hidden ones). Renderers call this to drop hidden columns
// from the default kanban view; the toggle function flips a local `showAll` flag.
// Treats missing `default_visible` as `true` for v1 statuses.json back-compat.
function selectVisibleStatuses(statuses, showAll = false) {
  if (showAll) return statuses;
  return statuses.filter((s) => s.default_visible !== false);
}

// Append an "Other" column for rows whose folder isn't in the configured
// statuses set (daemon flags them with `status_unknown: true`). Color is amber
// per Decision §11.
const OTHER_COLUMN = Object.freeze({
  name: '__other__',
  display_label: 'Other',
  color: '#f5b78a',
  is_other: true,
});

function statusesOrDefault(statuses) {
  if (Array.isArray(statuses) && statuses.length > 0) return statuses;
  return DEFAULT_STATUSES;
}

function escapeHtml(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// Partition rows by status for kanban rendering. `statuses` is the array
// from the daemon's capabilities payload (or DEFAULT_STATUSES as a fallback).
// Rows whose `status_unknown` is true (folder name not in statuses.json) are
// routed to a synthetic "Other" bucket so the user sees the typo rather than
// having the row silently disappear.
//
// The `lifecycle` alias was dropped in public redesign of the redesign spec; this
// function now keys exclusively off `row.status`.
function partitionRows(rows, statuses) {
  const unresolved = [];
  const byStatus = {};
  for (const s of statusesOrDefault(statuses)) byStatus[s.name] = [];
  const otherStatusRows = [];
  for (const row of rows || []) {
    if (!row || typeof row !== 'object') continue;
    if (row.synthetic === true) {
      unresolved.push(row);
      continue;
    }
    const name = row.status;
    if (!name) continue;
    if (row.status_unknown === true || !(name in byStatus)) {
      otherStatusRows.push(row);
    } else {
      byStatus[name].push(row);
    }
  }
  return { unresolved, byStatus, otherStatusRows };
}

function _haystackForRow(row) {
  return [
    row.spec_id,
    row.repo,
    row.topic,
    row.title,
    row.status,
    row.machine,
    row.owner,
    row.goal_excerpt,
    row.next_action,
  ]
    .map((value) => String(value == null ? '' : value).toLowerCase())
    .join(' ');
}

// Client-side filter mirror of daemon's specs.list filters. Used when the
// dashboard wants to re-apply filters locally on an already-fetched batch
// (e.g. the user toggles a chip without waiting for a new RPC round-trip).
// The `lifecycle` filter key was retired in public redesign of the redesign spec.
function applyClientFilter(rows, filters) {
  const f = filters || {};
  const search = String(f.search || '').trim().toLowerCase();
  return (rows || []).filter((row) => {
    if (!row) return false;
    for (const key of ['repo', 'machine', 'status']) {
      if (f[key] && row[key] !== f[key]) return false;
    }
    if (!search) return true;
    return _haystackForRow(row).includes(search);
  });
}

// Distinct values for filter chip menus. Synthetic rows are excluded
// because their frontmatter-derived fields are null per spec.
function distinctChipValues(rows) {
  const out = { repo: new Set(), machine: new Set(), status: new Set() };
  for (const row of rows || []) {
    if (!row || row.synthetic) continue;
    if (row.repo) out.repo.add(row.repo);
    if (row.machine) out.machine.add(row.machine);
    if (row.status) out.status.add(row.status);
  }
  return {
    repo: Array.from(out.repo).sort(),
    machine: Array.from(out.machine).sort(),
    status: Array.from(out.status).sort(),
  };
}

function formatProgress(progress) {
  const safe = progress || {};
  const commonDone = Number(safe.common_ac_done || 0);
  const commonTotal = Number(safe.common_ac_total || 0);
  const customDone = Number(safe.custom_ac_done || 0);
  const customTotal = Number(safe.custom_ac_total || 0);
  return {
    label: `${commonDone}/${commonTotal} Common · ${customDone}/${customTotal} Custom`,
    phaseDrift: !!safe.phase_drift,
    phase: safe.phase || 'unknown',
  };
}

function formatLeaderChip(leader) {
  const provider = String(leader && leader.provider ? leader.provider : 'unknown');
  const host = String(leader && leader.host ? leader.host : 'unknown');
  return {
    label: `${provider}@${host}`,
    streamId: leader && leader.stream_id ? String(leader.stream_id) : '',
    provider,
    host,
  };
}

// Decide the primary-action shape per dashboard contract Primary action toggles.
// Returns one of:
//   { kind: 'unresolved', leaderChips, unresolvedReason }
//   { kind: 'drive', leaderChips: [] }
//   { kind: 'spawn_additional', leaderChips }
function decideAction(row) {
  if (!row || typeof row !== 'object') {
    return { kind: 'drive', leaderChips: [] };
  }
  const leaders = Array.isArray(row.live_leaders) ? row.live_leaders : [];
  const leaderChips = leaders.map(formatLeaderChip);
  if (row.synthetic === true) {
    return {
      kind: 'unresolved',
      leaderChips,
      unresolvedReason: row.unresolved_reason || null,
    };
  }
  if (leaders.length === 0) {
    return { kind: 'drive', leaderChips: [] };
  }
  return { kind: 'spawn_additional', leaderChips };
}

// Normalize a host-ish name (spec frontmatter `machine:`, env hostname,
// renderer host id) onto the agent-orch host id local daemon uses in
// specs.capabilities ("hosta" / "hostc" / "hostb" / "hostd"). The
// substring rules mirror renderer/app.js streamHostForHostId — we keep the
// alias table local rather than reaching across the renderer surface so the
// helper module stays standalone-testable.
function normalizeHostAlias(name) {
  const raw = String(name == null ? '' : name).toLowerCase().trim();
  if (!raw) return null;
  if (raw.includes('hosta')) return 'hosta';
  if (raw.includes('hostc')) return 'hostc';
  if (raw.includes('hostb')) return 'hostb';
  if (raw.includes('hostd')) return 'hostd';
  return raw;
}

// Gate hosts for the modal dropdown using daemon capabilities + the spec's
// frontmatter machine. Returns an ordered list of host entries the renderer
// can render verbatim, with exactly one entry's `defaultSelected=true`.
//
// Default-selection rules (per dashboard contract Primary action toggles + public QA rule):
//   1. If spec frontmatter `machine:` resolves (after alias normalization)
//      to a single known host in the capabilities map, that host is the
//      default — EVEN IF it is currently unsupported. The disabled tooltip
//      explains the daemon-supplied reason. The modal must then block
//      submit until the user picks an enabled host.
//   2. Otherwise (frontmatter machine absent, unknown after normalization,
//      or — by the public contract — ambiguous when frontmatter names more than one host
//      via comma-separation), fall back to the current host if enabled,
//      then to the first enabled entry, then to the first entry at all.
//
// `currentHost` may be a renderer-host id like "local" or "hosta"; we
// pipe it through normalizeHostAlias so callers don't have to.
function gateHosts(capabilitiesPayload, specMachine, currentHost) {
  const hostsMap = (capabilitiesPayload && capabilitiesPayload.hosts) || {};
  const hostNames = Object.keys(hostsMap).sort();
  if (hostNames.length === 0) return [];
  const entries = hostNames.map((host) => {
    const cap = hostsMap[host] || {};
    const enabled = !!cap.supports_spec_id;
    let tooltip = null;
    if (!enabled) {
      const reason = cap.reason || 'capability_never_reported';
      const labels = {
        host_offline: 'Host is offline',
        capability_never_reported: 'Host CLI has not reported capabilities yet',
        flag_not_present: 'Host CLI does not support --spec-id',
      };
      tooltip = labels[reason] || `Host capability missing: ${reason}`;
    }
    return {
      host,
      enabled,
      online: !!cap.online,
      reason: cap.reason || null,
      agentOrchVersion: cap.agent_orch_version || null,
      tooltip,
      defaultSelected: false,
    };
  });

  // Step 1 — frontmatter resolves to a single known host? Honor it.
  const specAlias = normalizeHostAlias(specMachine);
  const specSingle = specAlias && entries.find((e) => e.host === specAlias);
  if (specSingle) {
    specSingle.defaultSelected = true;
    return entries;
  }

  // Step 2 — fall back to currentHost (alias-normalized), then any enabled
  // entry, then the first entry. Honoring `enabled` here is deliberate: the
  // current-host fallback should pick something the user can submit with.
  const currentAlias = normalizeHostAlias(currentHost);
  const chosen =
    (currentAlias && entries.find((e) => e.host === currentAlias && e.enabled))
    || (currentAlias && entries.find((e) => e.host === currentAlias))
    || entries.find((e) => e.enabled)
    || entries[0];
  if (chosen) chosen.defaultSelected = true;
  return entries;
}

function frontmatterDriftChip(row) {
  return !!(row && row.frontmatter_drift);
}

function phaseDriftChip(row) {
  return !!(row && row.progress && row.progress.phase_drift);
}

function liveLeaderSummary(row) {
  const leaders = (row && Array.isArray(row.live_leaders)) ? row.live_leaders : [];
  return {
    count: leaders.length,
    chips: leaders.map(formatLeaderChip),
  };
}

// Stable sort within a lifecycle tab: rows with live leaders first, then by
// next_action presence, then by the public contract_id alpha. Synthetic rows do not flow
// through this — they live in a dedicated "Unresolved" section.
function sortForList(rows) {
  return (rows || []).slice().sort((a, b) => {
    const aLead = (a.live_leaders && a.live_leaders.length) ? 1 : 0;
    const bLead = (b.live_leaders && b.live_leaders.length) ? 1 : 0;
    if (aLead !== bLead) return bLead - aLead;
    const aNext = a.next_action ? 1 : 0;
    const bNext = b.next_action ? 1 : 0;
    if (aNext !== bNext) return bNext - aNext;
    return String(a.spec_id || '').localeCompare(String(b.spec_id || ''));
  });
}

// selectVisibleRows is the single entry point the dashboard uses to compute
// what goes in the Unresolved section vs each kanban column. It guarantees
// the spec's invariant: synthetic rows always render in Unresolved regardless
// of any repo/machine/status/search filter. Per-column rows are filtered
// against the user's chips.
//
// Returns:
//   { unresolved: rows[],
//     columns: [{ status: <name>, label, color, rows: rows[] }, ...],
//     other:   { status: '__other__', label, color, rows: rows[] } | null,
//     // Legacy shim for transitional callers (Stage 4 → public redesign):
//     lifecycle:    <selected lifecycle name or null>,
//     lifecycleRows: rows[] filtered for the selected lifecycle (or []) }
//
// `statuses` is the array from the daemon's capabilities payload (preferred)
// or DEFAULT_STATUSES (fallback). `selectedLifecycle` is honored only when a
// transitional caller still wants tab-style single-column output; the kanban
// renderer ignores it.
// Compute what goes in the Unresolved section vs each kanban column. The
// `lifecycle` legacy plumbing was removed in public redesign of the redesign spec.
function selectVisibleRows(rows, filters, statuses) {
  const resolvedStatuses = statusesOrDefault(statuses);
  const partitioned = partitionRows(rows || [], resolvedStatuses);
  const columns = resolvedStatuses.map((s) => ({
    status: s.name,
    label: s.display_label || s.name,
    color: s.color || '#8aa097',
    is_terminal: !!s.is_terminal,
    transitional: !!s.transitional,
    default_visible: s.default_visible !== false,
    rows: applyClientFilter(partitioned.byStatus[s.name] || [], filters),
  }));
  const otherRows = applyClientFilter(partitioned.otherStatusRows || [], filters);
  const other = otherRows.length > 0
    ? { status: OTHER_COLUMN.name, label: OTHER_COLUMN.display_label, color: OTHER_COLUMN.color, is_other: true, rows: otherRows }
    : null;
  return {
    unresolved: partitioned.unresolved,
    columns,
    other,
  };
}

// Human-readable tooltip body for a synthetic row's unresolved_reason. Used
// for both the row's `title` attribute and the detail-pane explanation.
function unresolvedReasonTooltip(unresolvedReason) {
  if (unresolvedReason === 'multiple_matches') {
    return 'Multiple work folders share this spec_id; canonical row was picked in statuses.json precedence (lower order wins; unknown statuses sort last)';
  }
  // Default treats null/unknown as zero_matches per spec wording.
  return 'Live leaders reference an unknown spec_id; resolve by renaming the spec folder or telling the leader to retag';
}

const SUBSYSTEM_STATE_DISABLED = 'disabled';
const SUBSYSTEM_STATE_DEGRADED = 'degraded';
const SUBSYSTEM_STATE_HEALTHY = 'healthy';

// Translate a subsystem_state object (from specs.list / specs.changed) into a
// renderer-friendly status descriptor. Tolerant of the daemon adding fields
// later — only the keys we read here are required.
function describeSubsystemState(payload) {
  if (!payload || typeof payload !== 'object') {
    return { state: SUBSYSTEM_STATE_HEALTHY, message: null };
  }
  if (payload.disabled === true || payload.specs_subsystem_disabled === true) {
    return {
      state: SUBSYSTEM_STATE_DISABLED,
      message: payload.disabled_reason || 'Specs subsystem disabled (memory-root missing or invalid)',
    };
  }
  if (payload.watcher_attached === false || payload.push_disabled === true) {
    return {
      state: SUBSYSTEM_STATE_DEGRADED,
      message: payload.degraded_reason || 'Filesystem watcher attach failed; using mtime polling fallback',
    };
  }
  return { state: SUBSYSTEM_STATE_HEALTHY, message: null };
}

module.exports = {
  DEFAULT_STATUSES,
  OTHER_COLUMN,
  SUBSYSTEM_STATE_DISABLED,
  SUBSYSTEM_STATE_DEGRADED,
  SUBSYSTEM_STATE_HEALTHY,
  applyClientFilter,
  decideAction,
  describeSubsystemState,
  distinctChipValues,
  escapeHtml,
  selectVisibleStatuses,
  formatLeaderChip,
  formatProgress,
  frontmatterDriftChip,
  gateHosts,
  liveLeaderSummary,
  normalizeHostAlias,
  partitionRows,
  phaseDriftChip,
  selectVisibleRows,
  sortForList,
  statusesOrDefault,
  unresolvedReasonTooltip,
};
