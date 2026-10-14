import React, { useEffect, useLayoutEffect, useRef, useSyncExternalStore } from 'react';
import { GlassesController, QUICK_REPLIES } from './glasses_controller';
import { machineSigil } from './cosmic_components';
import { renderTranscriptTimelineHtml } from './shared_transcript_view';

function Sigil({ kind = 'djinni', accent = '#3dff66' }: { kind?: string; accent?: string }) {
  const markup = machineSigil(kind as Parameters<typeof machineSigil>[0], { size: 26, color: accent }).outerHTML;
  return <span className="sigil" aria-hidden="true" style={{ borderColor: accent }} dangerouslySetInnerHTML={{ __html: markup }}/>;
}

function StateDot({ status }: { status: string }) {
  return <span aria-label={status} className={`state-dot ${status}`}>{status === 'blocked' ? '?' : status === 'working' ? '' : 'Ⅱ'}</span>;
}
function Stars() {
  return <div className="stars" aria-hidden="true">{Array.from({ length: 70 }, (_, i) => <i key={i} style={{ left: `${(i * 37.71 + 9) % 100}%`, top: `${(i * 23.31 + 3) % 100}%`, opacity: .14 + (i % 5) * .07, width: 1 + i % 3, height: 1 + i % 3 }} />)}</div>;
}
function questionEnvelope(q: any) { return q?.envelope || q?.question || q || {}; }
function optionsFor(q: any) {
  const envelope = questionEnvelope(q);
  return (Array.isArray(envelope.options) ? envelope.options : []).map((option: any) => {
    const matches = (Array.isArray(q.actions) ? q.actions : []).filter((a: any) => a.value?.question_id === q.question_id && a.value?.answer === option.value);
    return { ...option, action_id: matches.length === 1 ? matches[0].action_id : undefined, kind: matches.length === 1 ? matches[0].kind : undefined };
  });
}

export function GlassesApp({ controller: c }: { controller: GlassesController }) {
  useSyncExternalStore(c.subscribe, c.getSnapshot);
  const screen = useRef<HTMLDivElement>(null);
  const thread = useRef<HTMLDivElement>(null);
  const questionText = useRef<HTMLDivElement>(null);
  const composer = useRef<HTMLTextAreaElement>(null);
  const top = c.top;
  const streamId = top.type === 'chat' || top.type === 'questions' ? top.streamId : '';
  const question = c.currentQuestions[c.page];
  const envelope = questionEnvelope(question);
  const options = optionsFor(question);
  const readOnly = question && (!question.noticeMatched || options.some((o: any) => !o.action_id));
  const customAllowed = envelope.response_mode === 'free_text' || envelope.allow_custom === true || envelope.allowCustom === true;
  const answered = question && c.answered.has(c.questionId(question));
  const rows = c.sessions.filter((s: any) => s.streamId !== c.assistantId);
  const focusElements = () => Array.from(screen.current?.querySelectorAll<HTMLElement>(c.voice ? '.voice [data-nav]' : 'main [data-nav]') || []).filter(el => !(el as HTMLButtonElement).disabled && !el.hidden);

  useLayoutEffect(() => {
    const el = focusElements()[Math.min(c.focus, Math.max(0, focusElements().length - 1))];
    if (el && document.activeElement !== el) el.focus({ preventScroll: true });
    if (el) {
      const list = el.closest<HTMLElement>('.list');
      if (list) list.scrollTop = Math.max(0, el.offsetTop - list.offsetTop - list.clientHeight / 2 + el.clientHeight / 2);
    }
  }, [c.focus, top.type, streamId, c.page, c.voice?.phase]);
  useEffect(() => { if (questionText.current) questionText.current.scrollTop = 0; }, [c.page, top.type]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // System-composer text is committed through input/change, never keydown.
      // Leave typing and Backspace editing alone while its field has focus.
      if (e.isComposing || (document.activeElement === composer.current && !e.key.startsWith('Arrow') && e.key !== 'Escape' && e.key !== 'Enter')) return;
      if (!['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Enter', ' ', 'Escape', 'Backspace'].includes(e.key)) return;
      e.preventDefault();
      if (e.key === 'Escape' || e.key === 'Backspace') return c.back();
      if (e.key === 'Enter' || e.key === ' ') {
        const active = document.activeElement as HTMLElement;
        if (active === composer.current) { if (c.voice?.text.trim()) void c.send(); else active.click(); return; }
        if (active?.hasAttribute('data-nav')) active.click();
        return;
      }
      if (!c.voice && top.type === 'chat' && (e.key === 'ArrowUp' || e.key === 'ArrowDown')) {
        thread.current?.scrollBy({ top: e.key === 'ArrowUp' ? -130 : 130, behavior: 'smooth' }); return;
      }
      if (!c.voice && top.type === 'chat' && e.key === 'ArrowRight') return c.openQuestions(streamId);
      if (!c.voice && top.type === 'questions' && c.focus === 0 && (e.key === 'ArrowUp' || e.key === 'ArrowDown')) {
        const el = questionText.current;
        if (el && ((e.key === 'ArrowDown' && el.scrollTop < el.scrollHeight - el.clientHeight - 1) || (e.key === 'ArrowUp' && el.scrollTop > 0))) { el.scrollBy({ top: e.key === 'ArrowDown' ? 100 : -100, behavior: 'smooth' }); return; }
      }
      if (!c.voice && top.type === 'questions' && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) return c.paginate(e.key === 'ArrowLeft' ? -1 : 1);
      if (e.key === 'ArrowLeft' && !c.voice) return c.back();
      if (e.key === 'ArrowRight' && !c.voice && (top.type === 'home' || top.type === 'chats')) return (document.activeElement as HTMLElement)?.click();
      if (e.key === 'ArrowUp' || e.key === 'ArrowDown') { c.focus = Math.max(0, focusElements().indexOf(document.activeElement as HTMLElement)); c.moveFocus(e.key === 'ArrowUp' ? -1 : 1, focusElements().length); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });

  const row = (key: string, title: string, sub: string, action: () => void, badge?: React.ReactNode, host?: string, showIcon = true) => {
    const accent = host ? c.hostAccent(host) : '#3dff66';
    return <button key={key} type="button" className="row" data-nav style={{ '--accent': accent } as React.CSSProperties} onClick={action}>{showIcon && <Sigil kind={host ? c.hostIcon(host) : 'djinni'} accent={accent}/>}<span className="row-body"><b>{title}</b><small>{sub}</small></span>{badge}</button>;
  };
  const header = (title: string, right?: React.ReactNode, host?: string) => <header><Sigil kind={host ? c.hostIcon(host) : 'djinni'} accent={host ? c.hostAccent(host) : '#3dff66'}/><b>{title}</b>{right}</header>;
  let view: React.ReactNode;
  if (top.type === 'home') {
    // Home entries are data-shaped; a future read-only entry can be added here.
    const home = [
      { key: 'assistant', title: c.title(c.assistantId), sub: c.store.selectSessionDetail(c.assistantId)?.transcriptItems.at(-1)?.text || 'Open assistant chat', action: () => { if (c.assistantId) c.open({ type: 'chat', streamId: c.assistantId }); }, badge: c.questionCount(c.assistantId) > 0 ? <StateDot status="blocked"/> : null },
      { key: 'chats', title: 'Chats', sub: `${rows.length} sessions`, action: () => c.open({ type: 'chats' }), badge: null },
    ];
    view = <div className="list home">{home.map(entry => row(entry.key, entry.title, entry.sub, entry.action, entry.badge, undefined, entry.key === 'assistant'))}</div>;
  } else if (top.type === 'chats') {
    view = <>{header('Chats')}<div className="list">{rows.map((s: any) => row(s.streamId, s.title, `${c.hostTitle(s.host)} · ${s.previewText || s.statusLabel}`, () => c.open({ type: 'chat', streamId: s.streamId }), <StateDot status={c.status(s.streamId)}/>, s.host))}{!rows.length && <p className="empty">No sessions available.</p>}</div></>;
  } else if (top.type === 'questions') {
    const producerHost = String(question?.producer_stream_id || streamId).split(':')[0];
    view = <>{header(c.hostTitle(producerHost), <span className="pager" aria-label={`Question ${c.page + 1} of ${c.currentQuestions.length}`}>{c.currentQuestions.map((q, i) => <span key={c.questionId(q)} className={i === c.page ? 'current' : c.answered.has(c.questionId(q)) ? 'answered' : ''}/>)}</span>, producerHost)}
      <div className="question-text" ref={questionText}>{envelope.title && <b>{envelope.title}<br/></b>}{envelope.body || 'No pending questions.'}</div>{readOnly && <p className="read-only">Answer from phone/desktop: actions unavailable.</p>}
      <div className="list amber">{options.map((option: any, i: number) => <button data-nav key={option.action_id || i} className="row" disabled={c.settling || answered || readOnly} onClick={() => { void c.settle(question, option).catch(() => {}); }}><span className="option-dot">{answered && c.answerValues.get(c.questionId(question)) === option.value ? '✓' : ''}</span><span className="row-body"><b>{option.label || String(option.value)}</b>{option.description && <small>{option.description}</small>}</span></button>)}{question && <button data-nav className="row custom" disabled={c.settling || answered || readOnly || !customAllowed} onClick={() => c.compose(streamId, question)}>♩ <b>Say something else</b></button>}</div></>;
  } else {
    const isAssistant = c.store.getState().sessions.find(s => s.stream_id === streamId)?.session_kind === 'assistant_composite';
    const available = !isAssistant || c.assistantId === streamId;
    const detail = available ? c.store.selectSessionDetail(streamId) : null;
    // Share parsing, escaping and display rules; v1 only shows text bubbles.
    const transcriptItems = (detail?.transcriptItems || []).filter(item => item.displayRule === 'bubble:user' || item.displayRule === 'bubble:assistant').slice(-40).reverse();
    const html = renderTranscriptTimelineHtml(detail ? { ...detail, transcriptItems: transcriptItems.map(item => ({ ...item, attachments: [] })) } : null)
      .replace(/<a\b[^>]*>/g, '<span>').replace(/<\/a>/g, '</span>');
    view = <>{header(c.title(streamId), undefined, isAssistant ? undefined : streamId.split(':')[0])}<div ref={thread} className="thread" role="log" aria-label="Latest messages" dangerouslySetInnerHTML={{ __html: html || (available ? '<p class="empty">No messages yet.</p>' : '<p class="empty">Assistant binding unavailable or changed. Open the main app.</p>') }}/><div className="chat-bar"><span className="status"><StateDot status={c.status(streamId)}/>{c.status(streamId) === 'working' ? 'Working' : c.status(streamId) === 'offline' ? 'Offline' : ''}</span><button data-nav className="compose-button" disabled={!c.connected || !available} onClick={() => c.compose(streamId)}>♩ Speak</button><button data-nav className="questions-button" hidden={!c.questionCount(streamId)} onClick={() => c.openQuestions(streamId)}>{c.questionCount(streamId)} questions →</button></div></>;
  }
  const voice = c.voice;
  const voiceName = voice?.question ? c.hostTitle(String(voice.question.producer_stream_id || voice.streamId).split(':')[0]) : c.title(voice?.streamId || '');
  return <div className="glasses" ref={screen} data-screen-label={top.type}><Stars/><main inert={voice ? true : undefined}>{view}</main>
    {voice && <section className="voice" role="dialog" aria-label="Voice message"><div className="voice-status" role="status">{voice.phase === 'rec' ? <><span className="wave">{Array.from({ length: 9 }, (_, i) => <i key={i} style={{ animationDelay: `${i % 5 * .12}s` }}/>)}</span><b>To {voiceName}</b><small>Pinch send · Back cancel</small></> : voice.phase === 'sending' ? <><StateDot status="working"/><b>Sending</b></> : <><span className="sent-check">✓</span><b>Sent to {voiceName}</b></>}</div>
      {voice.phase === 'rec' && <><textarea ref={composer} data-nav aria-label="Activate dictation composer" placeholder="Pinch here to dictate" value={voice.text} onInput={e => c.commit(e.currentTarget.value)} onChange={e => c.commit(e.currentTarget.value)} onClick={e => e.currentTarget.focus()}/><button data-nav className="send-button" disabled={!voice.text.trim() || !c.connected} onClick={() => { void c.send(); }}>Send message</button><div className="quick-replies"><small>Quick replies</small>{QUICK_REPLIES.map(text => <button data-nav key={text} disabled={!c.connected} onClick={() => { void c.send(text); }}>{text}</button>)}</div></>}{voice.error && <p className="error" role="alert">{voice.error}</p>}</section>}
    {(c.error || (top.type === 'home' && (c.bindingError || c.assistantResolution.error))) && <p className="error" role="alert">{c.error || c.bindingError || c.assistantResolution.error}</p>}
    <footer>NEURAL BAND · ARROWS · ENTER = PINCH · ESC = BACK{!c.connected && <span> · Offline</span>}</footer>
  </div>;
}
