/**
 * Business dashboard envelope helpers.
 *
 * Resolves the per-batch snapshot from a `bart.business` envelope so the IPC
 * handler in main.js stays a thin wrapper around `hubClient.get(...)` and the
 * resolution logic stays unit-testable.
 */

'use strict';

const NUMERIC_KEYS = ['scraped', 'deduped', 'enriched', 'qualified', 'promoted', 'skipped'];
const STAGE_KEYS = ['scrape', 'enrich', 'qualify', 'promote'];
const QUEUE_KEYS = ['pending', 'running', 'completed', 'failed', 'cancelled', 'total', 'total_scraped', 'total_ingested', 'total_duped'];

function addNumber(target, key, value) {
  target[key] = (Number(target[key]) || 0) + (Number(value) || 0);
}

function mergeMetricMap(target, source) {
  Object.entries(source || {}).forEach(([name, stats]) => {
    if (!target[name]) target[name] = {};
    Object.entries(stats || {}).forEach(([key, value]) => addNumber(target[name], key, value));
  });
}

function mergedState(states) {
  if (states.includes('failed')) return 'failed';
  if (states.includes('blocked')) return 'blocked';
  if (states.includes('running')) return 'running';
  if (states.includes('waiting')) return 'waiting';
  if (states.length && states.every((state) => state === 'complete')) return 'complete';
  return states[0] || 'waiting';
}

function aggregateStages(snapshots) {
  return STAGE_KEYS.map((key) => {
    const stages = snapshots
      .map((snapshot) => (snapshot.pipeline_stages || []).find((stage) => stage && stage.stage === key))
      .filter(Boolean);
    const metrics = {};
    stages.forEach((stage) => mergeMetricMap({ metrics }, { metrics: stage.metrics || {} }));
    return {
      stage: key,
      state: mergedState(stages.map((stage) => stage.state || 'waiting')),
      metrics: metrics.metrics || {},
      error: stages.find((stage) => stage.error) ? stages.find((stage) => stage.error).error : null,
      updated_at: stages.map((stage) => stage.updated_at).filter(Boolean).sort().pop() || null,
    };
  });
}

function aggregateQueue(snapshots) {
  const queue = {
    running_job: null,
    failed_jobs: [],
    jobs: [],
    by_area: {},
  };
  QUEUE_KEYS.forEach((key) => { queue[key] = 0; });
  snapshots.forEach((snapshot) => {
    const sq = snapshot.scraper_queue || {};
    QUEUE_KEYS.forEach((key) => addNumber(queue, key, sq[key]));
    mergeMetricMap(queue.by_area, sq.by_area);
    if (sq.running_job && !queue.running_job) queue.running_job = sq.running_job;
    if (Array.isArray(sq.failed_jobs)) queue.failed_jobs.push(...sq.failed_jobs);
    if (Array.isArray(sq.jobs)) queue.jobs.push(...sq.jobs);
    else {
      if (sq.running_job) queue.jobs.push(sq.running_job);
      if (Array.isArray(sq.failed_jobs)) queue.jobs.push(...sq.failed_jobs);
    }
  });
  return queue;
}

function aggregateReports(data, snapshots, key) {
  const reports = [];
  if (Array.isArray(data[key])) reports.push(...data[key]);
  snapshots.forEach((snapshot) => {
    if (Array.isArray(snapshot[key])) reports.push(...snapshot[key]);
  });
  return reports;
}

function aggregateBusinessSnapshots(data, snapshots, base) {
  const snapshotList = Object.values(snapshots || {});
  const aggregate = {
    batch: 'All leads',
    all_leads: true,
    all_batches: data.all_batches,
    all_batches_meta: data.all_batches_meta,
    default_batch: data.default_batch,
    by_area: {},
    by_source: {},
    _missing_batch: null,
    ...base,
  };
  NUMERIC_KEYS.forEach((key) => { aggregate[key] = 0; });
  snapshotList.forEach((snapshot) => {
    NUMERIC_KEYS.forEach((key) => addNumber(aggregate, key, snapshot[key]));
    mergeMetricMap(aggregate.by_area, snapshot.by_area);
    mergeMetricMap(aggregate.by_source, snapshot.by_source);
  });
  aggregate.pipeline_stages = aggregateStages(snapshotList);
  const activeStage = aggregate.pipeline_stages.find((stage) => ['failed', 'blocked', 'running', 'waiting'].includes(stage.state))
    || aggregate.pipeline_stages[aggregate.pipeline_stages.length - 1];
  aggregate.pipeline_summary = {
    state_machine_batch: 'All leads',
    current_stage: activeStage ? activeStage.stage : null,
    current_state: activeStage ? activeStage.state : null,
    blocked: !!aggregate.pipeline_stages.find((stage) => stage.state === 'blocked'),
  };
  aggregate.scraper_queue = aggregateQueue(snapshotList);
  aggregate.coverage_reports = aggregateReports(data, snapshotList, 'coverage_reports');
  aggregate.recent_errors = aggregateReports(data, snapshotList, 'recent_errors');
  aggregate.crm_source_counts = data.crm_source_counts || data.crm_counts || {};
  return aggregate;
}

function resolveBusinessSnapshot(env, hubConnected, nowMs) {
  const connected = !!hubConnected;
  if (!env) {
    return { error: 'no data yet from hub', _transport_stale: !connected };
  }
  const ageSec = (nowMs - new Date(env.server_received_at).getTime()) / 1000;
  const ttl = env.freshness_ttl_sec != null ? env.freshness_ttl_sec : 120;
  const base = {
    _updated_at: env.updated_at,
    _server_received_at: env.server_received_at,
    _age_sec: ageSec,
    _transport_stale: !connected,
    _data_stale: ageSec > ttl,
  };
  const data = env.data || {};
  const snapshots = data.snapshots;
  const defaultBatch = data.default_batch;

  if (snapshots && typeof snapshots === 'object') {
    const aggregate = aggregateBusinessSnapshots(data, snapshots, base);
    if (!Object.keys(snapshots).length) {
      return {
        ...data,
        all_batches: data.all_batches,
        all_batches_meta: data.all_batches_meta,
        default_batch: defaultBatch,
        ...base,
      };
    }
    return aggregate;
  }

  return { ...data, ...base };
}

module.exports = { resolveBusinessSnapshot };
