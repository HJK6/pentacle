import type { WorkLane as CurrentWorkLane, WorkLaneGoalProgress as WorkLaneDefaultGoal, WorkLaneMember } from './workLanes';

// The pinned mobile helper reads these inventory fields in their pre-normalized
// shape. Keep that contract local so its presentation logic ports unchanged.
type WorkLane = Omit<CurrentWorkLane, 'goal_road' | 'goal_classes' | 'goal_counts' | 'default_goal'> & {
  goal_road?: string;
  goal_classes?: string;
  goal_counts?: readonly number[];
  default_goal?: WorkLaneDefaultGoal;
};

export type GoalClass = 'working' | 'moving' | 'blocked' | 'queued' | 'done';
export type LaneClass = 'parked' | 'paused' | 'working' | 'moving' | 'idle';
export type GoalPosition = 'next' | 'later';
export type GoalDot = 'done' | 'working' | 'moving' | 'blocked' | GoalPosition;
export type ItemKind = 'done' | 'needs' | 'working' | 'in_progress' | 'blocked' | 'next' | 'later';
export type RoadGoal = WorkLaneDefaultGoal & { goal_id: string | null; title: string; rank: number; pos?: number };
/** One stop of a lane's road. `number` counts unfinished goals; `position` is set only for goals nobody has started. */
export type RoadStop = { goal: RoadGoal; goalClass: GoalClass; isDefault: boolean; number: number | null; position: GoalPosition | null };
export type StopWord = { text: string; tone?: 'yellow' | 'green' };
const classes: Record<string, GoalClass> = { w: 'working', m: 'moving', b: 'blocked', q: 'queued', d: 'done' };
const DEFAULT_RANK = Number.MAX_SAFE_INTEGER;

export function readableGoalClass(goal: WorkLaneDefaultGoal, letter?: string, legacyInventory = false): GoalClass {
  if (goal.total === 0) return 'queued';
  if (letter && classes[letter]) return classes[letter];
  if (legacyInventory) return goal.state === 'in_progress' ? 'moving' : goal.state === 'done' ? 'done' : 'queued';
  if (goal.total > 0 && goal.done === goal.total) return 'done';
  if (goal.moving !== undefined && goal.blocked !== undefined && goal.working !== undefined) {
    if (goal.working) return 'working';
    if (goal.moving > 0) return 'moving';
    if (goal.blocked > 0) return 'blocked';
    return 'queued';
  }
  return goal.state === 'in_progress' ? 'moving' : goal.state === 'done' && goal.total > 0 ? 'done' : 'queued';
}

/** Older detail replies omit the default-goal aggregate; derive it only from a complete member list. */
export function derivedDefaultGoal(lane: WorkLane, members: WorkLaneMember[], complete: boolean): WorkLaneDefaultGoal | undefined {
  if (lane.default_goal || !complete) return lane.default_goal;
  const loose = members.filter(m => !m.goal_id && !readableDeprecated(m));
  if (!loose.length) return undefined;
  const done = loose.filter(m => m.status === 'completed').length;
  return { total: loose.length, done, state: done === loose.length ? 'done' : loose.some(m => ['analysis', 'in_progress', 'needs_qa'].includes(m.status ?? '')) ? 'in_progress' : 'not_started' };
}

/**
 * The lane's goals in goal order, default goal last while it has an open member. Letters pair by sent index before
 * sorting. A valid `goal_road` orders by `pos` and makes numbers and position words exact for goals the frame did not send.
 */
export function readableRoad(lane: WorkLane, { legacy = false, defaultGoal = lane.default_goal }: { legacy?: boolean; defaultGoal?: WorkLaneDefaultGoal } = {}): RoadStop[] {
  const sent = lane.goals ?? [];
  const goals = sent.map((goal, i) => ({ goal: goal as RoadGoal, goalClass: readableGoalClass(goal, lane.goal_classes?.[i], legacy), isDefault: false }));
  if (defaultGoal && defaultGoal.total > 0) {
    const goalClass = readableGoalClass(defaultGoal, lane.goal_classes?.[sent.length], legacy);
    if (goalClass !== 'done') goals.push({ goal: { ...defaultGoal, goal_id: null, title: lane.title, rank: DEFAULT_RANK }, goalClass, isDefault: true });
  }
  goals.sort((a, b) => a.isDefault !== b.isDefault ? (a.isDefault ? 1 : -1)
    : a.goal.pos !== undefined && b.goal.pos !== undefined ? a.goal.pos - b.goal.pos
      : a.goal.rank - b.goal.rank || a.goal.title.localeCompare(b.goal.title));
  const road = lane.goal_road;
  // Unfinished and not-yet-started goals that precede a stop on the road, counted over every goal of the lane.
  const before = (at: number, waiting: boolean) => [...road!.slice(0, at)].filter(l => l !== 'd' && (!waiting || l === 'b' || l === 'q')).length;
  let open = 0, waiting = 0;
  return goals.map(g => {
    const done = g.goalClass === 'done', started = g.goalClass === 'working' || g.goalClass === 'moving';
    const at = road !== undefined ? (g.isDefault ? road.length : g.goal.pos) : undefined;
    const number = done ? null : at !== undefined ? before(at, false) + 1 : ++open;
    const ahead = done || started ? null : at !== undefined ? before(at, true) : waiting++;
    return { ...g, number, position: ahead === null ? null : ahead === 0 ? 'next' : 'later' };
  });
}

export function readableDot(stop: Pick<RoadStop, 'goalClass' | 'position'>): GoalDot {
  return stop.goalClass === 'queued' ? stop.position ?? 'later' : stop.goalClass;
}

/** One dot per goal in goal order; `more` counts goals the frame did not send when no valid road is present. */
export function readableDots(lane: WorkLane, road = readableRoad(lane, { legacy: true })): { dots: GoalDot[]; more: number } {
  if (lane.goal_road === undefined) {
    const sent = lane.goals?.length ?? 0;
    return { dots: road.map(readableDot), more: Math.max(0, (lane.goals_total ?? sent) - sent) };
  }
  let waiting = 0;
  const dots: GoalDot[] = [...lane.goal_road].map(l => l === 'd' || l === 'w' || l === 'm' ? classes[l] as GoalDot
    : l === 'b' ? (waiting++, 'blocked') : waiting++ === 0 ? 'next' : 'later');
  const def = road.find(stop => stop.isDefault);
  return { dots: def ? [...dots, readableDot(def)] : dots, more: 0 };
}

/** Lane class, first match wins. `working` is the app's live-seat signal for the lane. */
export function readableLaneClass(lane: WorkLane, working = false): LaneClass {
  if (lane.parked) return 'parked';
  if (lane.state === 'paused') return 'paused';
  if (lane.activity === 'working') return 'working';
  if (working || /w/.test(`${lane.goal_classes ?? ''}${lane.goal_road ?? ''}`)) return 'working';
  if (lane.goal_counts) return lane.goal_counts[0] > 0 ? 'moving' : 'idle';
  if (lane.goal_classes || lane.goal_road) return /m/.test(`${lane.goal_classes ?? ''}${lane.goal_road ?? ''}`) ? 'moving' : 'idle';
  return readableRoad(lane, { legacy: true }).some(stop => stop.goalClass === 'moving') ? 'moving' : 'idle';
}

/** Additional prose for the existing lane age slots. Parked lanes keep their hold text. */
function daemonLaneWord(lane: Pick<CurrentWorkLane, 'activity' | 'external'>): string | null {
  if (lane.activity === 'waiting_on_dot') return `waiting on ${lane.external?.label ?? 'Dot'}`;
  return lane.activity === 'blocked' ? 'blocked' : null;
}

export function readableLaneWord(lane: WorkLane): string | null {
  return lane.parked ? null : daemonLaneWord(lane);
}

const laneLifecycleLabels = { active: 'Active', blocked: 'Blocked', paused: 'Paused', done: 'Done' };
const laneLeadLabels = { working: 'Lead working', idle: 'Lead idle', offline: 'Lead offline', lost: 'No visible lead' };

/** The aggregate lane activity and the bound seat's health are independent. */
export function readableLaneRowStatus(lane: CurrentWorkLane, current = true, placedQuestion = false) {
  const lifecycle = lane.parked ? 'Parked' : laneLifecycleLabels[lane.state] ?? null;
  const detail = lane.parked?.reason ?? (lane.state === 'blocked' ? lane.blocker : null);
  if (!current) {
    return { primary: 'Connection lost', lifecycle, detail, secondaryActivity: null, lead: null, questionRoute: false };
  }
  const word = daemonLaneWord(lane);
  const activity = lane.activity === 'working' ? 'Working'
    : lane.activity === 'waiting_on_you' ? 'Waiting on you'
    : word ? word[0].toUpperCase() + word.slice(1) : null;
  const held = !!lane.parked || lane.state === 'paused';
  const lead = lane.lead;
  const health = lead && (lead.status === 'closed' || !lead.presence.online) ? 'offline'
    : !lead?.qualifies ? 'lost' : lead.presence.working ? 'working' : 'idle';
  return {
    primary: held ? lifecycle : activity ?? (lane.state === 'blocked' ? lifecycle : null),
    lifecycle,
    detail,
    secondaryActivity: held && activity !== 'Blocked' ? activity : null,
    lead: { health, label: laneLeadLabels[health] },
    questionRoute: lane.activity === 'waiting_on_you' || placedQuestion
      || (lane.progress?.members ?? []).some(member => (member.open_questions?.count ?? 0) > 0),
  };
}

/** Header counts; parked lanes are in none of them. */
export function readableHomeCounts(laneClasses: LaneClass[]) {
  const n = (...of: LaneClass[]) => laneClasses.filter(c => of.includes(c)).length;
  return { working: n('working'), moving: n('moving'), quiet: n('idle', 'paused'), parked: n('parked') };
}

/** Last activity, the stamp the home card age shows: `state_at` for a paused lane, otherwise `progress_at`. */
export function readableLastActivity(lane: WorkLane, cls: LaneClass): string | null | undefined {
  return cls === 'paused' ? lane.state_at : lane.progress_at;
}

/** Most recent first; a missing or unreadable stamp sorts last; ties keep list order. */
export function readableByActivity<T>(items: readonly T[], at: (item: T) => string | null | undefined): T[] {
  const time = (item: T) => { const t = Date.parse(at(item) ?? ''); return Number.isFinite(t) ? t : -Infinity; };
  return items.map((item, i) => ({ item, i, t: time(item) })).sort((a, b) => (b.t - a.t) || a.i - b.i).map(row => row.item);
}

export type MapLane = { lane: WorkLane; cls: LaneClass; question: boolean };
/**
 * The home map's lanes. Up to six busy lanes sit inside in list order. With more, six are chosen: open questions first,
 * then working, then moving, each by last activity; the rest go on the outer circle in that order. Quiet (idle and paused)
 * lanes follow on the outer circle by last activity. Parked lanes are never on the map.
 */
export function readableMapLanes<T extends MapLane>(lanes: readonly T[]): { inner: T[]; extra: T[]; quiet: T[] } {
  const recent = (group: T[]) => readableByActivity(group, l => readableLastActivity(l.lane, l.cls));
  const busy = lanes.filter(l => l.cls === 'working' || l.cls === 'moving');
  const quiet = recent(lanes.filter(l => l.cls === 'idle' || l.cls === 'paused'));
  if (busy.length <= 6) return { inner: busy, extra: [], quiet };
  const ranked = [...recent(busy.filter(l => l.question)), ...recent(busy.filter(l => !l.question && l.cls === 'working')),
    ...recent(busy.filter(l => !l.question && l.cls === 'moving'))];
  return { inner: ranked.slice(0, 6), extra: ranked.slice(6), quiet };
}

/** Working goals first, then moving goals, each in goal order; at most two. */
export function readableLeadGoals(lane: WorkLane, road = readableRoad(lane, { legacy: true })): RoadStop[] {
  return [...road.filter(s => s.goalClass === 'working'), ...road.filter(s => s.goalClass === 'moving')].slice(0, 2);
}

/** Home cards: started goals are equal; choose at most two in lane goal order, never done goals. */
export function readableCardGoals(lane: WorkLane, road = readableRoad(lane, { legacy: true })): RoadStop[] {
  const unfinished = road.filter(stop => stop.goalClass !== 'done');
  const started = unfinished.filter(stop => ['working', 'moving', 'blocked'].includes(stop.goalClass));
  if (started.length >= 2) return started.slice(0, 2);
  if (!started.length || started[0] === unfinished[0]) return unfinished.slice(0, 1);
  return [unfinished[0], started[0]];
}

export function readableDeprecated(member: WorkLaneMember) {
  return member.status === 'deprecated' || member.terminal === 'deprecated';
}

/** Item kind, first match wins. It decides the mark, the goal-screen section and the row order. */
export function readableItemKind(member: WorkLaneMember): ItemKind {
  if (member.status === 'completed') return 'done';
  if ((member.open_questions?.count ?? 0) > 0) return 'needs';
  if (member.external?.some(entry => entry.state === 'working')) return 'working';
  if (member.seats?.some(s => s.activity === 'working')) return 'working';
  if (member.status === 'in_progress' || member.status === 'needs_qa') return 'in_progress';
  if (member.status === 'blocked') return 'blocked';
  if (member.status === 'ready_for_dev' || member.status === 'analysis') return 'next';
  return 'later';
}
export const ITEM_KIND_ORDER: readonly ItemKind[] = ['needs', 'working', 'in_progress', 'blocked', 'next', 'later', 'done'];
export function readableItemOrder(members: WorkLaneMember[]) {
  return members.filter(m => !readableDeprecated(m)).map((member, i) => ({ member, kind: readableItemKind(member), i }))
    .sort((a, b) => ITEM_KIND_ORDER.indexOf(a.kind) - ITEM_KIND_ORDER.indexOf(b.kind) || a.i - b.i);
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
export function readableDay(at: string | null | undefined): string | null {
  if (!at || !Number.isFinite(Date.parse(at))) return null;
  const day = new Date(at);
  return `${MONTHS[day.getMonth()]} ${day.getDate()}`;
}

/** The word on the right of a road stop, first match wins. A goal header uses rules 1 to 3 with `expanded`. */
export function readableStopWord(stop: RoadStop, { expanded = false, needsYou = false, now = Date.now() } = {}): StopWord | null {
  const goal = stop.goal, dt = `${goal.done}/${goal.total}`;
  if (stop.goalClass === 'done') { const day = readableDay(goal.progress_at); return { text: day ? `done ${day}` : 'done' }; }
  if (needsYou) return { text: 'needs you', tone: 'yellow' };
  if (expanded) {
    const age = readableAge(goal.progress_at, now);
    return age ? { text: age, ...(stop.goalClass === 'working' ? { tone: 'green' as const } : {}) } : null;
  }
  if (goal.total === 0) return { text: 'no items yet' };
  if (stop.goalClass === 'working' || stop.goalClass === 'moving') return { text: `in progress · ${dt}` };
  if (stop.goalClass === 'blocked') return { text: goal.blocked !== undefined ? `${goal.blocked} blocked · ${dt}` : `blocked · ${dt}`, tone: 'yellow' };
  return { text: `${stop.position ?? 'later'} · ${dt}` };
}

export function readableAge(at: string | null | undefined, now = Date.now()): string | null {
  if (!at || !Number.isFinite(Date.parse(at))) return null;
  const mins = Math.max(0, Math.floor((now - Date.parse(at)) / 60000));
  return mins < 1 ? 'now' : mins < 60 ? `${mins}m` : mins < 1440 ? `${Math.floor(mins / 60)}h` : `${Math.floor(mins / 1440)}d`;
}
export function readableHours(hours: number | null | undefined): string | null {
  if (hours == null || !Number.isFinite(hours) || hours < 0) return null;
  return hours < 1 ? '<1h left' : `~${Math.round(hours)}h left`;
}
/** The entry that names a queued item's waiting sentence: working, then review, then queued, whatever the wire order. */
function readableExternalEntry(member: WorkLaneMember) {
  for (const state of ['working', 'review', 'queued'] as const) {
    const entry = member.external?.find(entry => entry.state === state);
    if (entry) return entry;
  }
  return undefined;
}
/** The only sentence shown on a small (Later or Done) row. */
export function readableQueuedLine(member: WorkLaneMember): string | null {
  if (member.status === 'blocked' || (member.open_questions?.count ?? 0) > 0) return null;
  const entry = readableExternalEntry(member);
  return entry?.state === 'queued' ? `Waiting for ${entry.label}` : null;
}
export function readableSentence(member: WorkLaneMember): string | null {
  if (member.status === 'blocked') return member.blocked_line ? `Blocked on ${member.blocked_line}` : null;
  if ((member.open_questions?.count ?? 0) > 0) return 'Waiting on your answer.';
  return readableQueuedLine(member) ?? (member.status_line || null);
}
export function readableSeatCount(member: WorkLaneMember): string | null {
  const seats = member.seats ?? [];
  const working = seats.filter(s => s.activity === 'working').length + (member.external?.filter(entry => entry.state === 'working').length ?? 0);
  return working ? `${working}${(member.seats_total ?? seats.length) > seats.length ? '+' : ''} on it now` : null;
}
export function readableTrouble(member: WorkLaneMember, now = Date.now()): string[] {
  const result: string[] = [];
  const active = member.worked?.active_h;
  if (active != null && member.estimate_first && active > member.estimate_first.p75)
    result.push(`${(active - member.estimate_first.median).toFixed(1)}h over estimate`);
  if ((member.worked?.qa_rounds ?? 0) >= 2) result.push(`${member.worked!.qa_rounds} review rounds`);
  const labels: Record<string, string> = { analysis: 'Planning', in_progress: 'Building', needs_qa: 'In review' };
  if (member.status && labels[member.status]) {
    const times = (member.transitions ?? []).map(t => Date.parse(t.at)).filter(Number.isFinite);
    const newest = times.length ? Math.max(...times) : NaN;
    const days = Math.floor((now - newest) / 86400000);
    if (days >= 2) result.push(`${labels[member.status]} ${days}d`);
  }
  return result;
}
