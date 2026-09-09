/** Shared D1/M1 wire contract. Timestamps stay raw UTC strings. */
export interface ChildAgent {
  stream_id: string;
  session_generation: string;
  display_name: string;
  role: string | null;
  objective: string | null;
  state: 'working' | 'idle' | 'blocked' | 'done';
  model: string | null;
  since: string | null;
}

export interface ChildExchangeRow {
  row_id: string;
  ref_id: string;
  ts: string;
  direction: 'parent_to_child' | 'child_to_parent';
  kind: 'brief' | 'tell' | 'report';
  text: string;
  truncated: boolean;
}

export interface ThreadReadRequest {
  type: 'thread.read';
  request_id: string;
  child_stream_id: string;
  parent_stream_id?: string;
  limit?: number;
  cursor?: string;
}

export type ThreadErrorCode = 'unauthorized' | 'parent_required' | 'not_direct_child'
  | 'pair_closed' | 'cursor_invalid' | 'limit_invalid';
export type ThreadReadResponse = {
  type: 'thread.read.ok'; request_id: string; ok: true;
  parent_stream_id: string; child_stream_id: string;
  parent_generation: string; child_generation: string;
  rows: ChildExchangeRow[]; next_cursor: string | null;
} | {
  type: 'thread.error'; request_id: string; ok: false;
  error_code: ThreadErrorCode; message: string;
};

export interface DaemonQuestionAnswer {
  type: 'prompt.answer'; request_id: string; question_id: string;
  selections?: string[]; text?: string;
}

export function validSpawnObjective(value: unknown): value is string {
  return typeof value === 'string' && value.trim().length > 0
    && [...value].length <= 120 && !/[\r\n\v\f\u001c-\u001e\u0085\u2028\u2029]/u.test(value);
}

const record = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);
const nullableString = (value: unknown) => value === null || typeof value === 'string';
const utc = (value: unknown) => typeof value === 'string'
  && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$/.test(value)
  && Number.isFinite(Date.parse(value));

export function decodeChildAgents(value: unknown): ChildAgent[] | undefined {
  if (!Array.isArray(value)) return undefined;
  if (!value.every(row => record(row)
    && ['stream_id', 'session_generation', 'display_name'].every(k => typeof row[k] === 'string')
    && ['role', 'objective', 'model'].every(k => nullableString(row[k]))
    && (row.since === null || utc(row.since))
    && ['working', 'idle', 'blocked', 'done'].includes(String(row.state)))) return undefined;
  return value.map(row => ({ ...row })) as ChildAgent[];
}

export function decodeThreadRead(value: unknown): ThreadReadResponse | null {
  if (!record(value) || typeof value.request_id !== 'string') return null;
  if (value.type === 'thread.error' && value.ok === false
    && ['unauthorized', 'parent_required', 'not_direct_child', 'pair_closed', 'cursor_invalid', 'limit_invalid'].includes(String(value.error_code))
    && typeof value.message === 'string') return value as ThreadReadResponse;
  if (value.type !== 'thread.read.ok' || value.ok !== true
    || !['parent_stream_id', 'child_stream_id', 'parent_generation', 'child_generation'].every(k => typeof value[k] === 'string')
    || !nullableString(value.next_cursor) || !Array.isArray(value.rows) || value.rows.length > 50) return null;
  if (!value.rows.every(row => record(row)
    && ['row_id', 'ref_id', 'text'].every(k => typeof row[k] === 'string')
    && utc(row.ts) && typeof row.truncated === 'boolean'
    && ['parent_to_child', 'child_to_parent'].includes(String(row.direction))
    && ['brief', 'tell', 'report'].includes(String(row.kind))
    && row.row_id === `${row.kind}:${row.ref_id}`)) return null;
  return value as ThreadReadResponse;
}
