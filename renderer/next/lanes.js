'use strict';

const {
  derivedDefaultGoal,
  readableAge,
  readableDeprecated,
  readableDot,
  readableGoalClass,
  readableHours,
  readableItemKind,
  readableLaneClass,
  readableQueuedLine,
  readableRoad,
  readableSeatCount,
  readableSentence,
} = require('../../pentacle-chat-core/src/services/readableLanes.ts');

const SECTION_ORDER = [
  { kind: 'working', label: 'Working now', limit: 6 },
  { kind: 'needs', label: 'Needs you', limit: 6 },
  { kind: 'in_progress', label: 'In progress', limit: 6 },
  { kind: 'blocked', label: 'Blocked', limit: 6 },
  { kind: 'next', label: 'Next up', limit: 6 },
  { kind: 'later', label: 'Later', limit: 6 },
  { kind: 'done', label: 'Done', limit: 8 },
];

const STAGE_LABELS = {
  backlog: 'Backlog',
  analysis: 'Planning',
  ready_for_dev: 'Ready',
  in_progress: 'Building',
  needs_qa: 'In review',
  blocked: 'Blocked',
  completed: 'Done',
  done: 'Done',
};

const MEMBER_KIND_LABELS = {
  done: 'Done',
  needs: 'Needs your answer',
  working: 'Working',
  in_progress: 'In progress',
  blocked: 'Blocked',
  next: 'Queued next',
  later: 'Queued later',
};

const ICONS = {
  pentacle: [['path', 'M12 2.5l2.6 7.3h7.6l-6.1 4.5 2.3 7.4L12 17.2l-6.4 4.5 2.3-7.4-6.1-4.5h7.6z']],
  flask: [['path', 'M9 3h6M10 3v6l-5.5 9.5A2 2 0 0 0 6.2 21h11.6a2 2 0 0 0 1.7-2.5L14 9V3M7.5 15h9']],
  cube: [['path', 'M12 2.5l8.5 4.75v9.5L12 21.5l-8.5-4.75v-9.5zM12 12l8.5-4.75M12 12v9.5M12 12L3.5 7.25']],
  phone: [['rect', { x: 7, y: 2.5, width: 10, height: 19, rx: 2.2 }], ['path', 'M10.5 18.5h3']],
  bot: [['rect', { x: 4, y: 8, width: 16, height: 12, rx: 3 }], ['path', 'M12 8V4M9 4h6M8.5 14h.01M15.5 14h.01M9.5 17.5h5M1.5 13v3M22.5 13v3']],
  bug: [['path', 'M8.5 10.5a3.5 3.5 0 0 1 7 0v5a3.5 3.5 0 0 1-7 0zM3 13.5h5.5M15.5 13.5H21M5 8.5l3.5 2M19 8.5l-3.5 2M5 19.5l3.5-2.5M19 19.5l-3.5-2.5M10 7l-1.5-2.5M14 7l1.5-2.5']],
  truck: [['path', 'M1.5 6.5h12v10h-12zM13.5 10h4.5l3.5 3.5v3h-8z'], ['circle', { cx: 5.5, cy: 18, r: 1.8 }], ['circle', { cx: 17.5, cy: 18, r: 1.8 }]],
  wrench: [['path', 'M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z']],
  columns: [['path', 'M3 21.5h18M4 10h16M12 3l9 7H3zM6.5 10v11.5M12 10v11.5M17.5 10v11.5']],
  rocket: [['path', 'M12 2.5c3 2.5 4.5 6 4.5 10.5l-2 2.5h-5l-2-2.5C7.5 8.5 9 5 12 2.5zM9.5 13l-3 1.5-1 4 3.5-1.5M14.5 13l3 1.5 1 4-3.5-1.5M10.5 17.5l1.5 4 1.5-4'], ['circle', { cx: 12, cy: 9.5, r: 1.6 }]],
  hammer: [['path', 'M14 4l6 6-2.5 2.5-6-6zM11.5 6.5L3.5 14.5a1.6 1.6 0 0 0 0 2.2l1.8 1.8a1.6 1.6 0 0 0 2.2 0l8-8']],
  eye: [['path', 'M2 12s3.5-6.5 10-6.5S22 12 22 12s-3.5 6.5-10 6.5S2 12 2 12z'], ['circle', { cx: 12, cy: 12, r: 3 }]],
  camera: [['path', 'M3.5 8.5h3l1.5-2.5h8l1.5 2.5h3v11h-17z'], ['circle', { cx: 12, cy: 14, r: 3.2 }]],
  cursor: [['path', 'M5 3.5l14 8-6 1.5-3.5 5.5z']],
  home: [['path', 'M3.5 11.5L12 4l8.5 7.5M6 10v10.5h12V10M10 20.5v-5h4v5']],
  book: [['path', 'M3 4h6a4 4 0 0 1 3 1.5A4 4 0 0 1 15 4h6v16h-6a4 4 0 0 0-3 1.5A4 4 0 0 0 9 20H3zM12 5.5v16']],
  chart: [['path', 'M3 3v18h18M6 16l5-5 4 2 6-8']],
  shield: [['path', 'M12 2.5l9 4v6c0 5-9 9-9 9s-9-4-9-9v-6zM8 12l3 3 5-6']],
  key: [['circle', { cx: 8, cy: 8, r: 5 }], ['path', 'M11.5 11.5L21 21M16 16l3-3M18 18l3-3']],
  leaf: [['path', 'M21 3S4 1 3 12c-1 10 12 12 16 3 2-5 2-12 2-12zM5 19L17 7']],
  briefcase: [['path', 'M3 8h18v11H3zM9 8V5.5A1.5 1.5 0 0 1 10.5 4h3A1.5 1.5 0 0 1 15 5.5V8M3 13h18']],
  headset: [['path', 'M4 14v-2a8 8 0 0 1 16 0v2M4 14h3v5H5a1 1 0 0 1-1-1zM20 14h-3v5h2a1 1 0 0 1 1 1zM17 19c0 1.5-2 2.5-5 2.5']],
  film: [['path', 'M4 4h16v16H4zM8 4v16M16 4v16M4 9h4M4 15h4M16 9h4M16 15h4']],
  server: [['path', 'M4 4h16v6H4zM4 14h16v6H4zM7.5 7h.01M7.5 17h.01']],
};

function mapValue(map, key) {
  if (map instanceof Map) return map.get(key);
  return map && typeof map === 'object' ? map[key] : undefined;
}

function asArray(value) {
  return Array.isArray(value) ? value : [];
}

function inventoryLanes(inventory) {
  if (Array.isArray(inventory)) return inventory;
  return asArray(inventory && inventory.lanes);
}

function laneRead(reads, laneId, kind, goalId) {
  if (!reads || typeof reads.getRead !== 'function' || !laneId) return null;
  try { return reads.getRead(laneId, kind, goalId) || null; }
  catch (_) { return null; }
}

function readReply(read) {
  return read && read.reply && typeof read.reply === 'object' ? read.reply : null;
}

function detailGoals(reply) {
  return asArray(reply && (reply.projection && reply.projection.goals || reply.goals || reply.lane && reply.lane.goals));
}

function detailMembers(reply) {
  return asArray(reply && (reply.members || reply.projection && reply.projection.members));
}

function detailDefaultGoal(reply) {
  return reply && (reply.projection && reply.projection.default_goal || reply.lane && reply.lane.default_goal) || null;
}

function detailGoalClasses(reply) {
  const value = reply && (reply.projection && reply.projection.goal_classes || reply.goal_classes);
  return typeof value === 'string' ? value : null;
}

function capped(lane) {
  const sent = asArray(lane && lane.goals);
  return Number(lane && lane.goals_total || sent.length) > sent.length;
}

function goalLetter(lane, index) {
  return typeof lane.goal_classes === 'string' ? lane.goal_classes[index] : undefined;
}

function classLetter(goalClass) {
  return ({ done: 'd', working: 'w', moving: 'm', blocked: 'b', queued: 'q' })[goalClass] || 'q';
}

function completeGoalClasses(lane, goals, defaultGoal, reply) {
  const detailClasses = detailGoalClasses(reply);
  const sentGoals = asArray(lane && lane.goals);
  const letters = goals.map((goal, index) => {
    if (typeof lane.goal_road === 'string' && Number.isSafeInteger(goal.pos)
      && goal.pos >= 0 && goal.pos < lane.goal_road.length) return lane.goal_road[goal.pos];
    if (detailClasses && detailClasses.length >= goals.length) return detailClasses[index];
    const sentIndex = sentGoals.findIndex(sent => sent.goal_id === goal.goal_id);
    if (sentIndex >= 0) return goalLetter(lane, sentIndex) || classLetter(readableGoalClass(goal));
    return classLetter(readableGoalClass(goal));
  });
  const frameDefaultLetter = goalLetter(lane, sentGoals.length);
  const detailDefaultLetter = detailClasses && detailClasses.length > goals.length ? detailClasses[goals.length] : null;
  const defaultLetter = defaultGoal
    ? (detailDefaultLetter && detailDefaultLetter !== '-' ? detailDefaultLetter : null)
      || (frameDefaultLetter && frameDefaultLetter !== '-' ? frameDefaultLetter : null)
      || classLetter(readableGoalClass(defaultGoal))
    : '-';
  return `${letters.join('')}${defaultLetter}`;
}

function laneClass(lane) {
  // A normalized road can be null after validation. Do not let the helper's legacy
  // fallback pass that null to readableRoad; derive only the lane class from goals.
  if (lane && lane.goal_road === null && !lane.goal_classes && !lane.goal_counts && !lane.parked && lane.state !== 'paused' && lane.activity !== 'working') {
    return asArray(lane.goals).some(goal => readableGoalClass(goal) === 'moving') ? 'moving' : 'idle';
  }
  const counts = lane && lane.goal_counts;
  const helperLane = counts && !Array.isArray(counts) && typeof counts === 'object'
    ? { ...lane, goal_counts: [counts.moving, counts.blocked, counts.queued, counts.done] }
    : lane || {};
  return readableLaneClass(helperLane);
}

function questionsForLane(questions, laneId) {
  return asArray(mapValue(questions && questions.byLane, laneId));
}

function questionsForGoal(questions, laneId, goalId) {
  const byLane = mapValue(questions && questions.byGoal, laneId);
  return asArray(mapValue(byLane, goalId));
}

function questionsForSpec(questions, specId) {
  return asArray(mapValue(questions && questions.bySpec, specId));
}

function previewMembers(lane) {
  return asArray(lane && lane.progress && lane.progress.members);
}

function previewMembersComplete(lane) {
  const members = previewMembers(lane);
  const total = lane && lane.progress && lane.progress.members_total;
  return Number.isFinite(total) && total >= 0 && total <= members.length;
}

function hasPreviewQuestion(lane) {
  return previewMembers(lane).some(member => Number(member && member.open_questions && member.open_questions.count || 0) > 0);
}

function readForLane(reads, lane, kind, goalId) {
  return laneRead(reads, lane && lane.lane_id, kind, goalId);
}

function fullGoalsFor(lane, reads, goalId) {
  const candidates = [
    readReply(readForLane(reads, lane, 'members')),
    readReply(readForLane(reads, lane, 'goal', goalId)),
    readReply(readForLane(reads, lane, 'goals')),
  ];
  for (const reply of candidates) {
    const goals = detailGoals(reply);
    if (goals.length) return { goals, reply };
  }
  return { goals: asArray(lane && lane.goals), reply: null };
}

function fullMembersFor(lane, reads, goalId) {
  const candidates = [
    readReply(readForLane(reads, lane, 'goal', goalId)),
    readReply(readForLane(reads, lane, 'members')),
  ];
  for (const reply of candidates) {
    const members = detailMembers(reply);
    if (reply && (members.length || Number(reply.members_total) === 0)) return { members, reply };
  }
  return { members: previewMembers(lane), reply: null };
}

function defaultGoalFor(lane, detailReply, members, completeMembers) {
  return detailDefaultGoal(detailReply) || lane.default_goal || derivedDefaultGoal(lane, members, completeMembers) || null;
}

function orderStops(lane, goals, defaultGoal, reply) {
  const classes = completeGoalClasses(lane, goals, defaultGoal, reply);
  if (typeof lane.goal_road === 'string') {
    const roadLane = { ...lane, goals, goal_classes: classes, default_goal: defaultGoal || null };
    return readableRoad(roadLane, { defaultGoal: defaultGoal || undefined });
  }
  const stops = goals.map((goal, index) => ({
    goal,
    goalClass: readableGoalClass(goal, classes[index]),
    isDefault: false,
    number: null,
    position: null,
  }));
  if (defaultGoal && Number(defaultGoal.total) > 0) {
    const defaultClass = readableGoalClass(defaultGoal, classes[goals.length]);
    if (defaultClass !== 'done') {
      stops.push({
        goal: { ...defaultGoal, goal_id: null, title: lane.title, rank: Number.MAX_SAFE_INTEGER },
        goalClass: defaultClass,
        isDefault: true,
        number: null,
        position: null,
      });
    }
  }
  return stops;
}

function progressFor(goal, detailHeld) {
  if (!detailHeld || !goal || !Number.isFinite(Number(goal.done)) || !Number.isFinite(Number(goal.total))) return null;
  const total = Math.max(0, Number(goal.total));
  const done = Math.min(total, Math.max(0, Number(goal.done)));
  const blocked = Number.isFinite(Number(goal.blocked)) ? Math.min(Math.max(0, Number(goal.blocked)), total - done) : 0;
  return {
    done,
    total,
    blocked,
    donePercent: total ? done / total * 100 : 0,
    blockedPercent: total ? blocked / total * 100 : 0,
    text: `${done}/${total}`,
  };
}

function attentionRows(lane, goals, detailHeld, laneQuestions, questions, goalReply) {
  const cls = laneClass(lane);
  const ordered = orderStops(lane, goals, null, goalReply).filter(stop => !stop.isDefault && stop.goalClass !== 'done');
  const question = [];
  const blocked = [];
  const active = [];
  for (const stop of ordered) {
    const goal = stop.goal;
    const goalQuestions = questionsForGoal(questions, lane.lane_id, goal.goal_id);
    if (goalQuestions.length) {
      question.push({ goal, goalClass: stop.goalClass, reason: 'needs you' });
    } else if (stop.goalClass === 'blocked') {
      const count = detailHeld && Number(goal.blocked) > 0 ? Math.min(Number(goal.blocked), Math.max(0, Number(goal.total || 0) - Number(goal.done || 0))) : 0;
      blocked.push({ goal, goalClass: stop.goalClass, reason: count ? `${count} blocked` : 'blocked' });
    } else if ((cls === 'working' || cls === 'moving') && (stop.goalClass === 'working' || stop.goalClass === 'moving')) {
      active.push({ goal, goalClass: stop.goalClass, reason: null });
    }
  }
  const all = [...question, ...blocked, ...active];
  return {
    items: all.slice(0, 2),
    more: Math.max(0, all.length - 2),
    hasQuestion: laneQuestions.length > 0 || hasPreviewQuestion(lane),
  };
}

function readFailure(read) {
  return !!read && read.status === 'failed';
}

function rootRow(lane, reads, questions, now) {
  const cappedLane = capped(lane);
  const goalsRead = readForLane(reads, lane, 'goals');
  const membersRead = readForLane(reads, lane, 'members');
  const detail = fullGoalsFor(lane, reads);
  const hasFullGoals = !cappedLane || !!detail.reply;
  const laneQuestions = questionsForLane(questions, lane.lane_id);
  const attention = hasFullGoals
    ? attentionRows(lane, detail.goals, !!detail.reply, laneQuestions, questions, detail.reply)
    : { items: [], more: null, hasQuestion: laneQuestions.length > 0 || hasPreviewQuestion(lane) };
  const readsForGoals = [goalsRead, membersRead].filter(Boolean);
  const read = readsForGoals.find(item => item.status === 'failed')
    || readsForGoals.find(item => readReply(item))
    || readsForGoals[0]
    || null;
  return {
    lane,
    laneClass: laneClass(lane),
    age: readableAge(laneClass(lane) === 'paused' ? lane.state_at : lane.progress_at, now),
    question: attention.hasQuestion,
    attention: attention.items.map(item => ({ title: item.goal.title, reason: item.reason, goalId: item.goal.goal_id })),
    more: attention.more,
    retry: cappedLane && readFailure(read),
    capped: cappedLane,
    goalsHeld: hasFullGoals,
  };
}

function memberSections(members, expanded, keyPrefix, questions = {}) {
  const visible = asArray(members).filter(member => !readableDeprecated(member));
  const classified = visible.map((member, index) => ({ member, kind: readableItemKind(member), index }));
  return SECTION_ORDER.flatMap(section => {
    const rows = classified.filter(row => row.kind === section.kind).map(({ member }) => ({
      member,
      title: member.title || null,
      kind: section.kind,
      question: Number(member.open_questions && member.open_questions.count || 0) > 0 || questionsForSpec(questions, member.spec_id).length > 0,
      sentence: section.kind === 'later' || section.kind === 'done' ? readableQueuedLine(member) : readableSentence(member),
      compact: section.kind === 'later' || section.kind === 'done',
    }));
    if (!rows.length) return [];
    const key = `${keyPrefix}:${section.kind}`;
    const isOpen = expanded && expanded.has(key);
    const showAll = expanded && expanded.has(`${key}:all`);
    const shownRows = section.kind === 'done'
      ? (showAll ? rows : isOpen ? rows.slice(0, section.limit) : [])
      : (isOpen ? rows : rows.slice(0, section.limit));
    return [{
      kind: section.kind,
      label: section.label,
      rows: shownRows,
      count: rows.length,
      more: Math.max(0, rows.length - shownRows.length),
      expanded: !!isOpen,
      showAll: !!showAll,
      key,
      limit: section.limit,
    }];
  });
}

function answeredValue(questions, specId) {
  return mapValue(questions && questions.youSaidBySpec, specId) || null;
}

function memberForSpec(lane, reads, route) {
  const details = fullMembersFor(lane, reads, route.goalId);
  const member = details.members.find(row => row.spec_id === route.specId)
    || previewMembers(lane).find(row => row.spec_id === route.specId)
    || null;
  return { member, reply: details.reply, detailHeld: !!details.reply };
}

function laneDetailView(lane, reads, questions, now, expanded) {
  const membersRead = readForLane(reads, lane, 'members');
  const detail = fullMembersFor(lane, reads);
  const detailHeld = !!detail.reply;
  const membersComplete = detailHeld || previewMembersComplete(lane);
  // A goals-only reply can drive the root attention row, but the open lane view
  // waits for its members read before replacing the inventory preview.
  const goals = detail.reply ? detailGoals(detail.reply) : asArray(lane.goals);
  const defaultGoal = defaultGoalFor(lane, detail.reply, detail.members, membersComplete);
  const stops = orderStops(lane, goals, defaultGoal, detail.reply).map(stop => {
    const isDefault = !!stop.isDefault;
    const goalId = isDefault ? null : stop.goal.goal_id;
    const group = isDefault ? questionsForLane(questions, lane.lane_id).filter(item => item.placement && item.placement.kind === 'lane')
      : questionsForGoal(questions, lane.lane_id, goalId);
    const goalMembers = isDefault ? detail.members.filter(member => !member.goal_id)
      : detail.members.filter(member => member.goal_id === goalId);
    return {
      goal: stop.goal,
      goalId,
      isDefault,
      goalClass: stop.goalClass,
      position: typeof lane.goal_road === 'string' ? stop.position : null,
      number: typeof lane.goal_road === 'string' ? stop.number : null,
      question: group.length > 0 || (isDefault
        ? previewMembers(lane).some(member => !member.goal_id && Number(member.open_questions && member.open_questions.count || 0) > 0)
        : previewMembers(lane).some(member => member.goal_id === goalId && Number(member.open_questions && member.open_questions.count || 0) > 0)),
      progress: progressFor(stop.goal, detailHeld),
      blockedCount: detailHeld && Number(stop.goal.blocked) > 0 ? Math.min(Number(stop.goal.blocked), Math.max(0, Number(stop.goal.total || 0) - Number(stop.goal.done || 0))) : 0,
      members: goalMembers,
      sections: isDefault && membersComplete
        ? memberSections(goalMembers, expanded, `goal:${lane.lane_id}:default`, questions)
        : [],
    };
  });
  const sentCount = asArray(lane.goals).length;
  const isCapped = capped(lane);
  const unknownMore = isCapped && !detail.reply ? Math.max(0, Number(lane.goals_total || sentCount) - sentCount) : 0;
  const defaultStop = stops.find(stop => stop.isDefault);
  const looseDone = membersComplete && !defaultStop ? detail.members.filter(member => !member.goal_id && (member.status === 'completed' || member.terminal === 'completed') && !readableDeprecated(member)) : [];
  return {
    level: 'lane',
    lane,
    laneClass: laneClass(lane),
    stateLine: laneClass(lane) === 'parked'
      ? (lane.parked && lane.parked.reason ? `Parked · ${lane.parked.reason}` : 'Parked')
      : laneClass(lane) === 'paused'
        ? `Paused${readableAge(lane.state_at, now) ? ` · ${readableAge(lane.state_at, now)}` : ''}`
        : readableAge(lane.progress_at, now),
    questions: questionsForLane(questions, lane.lane_id),
    stops,
    more: unknownMore,
    retry: readFailure(membersRead),
    detailHeld,
    finishedEarlierCount: looseDone.length,
    finishedEarlierMembers: looseDone,
    finishedEarlierOpen: false,
    readStatus: membersRead && membersRead.status || 'idle',
  };
}

function goalDetailView(lane, route, reads, questions, expanded, now) {
  const goalId = route.goalId == null ? null : route.goalId;
  const goalRead = goalId ? readForLane(reads, lane.lane_id, 'goal', goalId) : readForLane(reads, lane.lane_id, 'members');
  const goalReply = readReply(goalRead);
  const full = fullGoalsFor(lane, reads, goalId);
  const members = fullMembersFor(lane, reads, goalId);
  const defaultGoal = defaultGoalFor(lane, members.reply || goalReply || full.reply, members.members, !!members.reply);
  const selected = goalReply && goalReply.projection && goalReply.projection.selected_goal;
  const goal = goalId
    ? selected || full.goals.find(item => item.goal_id === goalId) || null
    : defaultGoal;
  const goalTitle = goal && goal.title || route.goalTitle || (goalId ? null : lane.title);
  const selectedMembers = members.reply
    ? members.members.filter(member => goalId ? member.goal_id === goalId : !member.goal_id)
    : [];
  const sections = members.reply ? memberSections(selectedMembers, expanded, `goal:${lane.lane_id}:${goalId || 'default'}`, questions) : [];
  return {
    level: 'goal',
    lane,
    goal,
    goalId,
    isDefault: goalId == null,
    title: goalTitle,
    goalClass: goal ? readableGoalClass(goal, goalLetter(lane, asArray(lane.goals).findIndex(item => item.goal_id === goalId))) : null,
    progress: progressFor(goal, !!(goalReply || members.reply)),
    hours: goal ? readableHours(goal.left_h) : null,
    questions: goalId ? questionsForGoal(questions, lane.lane_id, goalId)
      : questionsForLane(questions, lane.lane_id).filter(item => item.placement && item.placement.kind === 'lane'),
    sections,
    retry: readFailure(goalRead),
    readStatus: goalRead && goalRead.status || 'idle',
    now,
  };
}

function workedHours(member) {
  const hours = member && member.worked && member.worked.active_h;
  if (!Number.isFinite(hours) || hours < 0) return null;
  return `${hours < 10 && !Number.isInteger(hours) ? hours.toFixed(1) : Math.round(hours)}h worked`;
}

function specDetailView(lane, route, reads, questions, now) {
  const { member, reply, detailHeld } = memberForSpec(lane, reads, route);
  const detailRead = route.goalId ? readForLane(reads, lane.lane_id, 'goal', route.goalId) : readForLane(reads, lane.lane_id, 'members');
  const questionItems = questionsForSpec(questions, route.specId);
  const youSaid = answeredValue(questions, route.specId);
  const status = member && (member.status || (member.terminal === 'completed' ? 'completed' : null));
  const stage = questionItems.length ? 'Needs you' : STAGE_LABELS[status] || null;
  const age = member ? readableAge(member.progress_at, now) : null;
  const seatCount = member ? readableSeatCount(member) : null;
  const activeHours = workedHours(member);
  const meta = [age, activeHours, seatCount].filter(Boolean);
  const checks = member && Number.isFinite(member.ac_checked) && Number.isFinite(member.ac_total)
    ? { text: `${member.ac_checked}/${member.ac_total} checks`, done: member.ac_checked, total: member.ac_total,
      donePercent: member.ac_total > 0 ? Math.min(100, Math.max(0, member.ac_checked / member.ac_total * 100)) : 0,
      estimate: readableHours(member.estimate && member.estimate.median) }
    : null;
  const criteria = asArray(member && member.criteria);
  const recent = asArray(member && member.recent).map(item => ({
    text: item.text || (item.kind === 'checks' && Number.isFinite(item.count) ? `${item.count} checks` : item.kind === 'done' ? 'Done' : item.kind === 'blocked' ? 'Blocked' : null),
    age: readableAge(item.at, now),
  })).filter(item => item.text);
  return {
    level: 'spec',
    lane,
    specId: route.specId,
    member,
    title: member && member.title || route.specTitle || null,
    stage,
    meta,
    summary: member && member.summary_line || null,
    checks,
    now: member && member.status_line || null,
    youSaid,
    blocked: !youSaid && member && member.blocked_line || null,
    next: member && member.next_line || null,
    shortLines: !detailHeld && member ? [member.status_line, member.next_line].filter(Boolean) : [],
    criteriaLeft: criteria.filter(item => item.disposition === 'open'),
    criteriaPassed: criteria.filter(item => item.disposition !== 'open'),
    recent,
    questions: questionItems,
    routeGoalId: route.goalId || null,
    routeGoalTitle: route.goalTitle || (route.goalId && asArray(lane.goals).find(goal => goal.goal_id === route.goalId)?.title) || null,
    retry: readFailure(detailRead),
    readStatus: detailRead && detailRead.status || 'idle',
  };
}

function buildLanesViewModel({ inventory, reads, questions = {}, route = { level: 'all' }, now = Date.now(), expanded = new Set() } = {}) {
  const lanes = inventoryLanes(inventory);
  const currentRoute = route && route.level ? route : { level: 'all' };
  if (currentRoute.level === 'all' || !currentRoute.laneId) {
    const rows = lanes.map(lane => rootRow(lane, reads, questions, now));
    const active = rows.filter(row => row.laneClass !== 'parked');
    const parked = rows.filter(row => row.laneClass === 'parked');
    const more = inventory && inventory.truncated
      ? Math.max(0, Number(inventory.counts && inventory.counts.open || 0) - lanes.length)
      : 0;
    return { level: 'all', active, parked, truncatedMore: more > 0 ? more : null };
  }
  const lane = lanes.find(item => item.lane_id === currentRoute.laneId);
  if (!lane) return { level: 'all', active: [], parked: [], truncatedMore: null };
  if (currentRoute.level === 'lane') return laneDetailView(lane, reads, questions, now, expanded);
  if (currentRoute.level === 'goal') return goalDetailView(lane, currentRoute, reads, questions, expanded, now);
  if (currentRoute.level === 'spec') return specDetailView(lane, currentRoute, reads, questions, now);
  return { level: 'all', active: [], parked: [], truncatedMore: null };
}

function el(doc, tag, className, parent) {
  const element = doc.createElement(tag);
  if (className) element.className = className;
  if (parent) parent.appendChild(element);
  return element;
}

function button(doc, label, text, className, parent, action) {
  const element = el(doc, 'button', className, parent);
  element.type = 'button';
  element.setAttribute('aria-label', label);
  element.title = label;
  if (action) element.dataset.lanesAction = action;
  if (text != null) element.textContent = text;
  return element;
}

function iconNode(doc, icon) {
  const rows = ICONS[icon] || [];
  if (!rows.length) return null;
  const svg = doc.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '1.7');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  for (const [type, attrs] of rows) {
    const child = doc.createElementNS('http://www.w3.org/2000/svg', type);
    if (typeof attrs === 'string') child.setAttribute('d', attrs);
    else for (const [key, value] of Object.entries(attrs)) child.setAttribute(key, String(value));
    svg.appendChild(child);
  }
  return svg;
}

function renderLaneIcon(lane, doc = globalThis.document) {
  if (!doc) throw new TypeError('renderLaneIcon needs a document');
  const key = lane && lane.icon && ICONS[lane.icon] ? lane.icon : null;
  const ring = el(doc, 'span', 'next-lane-icon-ring');
  ring.dataset.icon = key || 'monogram';
  ring.setAttribute('aria-hidden', 'true');
  const icon = key ? iconNode(doc, key) : null;
  if (icon) ring.appendChild(icon);
  else ring.textContent = String(lane && lane.title || '').trim().slice(0, 1).toUpperCase();
  return ring;
}

function registerUnmount(registerUnmount, result) {
  if (typeof result === 'function') registerUnmount(result);
  else if (result && typeof result.destroy === 'function') registerUnmount(() => result.destroy());
}

function appendQuestionCards(doc, parent, questions, args, addUnmount) {
  if (!questions || typeof questions.mountCards !== 'function') return;
  const host = el(doc, 'div', 'next-lanes-question-cards', parent);
  registerUnmount(addUnmount, questions.mountCards(host, args));
}

function appendProgress(doc, parent, progress) {
  if (!progress) return;
  const bar = el(doc, 'div', 'next-lanes-progress');
  bar.setAttribute('role', 'img');
  bar.setAttribute('aria-label', `${progress.done} of ${progress.total} done${progress.blocked ? `, ${progress.blocked} blocked` : ''}`);
  const done = el(doc, 'span', 'next-lanes-progress-done', bar);
  done.style.width = `${progress.donePercent}%`;
  const blocked = el(doc, 'span', 'next-lanes-progress-blocked', bar);
  blocked.style.width = `${progress.blockedPercent}%`;
  blocked.style.left = `${progress.donePercent}%`;
  parent.appendChild(bar);
}

function appendMemberSections(doc, parent, sections, location = {}) {
  for (const section of sections || []) {
    const wrapper = el(doc, 'section', `next-lanes-section next-lanes-section-${section.kind}`, parent);
    if (section.kind === 'done') {
      const label = `Done · ${section.count} ${section.expanded ? '⌄' : '›'}`;
      const toggle = button(doc, label, label, 'next-lanes-section-toggle', wrapper, 'toggle-section');
      toggle.dataset.sectionKey = section.key;
    } else {
      const title = el(doc, 'h3', 'next-lanes-section-title', wrapper);
      title.textContent = section.label;
    }
    if (section.kind !== 'done' || section.expanded) {
      const list = el(doc, 'div', 'next-lanes-member-list', wrapper);
      for (const row of section.rows) {
        const item = el(doc, 'article', `next-lanes-member next-lanes-member-${row.kind}${row.compact ? ' compact' : ''}`, list);
        const mark = el(doc, 'span', `next-lanes-member-mark next-lanes-member-mark-${row.kind}`, item);
        mark.setAttribute('role', 'img');
        mark.setAttribute('aria-label', MEMBER_KIND_LABELS[row.kind] || 'Work status');
        if (row.question && row.kind !== 'needs') { const questionMark = el(doc, 'span', 'next-lanes-question-mark', item); questionMark.textContent = '?'; questionMark.setAttribute('aria-label', 'Needs your answer'); }
        if (row.title) {
          const title = button(doc, row.title, row.title, 'next-lanes-member-title', item, 'open-spec');
          title.dataset.laneId = location.laneId || '';
          title.dataset.specId = row.member.spec_id;
          title.dataset.specTitle = row.title;
          if (location.goalId) title.dataset.goalId = location.goalId;
          if (location.goalTitle) title.dataset.goalTitle = location.goalTitle;
        }
        if (row.sentence) { const line = el(doc, 'div', 'next-lanes-member-line', item); line.textContent = row.sentence; }
      }
      if (section.more) {
        const show = button(doc, `Show ${section.more} more`, `Show ${section.more} more`, 'next-lanes-show-more', wrapper, 'show-more');
        show.dataset.sectionKey = section.key;
        show.dataset.sectionKind = section.kind;
      }
    }
  }
}

function appendRetry(doc, parent, retry, laneId, kind, goalId) {
  if (!retry) return;
  const retryButton = button(doc, 'Retry', 'Retry', 'next-lanes-retry', parent, 'retry');
  retryButton.dataset.laneId = laneId;
  retryButton.dataset.readKind = kind;
  if (goalId) retryButton.dataset.goalId = goalId;
}

function appendLaneRow(doc, parent, row) {
  const wrap = el(doc, 'div', `next-lanes-row next-lanes-row-${row.laneClass}`, parent);
  if (row.capped && !row.goalsHeld) {
    wrap.dataset.goalsReadNeeded = 'true';
    wrap.dataset.laneId = row.lane.lane_id;
  }
  const open = button(doc, row.lane.title, null, 'next-lanes-row-open', wrap, 'open-lane');
  open.dataset.laneId = row.lane.lane_id;
  open.appendChild(renderLaneIcon(row.lane, doc));
  const copy = el(doc, 'span', 'next-lanes-row-copy', open);
  const titleLine = el(doc, 'span', 'next-lanes-row-title-line', copy);
  const title = el(doc, 'span', 'next-lanes-row-title', titleLine); title.textContent = row.lane.title;
  if (row.question) { const mark = el(doc, 'span', 'next-lanes-question-mark', titleLine); mark.textContent = '?'; mark.setAttribute('aria-label', 'Needs your answer'); }
  if (row.age) { const age = el(doc, 'span', 'next-lanes-age', titleLine); age.textContent = row.age; }
  for (const item of row.attention) {
    const reason = typeof item.reason === 'string' ? item.reason : '';
    const tone = reason === 'needs you' ? 'question' : reason === 'blocked' || reason.endsWith(' blocked') ? 'blocked' : 'active';
    const line = el(doc, 'span', `next-lanes-attention next-lanes-attention-${tone}`, copy);
    const goalTitle = el(doc, 'span', 'next-lanes-attention-goal', line); goalTitle.textContent = item.title;
    if (item.reason) { const reason = el(doc, 'span', 'next-lanes-attention-reason', line); reason.textContent = item.reason; }
  }
  if (row.more) { const more = el(doc, 'span', 'next-lanes-more', copy); more.textContent = `+${row.more} more`; }
  appendRetry(doc, wrap, row.retry, row.lane.lane_id, 'goals');
}

function appendStop(doc, parent, stop, laneId) {
  const dot = readableDot(stop);
  const position = stop.position ? ` next-lanes-stop-position-${stop.position}` : '';
  const wrapper = el(doc, 'div', `next-lanes-stop next-lanes-stop-${stop.goalClass} next-lanes-stop-dot-${dot}${position}${stop.isDefault ? ' default' : ''}`, parent);
  const open = button(doc, stop.goal.title, null, 'next-lanes-stop-open', wrapper, 'open-goal');
  open.dataset.laneId = laneId;
  if (stop.goalId) open.dataset.goalId = stop.goalId;
  open.dataset.goalTitle = stop.goal.title;
  if (stop.isDefault) open.dataset.defaultGoal = 'true';
  if (stop.number != null) { const number = el(doc, 'span', 'next-lanes-stop-number', open); number.textContent = String(stop.number); }
  const title = el(doc, 'span', 'next-lanes-stop-title', open); title.textContent = stop.goal.title;
  if (stop.question) { const mark = el(doc, 'span', 'next-lanes-question-mark', open); mark.textContent = '?'; mark.setAttribute('aria-label', 'Needs your answer'); }
  if (stop.progress) {
    appendProgress(doc, wrapper, stop.progress);
    const counts = el(doc, 'span', 'next-lanes-stop-counts', wrapper); counts.textContent = stop.progress.text;
    if (stop.blockedCount) { const blocked = el(doc, 'span', 'next-lanes-stop-blocked', wrapper); blocked.textContent = `${stop.blockedCount} blocked`; }
  }
  if (stop.isDefault && stop.sections.length) {
    const members = el(doc, 'div', 'next-lanes-default-members', wrapper);
    appendMemberSections(doc, members, stop.sections);
  }
}

function appendSpecFacts(doc, body, model, questions, addUnmount) {
  if (model.now) {
    const fact = el(doc, 'div', 'next-lanes-fact', body);
    const label = el(doc, 'span', 'next-lanes-fact-label', fact); label.textContent = 'Now';
    const value = el(doc, 'span', 'next-lanes-fact-value', fact); value.textContent = model.now;
  }
  if (questions && typeof questions.mountYouSaid === 'function') {
    if (!model.youSaid && model.blocked) {
      const fact = el(doc, 'div', 'next-lanes-fact next-lanes-fact-attention', body);
      const label = el(doc, 'span', 'next-lanes-fact-label', fact); label.textContent = 'Blocked on';
      const value = el(doc, 'span', 'next-lanes-fact-value', fact); value.textContent = model.blocked;
    }
    const host = el(doc, 'div', 'next-lanes-you-said', body);
    registerUnmount(addUnmount, questions.mountYouSaid(host, model.specId));
  } else if (model.youSaid) {
    const fact = el(doc, 'div', 'next-lanes-fact', body);
    const label = el(doc, 'span', 'next-lanes-fact-label', fact); label.textContent = 'You said';
    const value = el(doc, 'span', 'next-lanes-fact-value', fact);
    value.textContent = typeof model.youSaid === 'string' ? model.youSaid : model.youSaid.text || '';
  } else if (model.blocked) {
    const fact = el(doc, 'div', 'next-lanes-fact next-lanes-fact-attention', body);
    const label = el(doc, 'span', 'next-lanes-fact-label', fact); label.textContent = 'Blocked on';
    const value = el(doc, 'span', 'next-lanes-fact-value', fact); value.textContent = model.blocked;
  }
  if (model.next) {
    const fact = el(doc, 'div', 'next-lanes-fact', body);
    const label = el(doc, 'span', 'next-lanes-fact-label', fact); label.textContent = 'Next';
    const value = el(doc, 'span', 'next-lanes-fact-value', fact); value.textContent = model.next;
  }
}

function drawModel(root, model, { doc, questions, ui, scrollTop, addUnmount }) {
  root.replaceChildren();
  const head = el(doc, 'header', 'next-lanes-head', root);
  if (model.level !== 'all') {
    const backLabel = model.level === 'lane' ? 'All lanes'
      : model.level === 'goal' ? model.lane.title
        : (model.routeGoalTitle || model.lane.title);
    button(doc, backLabel, `‹  ${backLabel}`, 'next-lanes-back', head, 'back');
  }
  button(doc, 'Close', '×', 'next-lanes-close', head, 'close');
  const body = el(doc, 'div', 'next-lanes-scroll', root);
  body.dataset.level = model.level;

  if (model.level === 'all') {
    const list = el(doc, 'div', 'next-lanes-list', body);
    for (const row of model.active) appendLaneRow(doc, list, row);
    if (model.parked.length) {
      const fold = button(doc, `Parked · ${model.parked.length}`, `Parked · ${model.parked.length} ${ui.parkedOpen ? '⌄' : '›'}`, 'next-lanes-parked-toggle', body, 'toggle-parked');
      fold.setAttribute('aria-expanded', String(ui.parkedOpen));
      if (ui.parkedOpen) {
        const parked = el(doc, 'div', 'next-lanes-parked-list', body);
        for (const row of model.parked) appendLaneRow(doc, parked, row);
      }
    }
    if (model.truncatedMore) { const more = el(doc, 'p', 'next-lanes-truncated', body); more.textContent = `+${model.truncatedMore} more lanes`; }
  } else if (model.level === 'lane') {
    const title = el(doc, 'div', 'next-lanes-detail-title', body);
    title.appendChild(renderLaneIcon(model.lane, doc));
    const text = el(doc, 'div', 'next-lanes-detail-title-copy', title);
    const name = el(doc, 'h2', 'next-lanes-lane-title', text); name.textContent = model.lane.title;
    if (model.stateLine) { const state = el(doc, 'div', 'next-lanes-state', text); state.textContent = model.stateLine; }
    if (model.questions.length) appendQuestionCards(doc, body, questions, { level: 'lane', laneId: model.lane.lane_id }, addUnmount);
    const stops = el(doc, 'div', 'next-lanes-stops', body);
    for (const stop of model.stops) appendStop(doc, stops, stop, model.lane.lane_id);
    if (model.more) { const more = el(doc, 'p', 'next-lanes-more-stops', body); more.textContent = `+${model.more} more`; }
    appendRetry(doc, body, model.retry, model.lane.lane_id, 'members');
    if (model.finishedEarlierCount) {
      const fold = button(doc, `Finished earlier · ${model.finishedEarlierCount} ›`, `Finished earlier · ${model.finishedEarlierCount} ${ui.finishedEarlierOpen ? '⌄' : '›'}`, 'next-lanes-finished-toggle', body, 'toggle-finished');
      fold.setAttribute('aria-expanded', String(ui.finishedEarlierOpen));
      if (ui.finishedEarlierOpen) appendMemberSections(doc, body, memberSections(model.finishedEarlierMembers || [], ui.expandedSections, `finished:${model.lane.lane_id}`), { laneId: model.lane.lane_id });
    }
  } else if (model.level === 'goal') {
    const title = el(doc, 'h2', 'next-lanes-goal-title', body);
    if (model.title) title.textContent = model.title;
    const location = { level: model.isDefault ? 'lane' : 'goal', laneId: model.lane.lane_id };
    if (model.goalId) location.goalId = model.goalId;
    if (model.isDefault) location.noGoal = true;
    if (model.questions.length) appendQuestionCards(doc, body, questions, location, addUnmount);
    if (model.progress) {
      const bar = el(doc, 'div', 'next-lanes-goal-progress', body);
      appendProgress(doc, bar, model.progress);
      const counts = el(doc, 'span', 'next-lanes-goal-counts', bar); counts.textContent = `${model.progress.text} done`;
      if (model.hours) { const hours = el(doc, 'span', 'next-lanes-goal-hours', bar); hours.textContent = model.hours; }
    }
    appendMemberSections(doc, body, model.sections, { laneId: model.lane.lane_id, goalId: model.goalId, goalTitle: model.title });
    appendRetry(doc, body, model.retry, model.lane.lane_id, model.isDefault ? 'members' : 'goal', model.goalId);
  } else if (model.level === 'spec') {
    if (model.stage) { const chip = el(doc, 'span', `next-lanes-stage${model.stage === 'Needs you' ? ' attention' : ''}`, body); chip.textContent = model.stage; }
    if (model.meta.length) { const meta = el(doc, 'div', 'next-lanes-meta', body); meta.textContent = model.meta.join(' · '); }
    if (model.title) { const title = el(doc, 'h2', 'next-lanes-spec-title', body); title.textContent = model.title; }
    if (model.questions.length) appendQuestionCards(doc, body, questions, { level: 'spec', specId: model.specId }, addUnmount);
    if (model.summary) { const summary = el(doc, 'p', 'next-lanes-summary', body); summary.textContent = model.summary; }
    if (model.checks) {
      const checks = el(doc, 'div', 'next-lanes-checks', body);
      const label = el(doc, 'span', 'next-lanes-checks-label', checks); label.textContent = model.checks.text;
      const bar = el(doc, 'div', 'next-lanes-checks-progress', checks);
      bar.setAttribute('role', 'img');
      bar.setAttribute('aria-label', `${model.checks.done} of ${model.checks.total} checks complete`);
      const done = el(doc, 'span', 'next-lanes-checks-progress-done', bar);
      done.style.width = `${model.checks.donePercent}%`;
      if (model.checks.estimate) { const estimate = el(doc, 'span', 'next-lanes-checks-estimate', checks); estimate.textContent = model.checks.estimate; }
    }
    appendSpecFacts(doc, body, model, questions, addUnmount);
    if (model.shortLines.length) {
      const preview = el(doc, 'div', 'next-lanes-preview-lines', body);
      for (const lineText of model.shortLines) { const line = el(doc, 'p', 'next-lanes-preview-line', preview); line.textContent = lineText; }
    }
    if (model.criteriaLeft.length) {
      const section = el(doc, 'section', 'next-lanes-criteria-left', body);
      const heading = el(doc, 'h3', '', section); heading.textContent = 'Checks left';
      const list = el(doc, 'ul', '', section);
      for (const item of model.criteriaLeft) { const row = el(doc, 'li', '', list); row.textContent = item.title; }
    }
    if (model.criteriaPassed.length) {
      const key = `criteria:${model.specId}`;
      const buttonLabel = `Show ${model.criteriaPassed.length} passed`;
      const toggle = button(doc, buttonLabel, `${buttonLabel} ${ui.expandedSections.has(key) ? '⌄' : '›'}`, 'next-lanes-passed-toggle', body, 'toggle-section');
      toggle.dataset.sectionKey = key;
      if (ui.expandedSections.has(key)) {
        const list = el(doc, 'ul', 'next-lanes-criteria-passed', body);
        for (const item of model.criteriaPassed) { const row = el(doc, 'li', '', list); row.textContent = `${item.title} · ${item.disposition}`; }
      }
    }
    if (model.recent.length) {
      const section = el(doc, 'section', 'next-lanes-recent', body);
      const heading = el(doc, 'h3', '', section); heading.textContent = 'Recent';
      for (const item of model.recent) {
        const row = el(doc, 'div', 'next-lanes-recent-row', section);
        const text = el(doc, 'span', 'next-lanes-recent-text', row); text.textContent = item.text;
        if (item.age) { const age = el(doc, 'span', 'next-lanes-age', row); age.textContent = item.age; }
      }
    }
    appendRetry(doc, body, model.retry, model.lane.lane_id, model.routeGoalId ? 'goal' : 'members', model.routeGoalId);
  }
  if (Number.isFinite(scrollTop)) body.scrollTop = scrollTop;
  return body;
}

function routeKey(route) {
  return `${route.level || 'all'}:${route.laneId || ''}:${route.goalId || ''}:${route.specId || ''}`;
}

function mountLanesCard(root, { getState = () => ({}), reads = {}, questions = {}, navigation = {}, now = () => Date.now() } = {}) {
  if (!root || typeof root.replaceChildren !== 'function') throw new TypeError('mountLanesCard needs a root element');
  const doc = root.ownerDocument || globalThis.document;
  if (!doc) throw new TypeError('mountLanesCard needs a document');
  const ui = { route: { level: 'all' }, parkedOpen: false, finishedEarlierOpen: false, expandedSections: new Set() };
  const scrollPositions = new Map();
  const requested = new Map();
  let activeUnmounts = [];
  let currentBody = null;
  let observer = null;
  let destroyed = false;
  let unsubscribe = null;

  function snapshot() {
    const state = typeof getState === 'function' ? getState() : getState;
    const inventory = state && (state.inventory || state.workLanes || state.work_lanes) || state || {};
    const questionSnapshot = questions && typeof questions.getSnapshot === 'function' ? questions.getSnapshot() || {} : questions && questions.snapshot || {};
    return { inventory, questionSnapshot };
  }

  function currentRead(laneId, kind, goalId) {
    if (!reads || typeof reads.getRead !== 'function') return null;
    try { return reads.getRead(laneId, kind, goalId) || null; } catch (_) { return null; }
  }

  function addUnmount(cleanup) {
    if (typeof cleanup === 'function') activeUnmounts.push(cleanup);
  }

  function unmountQuestionHosts() {
    const cleanups = activeUnmounts;
    activeUnmounts = [];
    for (const cleanup of cleanups.reverse()) {
      try { cleanup(); } catch (_) { /* Detached question hosts must not prevent the lanes view from rendering. */ }
    }
  }

  function ensureRead(lane, kind, goalId) {
    if (!lane || !lane.lane_id) return;
    const key = `${lane.lane_id}\u0000${kind}\u0000${goalId || ''}`;
    const read = currentRead(lane.lane_id, kind, goalId);
    if (read && read.status !== 'idle') return;
    const revision = lane.detail_revision || '';
    if (requested.get(key) === revision) return;
    requested.set(key, revision);
    try {
      if (kind === 'goals' && typeof reads.requestGoals === 'function') reads.requestGoals(lane.lane_id);
      else if (kind === 'members' && typeof reads.requestMembers === 'function') reads.requestMembers(lane.lane_id);
      else if (kind === 'goal' && typeof reads.requestGoal === 'function') reads.requestGoal(lane.lane_id, goalId);
    } catch (_) { /* The controller reports failures through getRead. */ }
  }

  function requiredRead(view) {
    if (view.level === 'lane') ensureRead(view.lane, 'members');
    if (view.level === 'goal') ensureRead(view.lane, view.goalId ? 'goal' : 'members', view.goalId);
    if (view.level === 'spec') ensureRead(view.lane, view.routeGoalId ? 'goal' : 'members', view.routeGoalId);
  }

  function handleClick(event) {
    const target = event.target && event.target.closest ? event.target.closest('[data-lanes-action]') : null;
    if (!target || !root.contains(target)) return;
    const action = target.dataset.lanesAction;
    if (action === 'open-lane') openLane(target.dataset.laneId);
    else if (action === 'open-goal') openGoal(target.dataset.laneId, target.dataset.goalId || null, target.dataset.goalTitle || null);
    else if (action === 'open-spec') openSpec({
      laneId: target.dataset.laneId,
      goalId: target.dataset.goalId || null,
      goalTitle: target.dataset.goalTitle || null,
      specId: target.dataset.specId,
      specTitle: target.dataset.specTitle || null,
    });
    else if (action === 'back') back();
    else if (action === 'close') close();
    else if (action === 'toggle-parked') { ui.parkedOpen = !ui.parkedOpen; render(); }
    else if (action === 'toggle-finished') { ui.finishedEarlierOpen = !ui.finishedEarlierOpen; render(); }
    else if (action === 'toggle-section') {
      const key = target.dataset.sectionKey;
      if (ui.expandedSections.has(key)) { ui.expandedSections.delete(key); ui.expandedSections.delete(`${key}:all`); }
      else ui.expandedSections.add(key);
      render();
    } else if (action === 'show-more') {
      const key = target.dataset.sectionKey;
      if (target.dataset.sectionKind === 'done') ui.expandedSections.add(`${key}:all`);
      else ui.expandedSections.add(key);
      render();
    } else if (action === 'retry') {
      try { reads.retry && reads.retry(target.dataset.laneId, target.dataset.readKind, target.dataset.goalId || undefined); } catch (_) { /* The failed state remains visible. */ }
    }
  }

  function readRoute(view) {
    return { level: view.level, laneId: view.lane && view.lane.lane_id, goalId: view.goalId, specId: view.specId };
  }

  function findGoalTitle(laneId, goalId, specId) {
    if (!goalId) return null;
    const { inventory, questionSnapshot } = snapshot();
    const placedQuestion = asArray(mapValue(questionSnapshot && questionSnapshot.bySpec, specId))
      .find(item => item.placement && item.placement.laneId === laneId && item.placement.goalId === goalId);
    if (placedQuestion && placedQuestion.placement.goalTitle) return placedQuestion.placement.goalTitle;
    const lane = inventoryLanes(inventory).find(item => item.lane_id === laneId);
    if (!lane) return null;
    const full = fullGoalsFor(lane, reads, goalId).goals;
    const found = [...asArray(lane.goals), ...full].find(item => item.goal_id === goalId);
    return found && found.title || null;
  }

  function queueVisibleGoalReads() {
    if (observer) { observer.disconnect(); observer = null; }
    const targets = root.querySelectorAll('[data-goals-read-needed="true"]');
    if (!targets.length) return;
    const Observer = doc.defaultView && doc.defaultView.IntersectionObserver || globalThis.IntersectionObserver;
    if (typeof Observer === 'function') {
      observer = new Observer(entries => {
        for (const entry of entries) if (entry.isIntersecting) {
          const id = entry.target.dataset.laneId;
          const lane = snapshot().inventory && inventoryLanes(snapshot().inventory).find(item => item.lane_id === id);
          if (lane) ensureRead(lane, 'goals');
          observer && observer.unobserve(entry.target);
        }
      });
      targets.forEach(target => observer.observe(target));
      return;
    }
    const state = snapshot();
    for (const target of targets) {
      const lane = inventoryLanes(state.inventory).find(item => item.lane_id === target.dataset.laneId);
      if (lane) ensureRead(lane, 'goals');
    }
  }

  function render() {
    if (destroyed) return null;
    const oldKey = routeKey(ui.route);
    if (currentBody) scrollPositions.set(oldKey, currentBody.scrollTop || 0);
    const { inventory, questionSnapshot } = snapshot();
    const expanded = ui.expandedSections;
    const model = buildLanesViewModel({ inventory, reads, questions: questionSnapshot, route: ui.route, now: typeof now === 'function' ? now() : now, expanded });
    const route = readRoute(model);
    model.routeGoalTitle = ui.route.goalTitle;
    model.routeGoalId = ui.route.goalId;
    model.goalTitle = model.title;
    const scrollTop = scrollPositions.get(routeKey(ui.route)) || 0;
    unmountQuestionHosts();
    currentBody = drawModel(root, model, { doc, questions, ui, scrollTop, addUnmount });
    requiredRead(model);
    queueVisibleGoalReads();
    return model;
  }

  function navigate(route) {
    const oldKey = routeKey(ui.route);
    if (currentBody) scrollPositions.set(oldKey, currentBody.scrollTop || 0);
    ui.route = { ...route };
    render();
    if (typeof navigation.onNavigate === 'function') navigation.onNavigate({ ...ui.route });
  }

  function openLane(laneOrId) {
    const target = laneOrId && typeof laneOrId === 'object' ? laneOrId : { laneId: laneOrId };
    navigate({ level: 'lane', laneId: target.laneId || target.lane_id });
  }

  function openGoal(laneOrId, goalId, goalTitle) {
    if (laneOrId && typeof laneOrId === 'object') {
      const target = laneOrId;
      navigate({ level: 'goal', laneId: target.laneId || target.lane_id, goalId: target.goalId ?? target.goal_id ?? null, goalTitle: target.goalTitle || target.title || null });
      return;
    }
    navigate({ level: 'goal', laneId: laneOrId, goalId: goalId == null ? null : goalId, goalTitle: goalTitle || null });
  }

  function openSpec(laneOrTarget, goalId, specId, specTitle, goalTitle) {
    if (laneOrTarget && typeof laneOrTarget === 'object') {
      const target = laneOrTarget;
      const laneId = target.laneId || target.lane_id;
      const targetGoalId = target.goalId ?? target.goal_id ?? null;
      const targetSpecId = target.specId || target.spec_id;
      navigate({ level: 'spec', laneId, goalId: targetGoalId,
        goalTitle: target.goalTitle || target.goal_title || findGoalTitle(laneId, targetGoalId, targetSpecId),
        specId: targetSpecId, specTitle: target.specTitle || target.spec_title || target.title || null });
      return;
    }
    const targetGoalId = goalId == null ? null : goalId;
    navigate({ level: 'spec', laneId: laneOrTarget, goalId: targetGoalId,
      goalTitle: goalTitle || findGoalTitle(laneOrTarget, targetGoalId, specId), specId, specTitle: specTitle || null });
  }

  function back() {
    if (ui.route.level === 'spec') {
      if (ui.route.goalId) openGoal(ui.route.laneId, ui.route.goalId, ui.route.goalTitle);
      else openLane(ui.route.laneId);
    } else if (ui.route.level === 'goal') openLane(ui.route.laneId);
    else if (ui.route.level === 'lane') navigate({ level: 'all' });
    else close();
  }

  function close() {
    if (typeof navigation.onClose === 'function') navigation.onClose();
  }

  root.addEventListener('click', handleClick);
  if (reads && typeof reads.subscribe === 'function') unsubscribe = reads.subscribe(render);
  render();
  return {
    render,
    openLane,
    openGoal,
    openSpec,
    openTarget(target) {
      if (target && (target.specId || target.spec_id)) return openSpec(target);
      if (target && (target.goalId || target.goal_id)) return openGoal(target);
      if (target) return openLane(target);
    },
    back,
    close,
    getRoute: () => ({ ...ui.route }),
    destroy() {
      if (destroyed) return;
      destroyed = true;
      root.removeEventListener('click', handleClick);
      if (typeof unsubscribe === 'function') unsubscribe();
      if (observer) observer.disconnect();
      unmountQuestionHosts();
      root.replaceChildren();
    },
  };
}

module.exports = { buildLanesViewModel, mountLanesCard, renderLaneIcon };
