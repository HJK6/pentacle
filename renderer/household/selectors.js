'use strict';
// Pure display rules for Personal, Lists and Calendar (spec § B4/B5). Every calendar day comes
// from `snapshot.today` (America/Chicago) and string/UTC arithmetic, never the device clock/zone.
const LIST_ORDER = ['tasks', 'grocery', 'meals', 'chores', 'study'];
const LIST_META = {
    tasks: { name: 'To-do', placeholder: 'New task…' },
    grocery: { name: 'Grocery', placeholder: 'Add an item…' },
    meals: { name: 'Meals', placeholder: 'Add a meal…' },
    chores: { name: 'Chores', placeholder: 'Add a chore…' },
    study: { name: 'Study plan', placeholder: 'Add a study block…' },
};
const isListId = (value) => typeof value === 'string' && LIST_ORDER.includes(value);
const DOW_SHORT = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const MONTHS = [
    'January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December',
];
// ---- day arithmetic (UTC only) ----------------------------------------------------------------
function parts(date) {
    const [y, m, d] = date.split('-').map(Number);
    return [y, m, d];
}
function utc(date) {
    const [y, m, d] = parts(date);
    return new Date(Date.UTC(y, m - 1, d));
}
function addDays(date, n) {
    const [y, m, d] = parts(date);
    return new Date(Date.UTC(y, m - 1, d + n)).toISOString().slice(0, 10);
}
const monthOf = (date) => date.slice(0, 7);
const dayOfMonth = (date) => parts(date)[2];
const monthName = (date) => MONTHS[parts(date)[1] - 1];
const yearOf = (date) => date.slice(0, 4);
/** `Tue` */
const dowShort = (date) => DOW_SHORT[utc(date).getUTCDay()];
/** 0 = Sunday */
const weekdayIndex = (date) => utc(date).getUTCDay();
function daysInMonth(month) {
    const [y, m] = month.split('-').map(Number);
    return new Date(Date.UTC(y, m, 0)).getUTCDate();
}
const dateIn = (month, day) => `${month}-${String(day).padStart(2, '0')}`;
/** `WED 7` */
const dayLabel = (date) => `${dowShort(date).toUpperCase()} ${dayOfMonth(date)}`;
/** `TUE · OCTOBER 6` */
const personalHeaderLabel = (today) => `${dowShort(today).toUpperCase()} · ${monthName(today).toUpperCase()} ${dayOfMonth(today)}`;
/** Route `date?`: an America/Chicago `YYYY-MM-DD` in 2000-01-01 … 2100-12-31, else null (use today). */
function parseRouteDate(value) {
    if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value))
        return null;
    if (value < '2000-01-01' || value > '2100-12-31')
        return null;
    const [y, m, d] = parts(value);
    if (m < 1 || m > 12 || d < 1 || d > daysInMonth(`${value.slice(0, 4)}-${String(m).padStart(2, '0')}`)) {
        return null;
    }
    return value;
}
// ---- items ------------------------------------------------------------------------------------
const PRIORITY_RANK = { hi: 0, med: 1, lo: 2 };
/** Priority (hi, med, lo), then Cosmo order (position, id). Does not mutate its input. */
function sortItems(items) {
    return [...items].sort((a, b) => PRIORITY_RANK[a.priority] - PRIORITY_RANK[b.priority] || a.position - b.position || a.id - b.id);
}
/** Due tag first, then HIGH/LOW (D2, D5). Undated `med` items carry no tag. */
function itemTags(item, today) {
    const tags = [];
    const due = item.due_date;
    if (due) {
        if (due < today)
            tags.push({ text: 'OVERDUE', tone: 'red' });
        else if (due === today)
            tags.push({ text: 'DUE TODAY', tone: 'red' });
        else if (due === addDays(today, 1))
            tags.push({ text: 'DUE TOMORROW', tone: 'amber' });
        else
            tags.push({ text: `DUE ${monthName(due).slice(0, 3).toUpperCase()} ${dayOfMonth(due)}`, tone: 'amber' });
    }
    if (item.priority === 'hi')
        tags.push({ text: 'HIGH', tone: 'amber' });
    else if (item.priority === 'lo')
        tags.push({ text: 'LOW', tone: 'muted' });
    return tags;
}
/** The assistant's lamp only from actual attribution; due/priority alone never implies the assistant (D6). */
const showItemLamp = (item) => item.created_by === 'assistant';
/** Personal TO-DO: priority hi or due ≤ tomorrow (overdue included), list order then detail order. */
function criticalItems(snapshot) {
    const tomorrow = addDays(snapshot.today, 1);
    return LIST_ORDER.flatMap((list) => sortItems(snapshot.lists[list] ?? [])
        .filter((item) => item.priority === 'hi' || (item.due_date !== null && item.due_date <= tomorrow))
        .map((item) => ({ list, item })));
}
const DEFAULT_PARTNER = 'Partner';
const partnerName = (snapshot) => snapshot?.people?.partner?.trim() || DEFAULT_PARTNER;
/** Viewer-relative (the viewer is the operator). Scope only adds the visible PRIVATE suffix. */
function whoDisplay(event, partner = DEFAULT_PARTNER) {
    const name = partner.toUpperCase();
    const base = event.who === 'both'
        ? { label: `ME + ${name}`, bars: ['me', 'partner'] }
        : event.who === 'partner'
            ? { label: name, bars: ['partner'] }
            : { label: 'ME', bars: ['me'] };
    const privateToMe = event.scope === 'private' && (event.who === 'partner' || event.who === 'both');
    return privateToMe ? { ...base, label: `${base.label} · PRIVATE` } : base;
}
const isAssistantEvent = (event) => event.created_by === 'assistant';
/** All-day first, then 24 h time text, then id. */
function byDateTime(a, b) {
    if (a.date !== b.date)
        return a.date < b.date ? -1 : 1;
    if (a.time !== b.time) {
        if (a.time === null)
            return -1;
        if (b.time === null)
            return 1;
        return a.time < b.time ? -1 : 1;
    }
    return a.id - b.id;
}
const eventsOn = (events, date) => events.filter((e) => e.date === date).sort(byDateTime);
const todayEvents = (snapshot) => eventsOn(snapshot.events, snapshot.today);
function upcomingEvents(snapshot) {
    const last = addDays(snapshot.today, 7);
    return snapshot.events
        .filter((e) => e.date > snapshot.today && e.date <= last)
        .sort(byDateTime)
        .slice(0, 4);
}
/** `14:30` → `2:30p`, null → `ALL DAY` (D3). */
function formatTime(time) {
    if (time === null)
        return 'ALL DAY';
    const [h, m] = time.split(':').map(Number);
    const hour = h % 12 === 0 ? 12 : h % 12;
    return `${hour}:${String(m).padStart(2, '0')}${h < 12 ? 'a' : 'p'}`;
}
/** Blank → all day; `H:MM`/`HH:MM` 24 h; or 12 h with `a|am|p|pm`. */
function parseTimeInput(text) {
    const trimmed = text.trim();
    if (trimmed === '')
        return { ok: true, value: null };
    const match = /^(\d{1,2}):(\d{2})\s*(a|am|p|pm)?$/i.exec(trimmed);
    if (!match)
        return { ok: false };
    let hour = Number(match[1]);
    const minute = Number(match[2]);
    if (minute > 59)
        return { ok: false };
    const meridiem = match[3]?.toLowerCase();
    if (meridiem) {
        if (hour < 1 || hour > 12)
            return { ok: false };
        hour = (hour % 12) + (meridiem.startsWith('p') ? 12 : 0);
    }
    else if (hour > 23) {
        return { ok: false };
    }
    return { ok: true, value: `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}` };
}
/** New-event WHO toggles: Me → `self`, partner → `partner`, both → `both` (display only). */
function whoFromToggles(toggles) {
    if (toggles.me && toggles.partner)
        return 'both';
    return toggles.partner ? 'partner' : 'self';
}

module.exports = { LIST_ORDER, LIST_META, isListId, addDays, monthOf, dayOfMonth, monthName, yearOf,
  dowShort, weekdayIndex, daysInMonth, dateIn, dayLabel, personalHeaderLabel, parseRouteDate,
  sortItems, itemTags, showItemLamp, criticalItems, DEFAULT_PARTNER, partnerName, whoDisplay,
  isAssistantEvent, byDateTime, eventsOn, todayEvents, upcomingEvents, formatTime, parseTimeInput, whoFromToggles };
