export interface DesignTarget {
  project_id: string;
  design_id: string;
  view_id?: string;
}

export type DesignPresentation = 'workspace' | 'embed';

export interface DashboardLink {
  dashboard: 'design';
  target?: DesignTarget;
  title?: string;
  subtitle?: string;
  revision_id?: string;
}

const designId = /^[a-z0-9][a-z0-9-]{0,79}$/;
const revisionId = /^[0-9a-f]{64}$/;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function hasOwn(record: Record<string, unknown>, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(record, key);
}

function ownValue(record: Record<string, unknown>, key: string): unknown {
  return hasOwn(record, key) ? record[key] : undefined;
}

function isDesignId(value: unknown): value is string {
  return typeof value === 'string' && designId.test(value);
}

function parseDesignTarget(value: unknown): DesignTarget | null {
  if (!isRecord(value) || Object.keys(value).some(
    key => key !== 'project_id' && key !== 'design_id' && key !== 'view_id',
  )) return null;

  const project = ownValue(value, 'project_id');
  const design = ownValue(value, 'design_id');
  if (!isDesignId(project) || !isDesignId(design)) return null;

  if (!hasOwn(value, 'view_id')) return { project_id: project, design_id: design };
  const view = value.view_id;
  return isDesignId(view) ? { project_id: project, design_id: design, view_id: view } : null;
}

function displayText(value: unknown): string | undefined {
  return typeof value === 'string' ? value.trim() || undefined : undefined;
}

/** Read typed Design metadata. A null result leaves the event in the ordinary transcript. */
export function parseDashboardLinkEvent(event: unknown): DashboardLink | null {
  if (!isRecord(event)) return null;
  const raw = ownValue(event, 'raw');
  if (!isRecord(raw)) return null;
  const link = ownValue(raw, 'dashboard_link');
  if (!isRecord(link) || ownValue(link, 'dashboard') !== 'design') return null;

  const result: DashboardLink = { dashboard: 'design' };
  if (hasOwn(link, 'target')) {
    const target = parseDesignTarget(link.target);
    if (!target) return null;
    result.target = target;
  }

  const title = displayText(ownValue(link, 'title'));
  const subtitle = displayText(ownValue(link, 'subtitle'));
  const revision = ownValue(link, 'revision_id');
  if (title !== undefined) result.title = title;
  if (subtitle !== undefined) result.subtitle = subtitle;
  if (typeof revision === 'string' && revisionId.test(revision)) result.revision_id = revision;
  return result;
}

/** Build a current-revision path using the presentation selected by the caller. */
export function buildDesignPath(target: DesignTarget | undefined, presentation: DesignPresentation): string | null {
  if (presentation !== 'workspace' && presentation !== 'embed') return null;
  const prefix = presentation === 'workspace' ? '/next/design' : '/next/embed/design';
  if (target === undefined) return prefix;

  const parsed = parseDesignTarget(target);
  if (!parsed) return null;
  const board = `${prefix}/${parsed.project_id}/${parsed.design_id}`;
  return parsed.view_id === undefined ? board : `${board}/${parsed.view_id}`;
}
