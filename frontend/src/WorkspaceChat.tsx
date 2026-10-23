import usePermissions from './usePermissions';
import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { ArrowUp, BookOpen, Check, ChevronRight, Clock3, FileText, LoaderCircle, MessageCircle, Plus, Square, X } from 'lucide-react';
import { api, ApiError, message } from './api';
import type { Case, Project } from './types';
import './workspace-chat.css';
import WorkspaceMemory from './WorkspaceMemory';
import { chatIdentity, randomChatId, readChatOpen, readChatSelection, saveChatOpen, saveChatSelection } from './workspaceChatStorage';

type ChatSource = { case_id: string; label: string; project_name: string; location: string };
type Turn = { id: string; question: string; answer: string; state: string; progress: string; error: string | null; created_at: string; steps: { label: string; ok: boolean }[]; sources: ChatSource[] };
type Thread = { id: string; title: string; project_id: string | null; case_id: string | null; context_label: string; updated_at: string; turns: Turn[] };
type Index = { ready: boolean; model: string; threads: Omit<Thread, 'turns'>[]; active: { id: string; thread_id: string; state: string } | null };
const ACTIVE = new Set(['queued', 'running']);
function scopeOf(thread: Pick<Thread, 'project_id' | 'case_id'>) { return thread.case_id ? `case:${thread.case_id}` : thread.project_id ? `project:${thread.project_id}` : 'all'; }

export default function WorkspaceChat({ projects, currentCase, projectId, openCase, openSettings }: {
  projects: Project[]; currentCase?: Case; projectId: string;
  openCase: (caseId: string) => Promise<void>; openSettings: () => void;
}) {
  const { person, isAdmin } = usePermissions();
  const storageOwner = isAdmin ? '' : person.id;
  const [identity] = useState(chatIdentity), [selection] = useState(() => readChatSelection(storageOwner));
  const [open, setOpen] = useState(() => readChatOpen(storageOwner)), [history, setHistory] = useState(false);
  const [memoryOpen, setMemoryOpen] = useState(false);
  const [threadId, setThreadId] = useState(selection?.threadId || ''), [thread, setThread] = useState<Thread | null>(null);
  const [restoring, setRestoring] = useState(!selection), [missingThread, setMissingThread] = useState('');
  const [index, setIndex] = useState<Index | null>(null), [scope, setScope] = useState(selection?.scope || 'all');
  const [indexError, setIndexError] = useState(''), [threadError, setThreadError] = useState('');
  const [recoveryNotice, setRecoveryNotice] = useState('');
  const [draft, setDraft] = useState(''), [error, setError] = useState(''), [sending, setSending] = useState(false), [loading, setLoading] = useState(false), [revision, setRevision] = useState(0);
  const [unread, setUnread] = useState(false), [openingCase, setOpeningCase] = useState('');
  const dialog = useRef<HTMLDialogElement>(null), input = useRef<HTMLTextAreaElement>(null), launcher = useRef<HTMLButtonElement>(null), transcript = useRef<HTMLDivElement>(null);
  const pendingRequest = useRef<{ fingerprint: string; id: string } | null>(null), nearEnd = useRef(true), previousAnswer = useRef('');
  const scopeChosen = useRef(!!selection), unavailableThreads = useRef(new Set<string>());
  const request = useCallback(<T,>(path: string, options: RequestInit = {}) => api<T>('/api/workspace-chat' + path, { ...options, headers: { 'X-IDI-Chat-Key': identity.key } }), [identity.key]);
  const refresh = useCallback(() => setRevision(v => v + 1), []);
  const selectThread = useCallback((id: string, nextScope = 'all') => {
    scopeChosen.current = true;
    saveChatSelection({ threadId: id, scope: nextScope }, storageOwner);
    setRestoring(false); setMissingThread(''); setThreadError(''); setRecoveryNotice('');
    setThreadId(id); setThread(null); setScope(nextScope); nearEnd.current = true;
  }, []);
  const active = thread?.turns.find(t => ACTIVE.has(t.state));
  const busyElsewhere = !!index?.active && index.active.thread_id !== threadId;
  const busy = sending || !!active || busyElsewhere;
  const selectedScope = thread ? scopeOf(thread) : scope;
  const waitingForHistory = restoring || (!index && !indexError) || (!!threadId && !thread && !threadError);
  const readError = threadError || indexError;

  useEffect(() => {
    const abort = new AbortController();
    request<Index>('', { signal: abort.signal }).then(value => {
      if (!abort.signal.aborted) { setIndex(value); setIndexError(''); }
    }).catch(e => { if (!abort.signal.aborted) setIndexError(message(e)); });
    return () => abort.abort();
  }, [request, open, revision]);

  useEffect(() => {
    if (!index || (!restoring && !missingThread)) return;
    const latest = index.threads.find(item => !unavailableThreads.current.has(item.id));
    const activeId = index.active?.thread_id;
    const id = activeId && !unavailableThreads.current.has(activeId) ? activeId : latest?.id;
    if (id) selectThread(id);
    else {
      // Only an explicit 404 clears a stale selection. A network/server error
      // leaves the saved conversation intact for the next retry.
      if (missingThread) { selectThread(''); setRecoveryNotice('上次对话在当前浏览器中不可用，可从历史记录选择其他对话。'); }
      setRestoring(false);
    }
  }, [index, restoring, missingThread, selectThread]);

  useEffect(() => {
    if (!threadId) { setThread(null); setLoading(false); return; }
    const abort = new AbortController();
    setLoading(true);
    request<Thread>('/threads/' + threadId, { signal: abort.signal }).then(value => {
      if (abort.signal.aborted) return;
      setThread(value); setScope(scopeOf(value)); setLoading(false); setThreadError('');
      unavailableThreads.current.delete(value.id);
      saveChatSelection({ threadId: value.id, scope: scopeOf(value) }, storageOwner);
    }).catch(e => {
      if (!abort.signal.aborted) {
        setThreadError(message(e)); setLoading(false);
        if (e instanceof ApiError && [403, 404].includes(e.status)) { unavailableThreads.current.add(threadId); setMissingThread(threadId); }
      }
    });
    return () => abort.abort();
  }, [threadId, request, revision]);

  useEffect(() => {
    if (!active && !index?.active) return;
    const timer = window.setInterval(refresh, 1600);
    return () => window.clearInterval(timer);
  }, [active?.id, index?.active?.id, refresh]);

  useEffect(() => {
    saveChatOpen(open, storageOwner);
    const element = dialog.current;
    if (open) {
      if (element && !element.open) element.showModal();
      setUnread(false); nearEnd.current = true;
      window.requestAnimationFrame(() => { input.current?.focus(); transcript.current?.scrollTo({ top: transcript.current.scrollHeight }); });
    } else if (element?.open) { element.close(); launcher.current?.focus(); }
  }, [open]);

  useEffect(() => {
    if (!open || !window.visualViewport) return;
    const viewport = window.visualViewport;
    const fit = () => {
      dialog.current?.style.setProperty('--chat-visible-height', `${viewport.height}px`);
      dialog.current?.style.setProperty('--chat-keyboard-inset', `${Math.max(0, innerHeight - viewport.height - viewport.offsetTop)}px`);
      dialog.current?.style.setProperty('--chat-visible-top', `${viewport.offsetTop}px`);
    };
    fit(); viewport.addEventListener('resize', fit); viewport.addEventListener('scroll', fit);
    return () => { viewport.removeEventListener('resize', fit); viewport.removeEventListener('scroll', fit); };
  }, [open]);

  useEffect(() => {
    const last = thread?.turns.at(-1);
    const signature = last ? `${last.id}:${last.state}` : '';
    if (last && !ACTIVE.has(last.state) && previousAnswer.current.startsWith(last.id + ':') && previousAnswer.current !== signature && !open) setUnread(true);
    previousAnswer.current = signature;
    if (open && nearEnd.current) window.requestAnimationFrame(() => transcript.current?.scrollTo({ top: transcript.current.scrollHeight }));
  }, [thread, open]);

  function newConversation(nextScope?: string, keepDraft = false) {
    if (busy) return;
    selectThread('', nextScope || (currentCase ? `case:${currentCase.id}` : projectId ? `project:${projectId}` : 'all'));
    setHistory(false); setError(''); pendingRequest.current = null;
    setMemoryOpen(false);
    if (!keepDraft) setDraft('');
    window.requestAnimationFrame(() => input.current?.focus());
  }

  function show() {
    if (!threadId && !draft && !scopeChosen.current) setScope(currentCase ? `case:${currentCase.id}` : projectId ? `project:${projectId}` : 'all');
    setOpen(true); setHistory(false); setError(''); refresh();
  }

  async function send(question = draft) {
    if (!question.trim() || busy || waitingForHistory || !!readError || !index?.ready || (threadId && !thread)) return;
    const payload = { thread_id: threadId || undefined, question: question.trim(),
      ...(threadId ? {} : selectedScope.startsWith('case:') ? { case_id: selectedScope.slice(5) } : selectedScope.startsWith('project:') ? { project_id: selectedScope.slice(8) } : {}) };
    const fingerprint = JSON.stringify(payload);
    if (pendingRequest.current?.fingerprint !== fingerprint) pendingRequest.current = { fingerprint, id: randomChatId() };
    setSending(true); setError(''); setHistory(false); nearEnd.current = true;
    try {
      const result = await request<{ thread_id: string }>('/turns', { method: 'POST', body: JSON.stringify({ ...payload, request_id: pendingRequest.current.id }) });
      saveChatSelection({ threadId: result.thread_id, scope: selectedScope }, storageOwner);
      setThreadId(result.thread_id); setRestoring(false); setDraft(''); pendingRequest.current = null; refresh();
    } catch (e) { setError(message(e)); }
    finally { setSending(false); input.current?.focus(); }
  }

  async function stop() {
    if (!active) return;
    try { await request('/turns/' + active.id + '/cancel', { method: 'POST' }); refresh(); }
    catch (e) { setError(message(e)); }
  }

  async function visit(source: ChatSource) {
    setOpeningCase(source.case_id); setError('');
    try { await openCase(source.case_id); setOpen(false); }
    catch (e) { setError(message(e)); }
    finally { setOpeningCase(''); }
  }

  function submit(event: FormEvent) { event.preventDefault(); void send(); }
  const contextCase = thread?.case_id ? { id: thread.case_id, label: thread.context_label } : currentCase ? { id: currentCase.id, label: currentCase.platform_report_code || currentCase.short_code || currentCase.title } : null;
  const suggestions = selectedScope.startsWith('case:') ? ['这个案件目前进展如何？', '核对这个案件的照片和资料要求'] : ['目前有哪些项目？', '汇总查勘维修进展', '怎样查询案件编号？'];

  return <>
    <button ref={launcher} className={`workspace-chat-launcher ${unread ? 'has-unread' : ''}`} aria-label={unread ? '打开 AI 助手，有新答复' : '打开 AI 助手'} aria-haspopup="dialog" aria-expanded={open} aria-controls="workspace-chat" onClick={show}>
      {active ? <LoaderCircle size={21} className="spin" aria-hidden="true" /> : <MessageCircle size={21} aria-hidden="true" />}<span>AI 助手</span>{unread && <i aria-hidden="true" />}
    </button>
    <dialog ref={dialog} id="workspace-chat" className="workspace-chat" aria-labelledby="workspace-chat-title" onCancel={e => { e.preventDefault(); setOpen(false); }} onKeyDown={e => {
      if (e.key !== 'Tab') return;
      const controls = Array.from(e.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),a[href],summary')).filter(node => node.tabIndex >= 0 && node.getClientRects().length > 0);
      const first = controls[0], last = controls.at(-1);
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last?.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first?.focus(); }
    }} onClick={e => {
      if (e.target !== e.currentTarget) return;
      const rect = e.currentTarget.getBoundingClientRect();
      if (e.clientX < rect.left || e.clientX > rect.right || e.clientY < rect.top || e.clientY > rect.bottom) setOpen(false);
    }}>
      <header className="workspace-chat-head"><div className="workspace-chat-heading"><MessageCircle size={21} aria-hidden="true" /><div><h2 id="workspace-chat-title">工作台 AI 助手</h2><p>查案件 · 看进展 · 核对资料</p></div></div><div className="workspace-chat-actions">
        <button className="workspace-chat-history-toggle" aria-label="最近对话" title="最近对话" aria-expanded={history} onClick={() => { setMemoryOpen(false); setHistory(!history); }}><Clock3 size={18} aria-hidden="true" /><span>历史</span></button>
        <button aria-label="新建对话" title="新建对话" disabled={busy} onClick={() => newConversation()}><Plus size={19} aria-hidden="true" /></button>
        <button aria-label="收起 AI 助手" title="收起" onClick={() => setOpen(false)}><X size={20} aria-hidden="true" /></button>
      </div></header>
      <div className="workspace-chat-scope"><label htmlFor="workspace-chat-scope">查询范围</label><select id="workspace-chat-scope" value={selectedScope} disabled={busy || waitingForHistory || (loading && !thread)} onChange={e => { const stay = memoryOpen; newConversation(e.target.value, true); setMemoryOpen(stay); }}>
        <option value="all">全部项目</option>
        {contextCase && <option value={`case:${contextCase.id}`}>案件 · {contextCase.label}</option>}
        {projects.map(p => <option key={p.id} value={`project:${p.id}`}>{p.name}</option>)}
      </select><button className="workspace-memory-toggle" aria-label="AI 记忆" aria-pressed={memoryOpen} disabled={loading && !thread && !!threadId} onClick={() => { setMemoryOpen(!memoryOpen); setHistory(false); }}><BookOpen size={15} aria-hidden="true" />记忆</button></div>
      {memoryOpen ? <WorkspaceMemory key={threadId || selectedScope} request={request} threadId={threadId} scope={selectedScope} refreshToken={revision} back={() => setMemoryOpen(false)} changed={refresh} openCase={async caseId => { await openCase(caseId); setOpen(false); }} /> : history ? <div className="workspace-chat-history"><div className="workspace-chat-history-title"><strong>最近对话</strong><button onClick={() => setHistory(false)}>返回对话</button></div>
        {!index && !indexError && <p role="status">正在读取历史对话…</p>}
        {indexError ? <p>历史记录暂时无法读取，请重试。</p> : index && !index.threads.length ? <p>当前浏览器在此访问地址还没有对话记录。如果以前在其他网址聊天，请从原来的网址打开。</p> : index && <p>对话已保存在工作台，可选择后继续查看。</p>}
        {index?.threads.map(item => <button key={item.id} className={item.id === threadId ? 'is-current' : ''} onClick={() => { if (sending) return; selectThread(item.id, scopeOf(item)); setDraft(''); setHistory(false); setError(''); refresh(); }}><span><strong>{item.title}</strong><small>{item.context_label} · {new Date(item.updated_at).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}</small></span><ChevronRight size={16} aria-hidden="true" /></button>)}
      </div> : <div ref={transcript} className="workspace-chat-transcript" role="log" aria-label="AI 对话记录" aria-live="polite" aria-relevant="additions text" onScroll={e => { const node = e.currentTarget; nearEnd.current = node.scrollHeight - node.scrollTop - node.clientHeight < 90; }}>
        {waitingForHistory && !readError ? <p className="workspace-chat-loading" role="status"><LoaderCircle className="spin" size={18} aria-hidden="true" />正在恢复对话记录…</p> : readError && !thread ? <p className="workspace-chat-loading">对话记录暂时无法读取，请重新读取。</p> : !thread?.turns.length && <div className="workspace-chat-welcome"><span className="workspace-chat-mark"><MessageCircle size={28} aria-hidden="true" /></span><h3>今天需要了解什么？</h3><p>可以用案件号、楼栋房号来提问，也可以让我梳理项目进展和已归档资料。</p>{!!index?.threads.length && <button className="workspace-chat-resume" onClick={() => selectThread(index.threads[0].id, scopeOf(index.threads[0]))}>继续上次对话<ChevronRight size={16} aria-hidden="true" /></button>}<div className="workspace-chat-suggestions">{suggestions.map(text => <button key={text} disabled={busy || !index?.ready} onClick={() => void send(text)}>{text}<ChevronRight size={16} aria-hidden="true" /></button>)}</div></div>}
        {thread?.turns.map(turn => <section key={turn.id} className="workspace-chat-turn"><div className="workspace-chat-question"><span className="visually-hidden">你：</span>{turn.question}</div><div className="workspace-chat-answer"><span className="workspace-chat-author"><MessageCircle size={14} aria-hidden="true" />AI 助手</span>
          {ACTIVE.has(turn.state) ? <div className="workspace-chat-progress" role="status"><LoaderCircle size={16} className="spin" aria-hidden="true" />{turn.progress}…</div> : turn.answer ? <div className="workspace-chat-text">{turn.answer}</div> : <div className={`workspace-chat-outcome ${turn.state === 'failed' ? 'is-error' : ''}`}>{turn.error || '回答已停止'}</div>}
          {!!turn.sources.length && turn.state === 'completed' && <div className="workspace-chat-sources"><small>相关案件</small>{turn.sources.map(source => <button key={source.case_id} disabled={!!openingCase} onClick={() => void visit(source)}><FileText size={15} aria-hidden="true" /><span><strong>{source.label}</strong><small>{source.project_name}{source.location && ` · ${source.location}`}</small></span><ChevronRight size={15} aria-hidden="true" /></button>)}</div>}
          {!!turn.steps.length && <details className="workspace-chat-steps"><summary>{ACTIVE.has(turn.state) ? '正在处理' : `已执行 ${turn.steps.length} 个处理步骤`}</summary><ol>{turn.steps.map((step, n) => <li key={n}>{step.ok ? <Check size={13} aria-hidden="true" /> : <X size={13} aria-hidden="true" />}{step.label}{!step.ok && '（未执行成功）'}</li>)}</ol></details>}
          {['failed', 'cancelled'].includes(turn.state) && <button className="workspace-chat-retry" disabled={busy || !index?.ready} onClick={() => void send(turn.question)}>重新发送</button>}
        </div></section>)}
      </div>}
      {!memoryOpen && <div className="workspace-chat-footer">
        {(error || readError || recoveryNotice) && <div className="workspace-chat-error" role="alert">{error || readError || recoveryNotice}<button onClick={() => { setError(''); if (recoveryNotice) { unavailableThreads.current.clear(); setRecoveryNotice(''); setRestoring(true); } refresh(); }}>重新读取</button></div>}
        {!identity.persistent && <div className="workspace-chat-config" role="status">{identity.reloadable ? '浏览器仅允许当前标签页保存对话标识。关闭标签页后可能无法恢复历史，请允许此站点保存数据。' : '浏览器未允许保存对话标识。刷新后可能无法恢复历史，请允许此站点保存数据。'}</div>}
        {index && !index.ready && <div className="workspace-chat-config">尚未启用大模型。<button onClick={() => { setOpen(false); openSettings(); }}>前往配置</button></div>}
        {busyElsewhere && <div className="workspace-chat-config">另一段对话正在回答。<button onClick={() => { if (!index?.active) return; selectThread(index.active.thread_id); setHistory(false); refresh(); }}>查看进度</button></div>}
        <form onSubmit={submit}><label className="visually-hidden" htmlFor="workspace-chat-input">发送给 AI</label><textarea ref={input} id="workspace-chat-input" rows={2} maxLength={6000} value={draft} onChange={e => setDraft(e.target.value)} placeholder="输入案件号、房号，或直接提问…" onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing && e.keyCode !== 229) { e.preventDefault(); void send(); } }} />
          {active ? <button type="button" className="workspace-chat-send is-stop" aria-label="停止回答" title="停止回答" onClick={() => void stop()}><Square size={17} aria-hidden="true" /></button> : <button type="submit" className="workspace-chat-send" aria-label="发送问题" title="发送" disabled={!draft.trim() || busy || waitingForHistory || !!readError || !index?.ready || (loading && !thread)}>{sending ? <LoaderCircle size={19} className="spin" aria-hidden="true" /> : <ArrowUp size={21} aria-hidden="true" />}</button>}
        </form><p className="workspace-chat-hint">依据已归档资料回答 · 重要信息请核对<span>Enter 发送</span></p>
      </div>}
    </dialog>
  </>;
}
