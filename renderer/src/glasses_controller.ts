import { ChatStoreController } from './chat_store_controller';
import { hostLabel, hostColor, hostSigil, ACCENTS } from '../host_presentation';
import { filterSidebarSessions } from '../sidebar_filter';

// The browser uses the existing /cc transport. All daemon credentials stay on its host.
export interface GlassesCc {
  getAssistantBinding(): Promise<any>;
  getChatStreamState(): Promise<any>;
  onChatStreamFrame(cb: (frame: any) => void): (() => void) | void;
  onReconnect?(cb: () => void): (() => void) | void;
  promptList(args: { producer_stream_id: string; open: boolean }): Promise<any>;
  requestStreamEvents(args: { streamId: string; generation?: string; limit: number }): Promise<any>;
  notificationList(args: { states: string[]; limit: number }): Promise<any>;
  notificationResolve(id: string, kind: string, options: any): Promise<any>;
  chatSendCorrelated(host: string, session: string, text: string, requestId: string, optimisticId: string, attachments?: any[], reply?: any): Promise<any>;
}
export type Screen = { type: 'home' | 'chats' } | { type: 'chat' | 'questions'; streamId: string };
export const QUICK_REPLIES = ['Please give me a brief update.', 'Please continue.', 'Please pause and wait for me.'];
export type Voice = { phase: 'rec' | 'sending' | 'sent'; streamId: string; text: string; question?: any; error?: string };

export class GlassesController {
  readonly store = new ChatStoreController();
  stack: Screen[] = [{ type: 'home' }];
  focus = 0;
  page = 0;
  binding: any = null;
  questions: any[] = [];
  answered = new Set<string>();
  answerValues = new Map<string, string>();
  private settlingQuestionId: string | null = null;
  voice: Voice | null = null;
  error = '';
  bindingError = '';
  connected = false;
  settling = false;
  private revision = 0;
  private listeners = new Set<() => void>();
  private disposers: (() => void)[] = [];
  private poll?: ReturnType<typeof setInterval>;
  private timer?: ReturnType<typeof setTimeout>;
  private refreshGeneration = 0;
  private bindingGeneration = 0;
  private disposed = false;
  private sendAttempt: { text: string; streamId: string; requestId: string; optimisticId: string } | null = null;
  constructor(readonly cc: GlassesCc, private id: () => string = () => crypto.randomUUID(), readonly config: any = {}) {}
  subscribe = (fn: () => void) => { this.listeners.add(fn); return () => { this.listeners.delete(fn); }; };
  getSnapshot = () => this.revision;
  changed() { if (this.disposed) return; this.revision++; this.listeners.forEach(fn => fn()); }
  get top() { return this.stack[this.stack.length - 1]; }
  get assistantResolution(): { streamId: string; error: string } {
    const sessions = this.store.getState().sessions;
    const composites = filterSidebarSessions(sessions).filter((s: any) => s.session_kind === 'assistant_composite');
    if (!composites.length) return { streamId: '', error: 'Assistant unavailable.' };
    if (composites.length > 1) return { streamId: '', error: 'Ambiguous assistant binding. Open the main app.' };
    const backend = sessions.find(s => s.stream_id === this.binding?.stream_id);
    if (!this.binding || this.binding.ok === false || !backend || !this.binding.generation
      || backend.session_generation !== this.binding.generation || (backend as any).closed_at || (backend as any).status === 'closed') {
      return { streamId: '', error: 'Assistant binding unavailable or changed. Open the main app.' };
    }
    return { streamId: composites[0].stream_id, error: '' };
  }
  get assistantId(): string { return this.assistantResolution.streamId; }
  get sessions() {
    const visible = new Set(filterSidebarSessions(this.store.getState().sessions).map((s: any) => s.stream_id));
    return this.store.selectChatList().filter(row => visible.has(row.streamId));
  }
  title(streamId: string) { return this.store.selectSessionDetail(streamId)?.title || this.binding?.display_name || 'Assistant'; }
  hostTitle(host: string) { return hostLabel(this.config, host); }
  hostAccent(host: string) { return ACCENTS[hostColor(this.config, host) as keyof typeof ACCENTS] || '#3dff66'; }
  hostIcon(host: string) { return hostSigil(this.config, host) || 'djinni'; }
  get currentQuestions() { return this.questions.filter(q => q.scope === (this.top as any).streamId); }
  questionId(q: any): string { return q.question_id || q.notification_id; }
  questionCount(streamId: string) { return this.questions.filter(q => q.scope === streamId && !this.answered.has(this.questionId(q))).length; }
  status(streamId: string): 'working' | 'blocked' | 'offline' | 'idle' {
    const detail = this.store.selectSessionDetail(streamId);
    const isAssistant = this.store.getState().sessions.find(s => s.stream_id === streamId)?.session_kind === 'assistant_composite';
    const backendDetail = isAssistant ? this.store.selectSessionDetail(this.binding?.stream_id || '') : null;
    if (backendDetail?.status === 'offline') return 'offline';
    if (detail?.status === 'offline') return 'offline';
    const summary = this.store.getState().sessions.find(s => s.stream_id === streamId);
    if (this.questionCount(streamId) || summary?.question || summary?.assistant_activity?.waiting_for_operator_count || /waiting|blocked/i.test(summary?.working_label || detail?.statusLabel || '')) return 'blocked';
    return detail?.status === 'working' || detail?.status === 'sending' || backendDetail?.status === 'working' ? 'working' : 'idle';
  }
  async start() {
    let booting = true;
    const queued: any[] = [];
    const off = this.cc.onChatStreamFrame(frame => {
      if (booting) { queued.push(frame); return; }
      this.applyFrame(frame);
    });
    if (off) this.disposers.push(off);
    try { await this.bootstrap(); } finally {
      booting = false;
      queued.forEach(frame => this.applyFrame(frame));
    }
    if (this.disposed) return;
    const reconnect = this.cc.onReconnect?.(() => { void this.bootstrap(); });
    if (reconnect) this.disposers.push(reconnect);
    if (!this.disposed) this.poll = setInterval(() => { void this.refreshBinding(); void this.refreshQuestions(); }, 10000);
  }
  private async bootstrap() {
    try {
      const [binding, state] = await Promise.all([this.cc.getAssistantBinding(), this.cc.getChatStreamState()]);
      if (binding?.ok === false) throw new Error(binding.error || 'Assistant binding unavailable');
      if (!state || state.ok === false) throw new Error(state?.error || 'Chat state unavailable');
      if (this.disposed) return;
      this.binding = binding;
      this.connected = state.connected !== false;
      this.store.applyFrame({ ...state, type: 'snapshot' });
      this.error = this.assistantResolution.error;
      this.changed();
      await Promise.all([this.refreshQuestions(), this.assistantId ? this.loadTranscript(this.assistantId) : Promise.resolve()]);
    } catch (error) { this.error = String((error as Error).message || error); this.changed(); }
  }
  async refreshBinding() {
    const generation = ++this.bindingGeneration;
    try {
      const binding = await this.cc.getAssistantBinding();
      if (this.disposed || generation !== this.bindingGeneration) return;
      if (binding?.ok === false) throw new Error(binding.error || 'Assistant binding unavailable');
      this.binding = binding; this.bindingError = ''; this.changed();
    } catch (error) { this.binding = null; this.bindingError = String((error as Error).message || error); this.changed(); }
  }
  applyFrame(frame: any) {
    this.store.applyFrame(frame);
    if (typeof frame.connected === 'boolean') this.connected = frame.connected;
    this.changed();
    if (/session.inventory|snapshot/.test(frame.type || '')) void this.refreshBinding();
    if (/notification|prompt|question|session.inventory|snapshot/.test(frame.type || '')) void this.refreshQuestions();
  }
  async refreshQuestions() {
    const generation = ++this.refreshGeneration;
    const scope = this.top.type === 'chat' || this.top.type === 'questions' ? this.top.streamId : this.assistantId;
    if (!scope) return;
    try {
      const result = await this.cc.promptList({ producer_stream_id: scope, open: true });
      if (generation !== this.refreshGeneration || this.disposed) return;
      if (result?.ok === false || !Array.isArray(result?.questions)) throw new Error(result?.error || 'Questions unavailable');
      // prompt.list supplies envelopes; notification.list supplies their action IDs.
      const notices = await this.cc.notificationList({ states: ['open'], limit: 1000 });
      if (generation !== this.refreshGeneration || this.disposed) return;
      if (notices?.ok === false || !Array.isArray(notices?.notifications)) throw new Error(notices?.error || 'Question actions unavailable');
      const rows = result.questions.map((q: any) => ({ ...q, scope,
        noticeMatched: notices.notifications.some((n: any) => n.notification_id === q.notification_id && n.state === 'open'),
        actions: notices.notifications.find((n: any) => n.notification_id === q.notification_id && n.state === 'open')?.actions || [],
      }));
      // Retain answered cards while paging, so the green dots remain visible.
      const retained = this.questions.filter(q => q.scope !== scope || this.answered.has(this.questionId(q)) || this.questionId(q) === this.settlingQuestionId);
      this.questions = [...retained, ...rows.filter((q: any) => !retained.some(r => this.questionId(r) === this.questionId(q)))];
      this.changed();
    } catch (error) { this.error = String((error as Error).message || error); this.changed(); }
  }
  open(screen: Screen) { this.stack.push(screen); this.focus = 0; this.page = 0; this.error = ''; this.changed(); void this.refreshQuestions(); if (screen.type === 'chat') void this.loadTranscript(screen.streamId); }
  async loadTranscript(streamId: string) {
    try {
      const session = this.store.getState().sessions.find(s => s.stream_id === streamId);
      const reply = await this.cc.requestStreamEvents({ streamId, generation: session?.session_generation, limit: 80 });
      if (this.disposed) return;
      if (reply?.ok === false) throw new Error(reply.error || 'History unavailable');
      // The host also broadcasts stream_events; the shared reducer deduplicates.
      if (Array.isArray(reply?.events)) this.store.applyFrame({ ...reply, type: 'stream_events', stream_id: streamId });
      this.changed();
    } catch (error) { this.error = String((error as Error).message || error); this.changed(); }
  }
  back() {
    if (this.voice?.phase === 'sending' || this.settling) return;
    if (this.voice) { this.voice = null; this.sendAttempt = null; }
    else if (this.stack.length > 1) { this.stack.pop(); this.focus = 0; this.page = 0; }
    this.error = ''; this.changed();
  }
  openQuestions(streamId: string) { if (this.questionCount(streamId)) this.open({ type: 'questions', streamId }); }
  moveFocus(delta: number, count: number) { this.focus = Math.min(Math.max(0, this.focus + delta), Math.max(0, count - 1)); this.changed(); }
  paginate(delta: number) {
    if (this.page === 0 && delta < 0) return this.back();
    this.page = Math.min(Math.max(0, this.page + delta), Math.max(0, this.currentQuestions.length - 1)); this.focus = 0; this.changed();
  }
  compose(streamId: string, question?: any) { this.voice = { phase: 'rec', streamId, text: '', question }; this.focus = 0; this.sendAttempt = null; this.changed(); }
  commit(text: string) { if (this.voice?.phase === 'rec') { this.voice.text = text; this.changed(); } }
  async send(text = this.voice?.text || '') {
    const voice = this.voice;
    text = text.trim();
    if (!voice || voice.phase !== 'rec' || !text || !this.connected) return;
    const destination = this.store.getState().sessions.find(s => s.stream_id === voice.streamId);
    if (destination?.session_kind === 'assistant_composite' && this.assistantId !== voice.streamId) {
      voice.error = 'Assistant binding changed. Close this message and reopen the chat.'; this.changed(); return;
    }
    voice.text = text; voice.phase = 'sending'; voice.error = ''; this.changed();
    try {
      if (voice.question) {
        await this.settle(voice.question, undefined, text);
      } else {
        if (!this.sendAttempt || this.sendAttempt.text !== text || this.sendAttempt.streamId !== voice.streamId) {
          this.sendAttempt = { text, streamId: voice.streamId, requestId: this.id(), optimisticId: this.id() };
        }
        const attempt = this.sendAttempt;
        const colon = voice.streamId.indexOf(':');
        if (colon < 1) throw new Error('Invalid chat destination');
        const reply = await this.cc.chatSendCorrelated(voice.streamId.slice(0, colon), voice.streamId.slice(colon + 1), text, attempt.requestId, attempt.optimisticId, [], { stream_id: voice.streamId });
        if (reply?.ok !== true) throw new Error(reply?.error || 'Send failed');
      }
      voice.phase = 'sent'; this.changed();
      this.timer = setTimeout(() => { if (this.voice === voice) { this.voice = null; this.sendAttempt = null; this.changed(); } }, 1200);
    } catch (error) { voice.phase = 'rec'; voice.error = String((error as Error).message || error); this.changed(); }
  }
  async settle(q: any, option?: any, text?: string) {
    if (this.settling || this.answered.has(this.questionId(q))) return;
    const envelope = q.envelope || q.question || q;
    if (!q.noticeMatched || (option && !q.actions.some((a: any) => a.action_id === option.action_id && a.value?.answer === option.value && a.value?.question_id === q.question_id))) { this.error = 'Answer from phone/desktop: question actions unavailable.'; this.changed(); throw new Error(this.error); }
    if (option && !option.action_id) { this.error = 'This option has no action ID. Please use the main app.'; this.changed(); return; }
    this.settling = true; this.settlingQuestionId = this.questionId(q); this.error = ''; this.changed();
    try {
      const freeText = envelope.response_mode === 'free_text';
      const kind = freeText ? 'resolved' : option?.kind || q.actions?.find((action: any) => action?.kind)?.kind || 'yes_no';
      const answer = option ? { action_id: option.action_id, selections: [option.value] }
        : freeText ? { text } : { custom_text: text };
      const reply = await this.cc.notificationResolve(q.notification_id, kind, {
        ...answer, submit: true, by: 'operator',
      });
      if (reply?.ok !== true) throw new Error(reply?.error || 'Answer failed');
      // An acknowledged command is insufficient: read back the open set.
      const readback = await this.cc.promptList({ producer_stream_id: q.scope, open: true });
      if (readback?.ok === false || !Array.isArray(readback?.questions)
        || readback.questions.some((row: any) => this.questionId(row) === this.questionId(q))) {
        throw new Error('Answer not confirmed. Check the main app before retrying.');
      }
      this.answered.add(this.questionId(q));
      this.answerValues.set(this.questionId(q), option?.value || '__custom');
      const next = this.currentQuestions.findIndex(row => !this.answered.has(this.questionId(row)));
      if (next >= 0) { this.page = next; this.focus = 0; }
      else if (this.top.type === 'questions') { this.stack.pop(); this.page = 0; this.focus = 0; }
    } catch (error) { this.error = String((error as Error).message || error); throw error; }
    finally { this.settling = false; this.settlingQuestionId = null; this.changed(); }
  }
  dispose() { this.disposed = true; this.disposers.forEach(off => off()); clearInterval(this.poll); clearTimeout(this.timer); this.store.dispose(); this.listeners.clear(); }
}
