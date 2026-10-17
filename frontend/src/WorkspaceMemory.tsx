import { useEffect, useRef, useState, type FormEvent } from 'react';
import { ArrowLeft, BookOpen, Check, Download, FileText, LoaderCircle, Pencil, Plus, Search, Trash2, X } from 'lucide-react';
import { message } from './api';
import './workspace-memory.css';

type Memory = {
  id: string; scope: 'browser' | 'member' | 'project' | 'case'; scope_label: string;
  project_id: string | null; case_id: string | null; project_name: string | null; case_label: string | null; group_name: string | null;
  kind: 'preference' | 'note' | 'todo' | 'fact'; title: string; content: string; state: 'candidate' | 'active' | 'stale';
  source_kind: string; source_id: string; source_quote: string; source_time: string;
  expires_at: string | null; revision: number; confirmed_at: string | null; updated_at: string; stale_reason: string;
  origin: { kind?: string; quote?: string; time?: string };
};
type SummaryItem = { text: string; sources: string[] };
type Summary = { scope_key: string; channel: 'workspace' | 'feishu'; conversation_mode?: 'workspace' | 'topic' | 'member' | 'isolated'; revision: number; source_count: number; updated_at: string; last_error: string | null;
  sections: { focus?: SummaryItem[]; agreements?: SummaryItem[]; pending?: SummaryItem[] };
  source_turns: { source: string; question: string; answer: string; created_at: string; speaker?: string }[];
};
type MemoryIndex = { items: Memory[]; total: number; truncated: boolean; summary: Summary | null;
  summary_options: { key: string; label: string; channel: 'workspace' | 'feishu' }[];
  projects: { id: string; name: string }[]; cases: { id: string; project_id: string; label: string }[]; scope: string };
type Editor = { id?: string; revision?: number; scope: string; project_id: string; case_id: string;
  kind: string; title: string; content: string; expires_days: number };
type Props = { request: <T>(path: string, options?: RequestInit) => Promise<T>; threadId: string; scope: string;
  refreshToken: number; back: () => void; openCase: (caseId: string) => Promise<void>; changed: () => void };

const KIND: Record<string, string> = { preference: '表达偏好', note: '事项约定', todo: '待办', fact: '台账快照' };
const STATE: Record<string, string> = { candidate: '待确认', active: '使用中', stale: '待复核' };
const SOURCE: Record<string, string> = { manual: '人工确认', workspace: '网页消息', feishu: '群内消息', case_snapshot: '本机台账' };
const SECTIONS = [['focus', '关注事项'], ['agreements', '对话约定'], ['pending', '未决事项']] as const;
const displayTime = (value: string | null | undefined) => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '未登记';

export default function WorkspaceMemory({ request, threadId, scope, refreshToken, back, openCase, changed }: Props) {
  const [data, setData] = useState<MemoryIndex | null>(null), [refresh, setRefresh] = useState(0);
  const [tab, setTab] = useState<'memory' | 'summary'>('memory'), [filter, setFilter] = useState('all');
  const [query, setQuery] = useState(''), [search, setSearch] = useState('');
  const [editor, setEditor] = useState<Editor | null>(null), [deleting, setDeleting] = useState('');
  const [busy, setBusy] = useState(''), [error, setError] = useState(''), [notice, setNotice] = useState('');
  const [summaryText, setSummaryText] = useState<string | null>(null), [clearSummary, setClearSummary] = useState(false);
  const [summaryKey, setSummaryKey] = useState(''), [managedSummary, setManagedSummary] = useState<Summary | null>(null);
  const errorBox = useRef<HTMLDivElement>(null), titleInput = useRef<HTMLInputElement>(null), summaryInput = useRef<HTMLTextAreaElement>(null);
  const pendingCreate = useRef<{ body: string; id: string } | null>(null);
  const params = new URLSearchParams(threadId ? { thread_id: threadId } : scope.startsWith('case:') ? { case_id: scope.slice(5) } : scope.startsWith('project:') ? { project_id: scope.slice(8) } : {});
  const suffix = params.size ? '?' + params.toString() : '';
  const selectedSummaryKey = data?.summary_options.some(item => item.key === summaryKey) ? summaryKey : data?.summary_options[0]?.key || '';
  const summary = selectedSummaryKey === data?.summary?.scope_key ? data.summary : managedSummary?.scope_key === selectedSummaryKey ? managedSummary : null;

  useEffect(() => {
    const abort = new AbortController();
    const args = new URLSearchParams(params); if (search) args.set('query', search);
    request<MemoryIndex>('/memory?' + args.toString(), { signal: abort.signal }).then(value => {
      if (!abort.signal.aborted) setData(value);
    }).catch(e => { if (!abort.signal.aborted) setError(message(e)); });
    return () => abort.abort();
  }, [request, suffix, search, refresh, refreshToken]);
  useEffect(() => {
    if (!selectedSummaryKey || selectedSummaryKey === data?.summary?.scope_key) return;
    const abort = new AbortController(); setManagedSummary(null);
    request<Summary>('/memory/summaries/' + encodeURIComponent(selectedSummaryKey) + suffix, { signal: abort.signal }).then(value => {
      if (!abort.signal.aborted) setManagedSummary(value);
    }).catch(e => { if (!abort.signal.aborted) setError(message(e)); });
    return () => abort.abort();
  }, [request, suffix, selectedSummaryKey, data?.summary?.scope_key, refresh, refreshToken]);
  useEffect(() => { if (editor) titleInput.current?.focus(); }, [editor?.id, !!editor]);
  useEffect(() => { if (summaryText !== null) summaryInput.current?.focus(); }, [summaryText === null]);
  useEffect(() => { if (error) errorBox.current?.focus(); }, [error]);

  function reload() { setRefresh(v => v + 1); changed(); }
  function begin(item?: Memory) {
    setError(''); setNotice(''); setDeleting(''); pendingCreate.current = null;
    const caseChoice = scope.startsWith('case:') ? data?.cases.find(c => c.id === scope.slice(5)) : undefined;
    setEditor(item ? { id: item.id, revision: item.revision, scope: item.scope, project_id: item.project_id || '', case_id: item.case_id || '',
      kind: item.kind === 'fact' ? 'note' : item.kind, title: item.title, content: item.content, expires_days: item.expires_at ? 90 : 0 }
      : { scope: 'browser', project_id: caseChoice?.project_id || (scope.startsWith('project:') ? scope.slice(8) : data?.projects[0]?.id || ''),
        case_id: caseChoice?.id || '', kind: 'preference', title: '', content: '', expires_days: 90 });
  }

  async function save(event: FormEvent) {
    event.preventDefault(); if (!editor || busy) return;
    setBusy('editor'); setError(''); setNotice('');
    const payload = { title: editor.title.trim(), content: editor.content.trim(), kind: editor.kind, expires_days: editor.expires_days };
    try {
      if (editor.id) {
        await request('/memory/' + editor.id + suffix, { method: 'PATCH', body: JSON.stringify({ ...payload, revision: editor.revision, action: 'edit' }) });
      } else {
        const body = JSON.stringify({ ...payload, scope: editor.scope, project_id: editor.scope === 'browser' ? undefined : editor.project_id,
          case_id: editor.scope === 'case' ? editor.case_id : undefined });
        if (pendingCreate.current?.body !== body) pendingCreate.current = { body, id: Array.from(crypto.getRandomValues(new Uint8Array(24)), v => v.toString(16).padStart(2, '0')).join('') };
        await request('/memory' + suffix, { method: 'POST', body: JSON.stringify({ ...JSON.parse(body), request_id: pendingCreate.current.id }) });
      }
      setEditor(null); pendingCreate.current = null; setNotice('记忆已保存并启用，下次提问会按适用范围参考。'); reload();
    } catch (e) { const text = message(e); setError(text.includes('其他操作修改') ? '这条记忆已有更新。当前输入已保留，请取消编辑后打开最新记录核对。' : text); }
    finally { setBusy(''); }
  }

  async function act(item: Memory, action: 'confirm' | 'delete') {
    if (busy) return; setBusy(item.id); setError(''); setNotice('');
    try {
      await request('/memory/' + item.id + suffix, { method: 'PATCH', body: JSON.stringify({ revision: item.revision, action, expires_days: 90 }) });
      setDeleting(''); setNotice(action === 'delete' ? '已删除，不再作为长期记忆引用；原始聊天记录保留。' : '已确认并启用，下次提问会参考这条记忆。'); reload();
    } catch (e) { setError(message(e)); }
    finally { setBusy(''); }
  }

  async function updateSummary(clear = false) {
    if (!summary || busy) return;
    setBusy('summary'); setError(''); setNotice('');
    try {
      const updated = await request<Summary>('/memory/summaries/' + encodeURIComponent(summary.scope_key) + suffix, { method: 'PATCH', body: JSON.stringify({ revision: summary.revision, clear, text: summaryText || '' }) });
      if (updated.scope_key === data?.summary?.scope_key) setData(previous => previous ? { ...previous, summary: updated } : previous);
      else setManagedSummary(updated);
      setSummaryText(null); setClearSummary(false); setNotice(clear ? '摘要已清空，此前对话不再自动带入；原始记录保留。' : '摘要已纠正，下次接续对话时生效。'); reload();
    } catch (e) { setError(message(e)); }
    finally { setBusy(''); }
  }

  async function download() {
    setBusy('export'); setError('');
    try {
      const value = await request<{ filename: string; markdown: string }>('/memory/export' + suffix);
      const url = URL.createObjectURL(new Blob([value.markdown], { type: 'text/markdown;charset=utf-8' }));
      const link = document.createElement('a'); link.href = url; link.download = value.filename; document.body.appendChild(link); link.click(); link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000); setNotice('已导出当前范围的记忆和摘要。');
    } catch (e) { setError(message(e)); }
    finally { setBusy(''); }
  }

  const visible = data?.items.filter(item => filter === 'all' || item.state === filter) || [];
  const hasSummary = summary && SECTIONS.some(([key]) => summary.sections[key]?.length);
  const sourceExcerpt = (item: SummaryItem) => summary?.source_turns.filter(s => item.sources.includes(s.source)) || [];

  return <div className="workspace-memory">
    <div className="workspace-memory-title"><div><BookOpen size={18} aria-hidden="true" /><h3>AI 记忆</h3></div><button onClick={back}><ArrowLeft size={15} aria-hidden="true" />返回对话</button></div>
    <p className="workspace-memory-intro">查看依据、纠正内容，决定哪些信息可以继续使用。这里只修改 AI 记忆，不改动项目台账。</p>
    <div className="workspace-memory-tabs" role="tablist" aria-label="记忆类型" onKeyDown={event => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault(); const next = event.key === 'Home' ? 'memory' : event.key === 'End' ? 'summary' : tab === 'memory' ? 'summary' : 'memory';
      setTab(next); setError(''); document.getElementById(next === 'memory' ? 'memory-notes-tab' : 'memory-summary-tab')?.focus();
    }}>
      <button id="memory-notes-tab" role="tab" tabIndex={tab === 'memory' ? 0 : -1} aria-controls="memory-notes-panel" aria-selected={tab === 'memory'} onClick={() => { setTab('memory'); setError(''); }}>长期记忆</button>
      <button id="memory-summary-tab" role="tab" tabIndex={tab === 'summary' ? 0 : -1} aria-controls="memory-summary-panel" aria-selected={tab === 'summary'} onClick={() => { setTab('summary'); setError(''); }}>对话摘要</button>
    </div>
    {error && <div className="workspace-memory-alert" ref={errorBox} tabIndex={-1} role="alert"><span>{error}</span><button onClick={() => { setError(''); setRefresh(v => v + 1); }}>重新读取</button></div>}
    {notice && <p className="workspace-memory-notice" role="status"><Check size={15} aria-hidden="true" />{notice}</p>}
    {!data ? <p className="workspace-memory-loading" role="status"><LoaderCircle className="spin" size={17} aria-hidden="true" />正在读取记忆…</p> : tab === 'memory' ? <div id="memory-notes-panel" role="tabpanel" aria-labelledby="memory-notes-tab">
      {editor ? <form className="workspace-memory-editor" onSubmit={save}>
        <div className="workspace-memory-editor-title"><strong>{editor.id ? '纠正记忆' : '添加记忆'}</strong><button type="button" aria-label="取消编辑记忆" disabled={!!busy} onClick={() => setEditor(null)}><X size={17} aria-hidden="true" /></button></div>
        <label htmlFor="memory-title">记忆名称<input ref={titleInput} id="memory-title" value={editor.title} maxLength={80} required onChange={e => setEditor({ ...editor, title: e.target.value })} placeholder="例如：项目进展的表达方式" /></label>
        {!editor.id ? <><label htmlFor="memory-scope">适用范围<select id="memory-scope" value={editor.scope} onChange={e => setEditor({ ...editor, scope: e.target.value, kind: e.target.value === 'browser' ? 'preference' : 'note' })}>
          <option value="browser">仅此浏览器的偏好</option><option value="project" disabled={!data.projects.length}>指定项目</option><option value="case" disabled={!data.cases.length}>指定案件</option>
        </select></label>
        {editor.scope !== 'browser' && <label htmlFor="memory-project">项目<select id="memory-project" value={editor.project_id} required onChange={e => setEditor({ ...editor, project_id: e.target.value, case_id: '' })}>
          <option value="" disabled>选择项目</option>{data.projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select></label>}
        {editor.scope === 'case' && <label htmlFor="memory-case">案件<select id="memory-case" value={editor.case_id} required onChange={e => setEditor({ ...editor, case_id: e.target.value })}>
          <option value="" disabled>选择案件</option>{data.cases.filter(c => c.project_id === editor.project_id).map(c => <option key={c.id} value={c.id}>{c.label}</option>)}
        </select></label>}</> : <p className="workspace-memory-help">适用范围保持原样。台账快照经人工纠正后会作为备注使用，业务状态仍需查询台账。</p>}
        <div className="workspace-memory-fields"><label htmlFor="memory-kind">类别<select id="memory-kind" value={editor.kind} disabled={['browser', 'member'].includes(editor.scope)} onChange={e => setEditor({ ...editor, kind: e.target.value })}>
          <option value="preference">表达偏好</option><option value="note">事项约定</option><option value="todo">待办</option>
        </select></label><label htmlFor="memory-expiry">有效期<select id="memory-expiry" value={editor.expires_days} onChange={e => setEditor({ ...editor, expires_days: Number(e.target.value) })}>
          <option value={7}>7天</option><option value={30}>30天</option><option value={90}>90天</option><option value={365}>1年</option><option value={0}>长期</option>
        </select></label></div>
        <label htmlFor="memory-content">记忆内容<textarea id="memory-content" value={editor.content} maxLength={1200} rows={5} required onChange={e => setEditor({ ...editor, content: e.target.value })} placeholder="写明已经核对的约定、需要跟进的事项或表达偏好" /></label>
        <p className="workspace-memory-help">项目和案件记忆可供授权渠道共同参考。保存表示你已核对这段内容；它不能授予业务操作权限。</p>
        <div className="workspace-memory-form-actions"><button type="button" disabled={!!busy} onClick={() => setEditor(null)}>取消</button><button type="submit" className="is-primary" disabled={!!busy}>{busy === 'editor' ? <LoaderCircle className="spin" size={16} aria-hidden="true" /> : <Check size={16} aria-hidden="true" />}保存并启用</button></div>
      </form> : <>
        <div className="workspace-memory-toolbar"><button className="is-primary" onClick={() => begin()} disabled={!!busy}><Plus size={16} aria-hidden="true" />添加记忆</button><button onClick={() => void download()} disabled={!!busy}><Download size={15} aria-hidden="true" />导出</button></div>
        <form className="workspace-memory-search" onSubmit={e => { e.preventDefault(); setSearch(query.trim()); }}><label className="visually-hidden" htmlFor="memory-search">搜索当前范围的记忆</label><input id="memory-search" maxLength={160} value={query} onChange={e => { setQuery(e.target.value); if (!e.target.value) setSearch(''); }} placeholder="搜索记忆内容或案件号" /><button type="submit" aria-label="搜索记忆"><Search size={17} aria-hidden="true" /></button></form>
        <div className="workspace-memory-filter"><label htmlFor="memory-state">显示</label><select id="memory-state" value={filter} onChange={e => setFilter(e.target.value)}><option value="all">全部状态</option><option value="candidate">待确认</option><option value="active">使用中</option><option value="stale">待复核</option></select><span>{visible.length} 条</span></div>
        {!visible.length && <div className="workspace-memory-empty"><BookOpen size={25} aria-hidden="true" /><strong>{search || filter !== 'all' ? '没有符合条件的记忆' : '当前范围还没有记忆'}</strong><p>可以手工添加，也可以在对话中说“记住这个约定”。AI 提出的内容会先等待你确认。</p></div>}
        {visible.map(item => <article className="workspace-memory-card" key={item.id} aria-label={item.title}>
          <div className="workspace-memory-card-top"><span className={`workspace-memory-state is-${item.state}`}>{STATE[item.state]}</span><span>{KIND[item.kind]}</span></div>
          <h4>{item.title}</h4><p className="workspace-memory-scope-label">{item.scope_label}</p><p className="workspace-memory-content">{item.content}</p>
          {item.stale_reason && <p className="workspace-memory-stale">{item.stale_reason}。复核前不会自动引用。</p>}
          {item.state === 'candidate' && <p className="workspace-memory-help">来自对话原话，尚未核验；确认后才会长期使用。</p>}
          <details className="workspace-memory-source"><summary>来源与有效期</summary><p>来源：{SOURCE[item.source_kind] || item.source_kind}{item.group_name ? ` · ${item.group_name}` : ''}</p><p>来源时间：{displayTime(item.source_time)}</p>{item.confirmed_at && <p>确认时间：{displayTime(item.confirmed_at)}</p>}<p>有效期：{item.expires_at ? displayTime(item.expires_at) : '长期'}</p>
            {item.source_quote && <blockquote>{item.source_quote}</blockquote>}{item.origin.kind && <><p>纠正前来源：{SOURCE[item.origin.kind] || item.origin.kind} · {displayTime(item.origin.time)}</p>{item.origin.quote && <blockquote>{item.origin.quote}</blockquote>}</>}
            {item.scope === 'member' && <p>这条偏好只用于原群内原发言人的对话；不会带入其他群成员的对话。</p>}
            {item.case_id && <button disabled={!!busy} onClick={() => void openCase(item.case_id!).catch(e => setError(message(e)))}><FileText size={14} aria-hidden="true" />查看关联案件</button>}
          </details>
          {deleting === item.id ? <div className="workspace-memory-delete"><p>删除这条记忆？它将停止使用，原始聊天记录会保留。</p><div><button disabled={!!busy} onClick={() => setDeleting('')}>保留</button><button className="is-danger" disabled={!!busy} onClick={() => void act(item, 'delete')}>确认删除</button></div></div> : <div className="workspace-memory-card-actions">
            {item.state !== 'active' && item.kind !== 'fact' && <button className="is-primary" disabled={!!busy} onClick={() => void act(item, 'confirm')}><Check size={15} aria-hidden="true" />{item.state === 'stale' ? '复核后启用' : '确认启用'}</button>}
            <button disabled={!!busy} onClick={() => begin(item)}><Pencil size={14} aria-hidden="true" />纠正</button><button className="memory-delete-button" aria-label={`删除记忆：${item.title}`} disabled={!!busy} onClick={() => setDeleting(item.id)}><Trash2 size={14} aria-hidden="true" />删除</button>
          </div>}
        </article>)}
        {data.truncated && <p className="workspace-memory-help">当前仅显示前200条，请缩小项目范围或搜索后查看。</p>}
        <p className="workspace-memory-boundary">{data.scope}</p>
      </>}
    </div> : <div id="memory-summary-panel" role="tabpanel" aria-labelledby="memory-summary-tab">
      <p className="workspace-memory-help">较早的问答会整理成摘要，近期原文继续保留在上下文中。摘要只帮助接续对话，业务现状仍需重新查询。</p>
      {!!data.summary_options.length && <label className="workspace-memory-summary-choice" htmlFor="memory-summary-scope">摘要范围<select id="memory-summary-scope" value={selectedSummaryKey} disabled={!!busy || summaryText !== null || clearSummary} onChange={e => { setSummaryKey(e.target.value); setError(''); setNotice(''); }}>
        {data.summary_options.map(item => <option value={item.key} key={item.key}>{item.label}</option>)}
      </select></label>}
      {!summary ? selectedSummaryKey ? <p className="workspace-memory-loading" role="status"><LoaderCircle className="spin" size={17} aria-hidden="true" />正在读取摘要…</p> : <div className="workspace-memory-empty"><BookOpen size={25} aria-hidden="true" /><strong>{threadId ? '继续对话后会自动整理' : '当前范围还没有对话摘要'}</strong><p>达到需要整理的历史长度后，这里会显示摘要及对应的来源问答。群摘要可在包含该群全部项目的查询范围中管理。</p></div> : <>
        {summary.channel === 'feishu' && <p className="workspace-memory-boundary">{summary.conversation_mode === 'topic' ? '这份摘要供原群内同一回复话题的成员共同接续。纠正或清空会影响该话题的后续对话；个人偏好仍只用于本人。' : summary.conversation_mode === 'isolated' ? '这条消息的回复标识未能核对，单独处理，不接续其他会话。' : '这份摘要只供原群内原发言人接续无话题对话，不带入其他成员或回复话题。'}</p>}
        <p className="workspace-memory-summary-meta">已整理 {summary.source_count} 轮 · {displayTime(summary.updated_at)}</p>
        {summary.last_error && <p className="workspace-memory-stale">{summary.last_error}。原始对话保留，后续提问会再次尝试整理。</p>}
        {summaryText !== null ? <form className="workspace-memory-editor" onSubmit={e => { e.preventDefault(); void updateSummary(); }}><label htmlFor="memory-summary-text">纠正对话摘要<textarea ref={summaryInput} id="memory-summary-text" rows={10} maxLength={1400} value={summaryText} required onChange={e => setSummaryText(e.target.value)} /></label><p className="workspace-memory-help">请写明仍有效的案件、已达成的约定和未解决事项。保存后会替换当前摘要。</p><div className="workspace-memory-form-actions"><button type="button" disabled={!!busy} onClick={() => setSummaryText(null)}>取消</button><button type="submit" className="is-primary" disabled={!!busy}>保存纠正</button></div></form> : <>
          {!hasSummary && <p className="workspace-memory-help">目前只需读取近期问答，尚无较早内容需要摘要。</p>}
          {SECTIONS.map(([key, label]) => !!summary.sections[key]?.length && <section className="workspace-memory-summary-section" key={key}><h4>{label}</h4>{summary.sections[key]!.map((item, n) => <div key={n}><p>{item.text}</p>{sourceExcerpt(item).length ? <details className="workspace-memory-source"><summary>查看来源问答（节选）</summary>{sourceExcerpt(item).map(source => <div className="workspace-memory-source-turn" key={source.source}><small>{displayTime(source.created_at)}</small><p><strong>{source.speaker || '用户'}：</strong>{source.question}</p>{source.answer && <p><strong>AI：</strong>{source.answer}</p>}</div>)}</details> : <small>来源：人工纠正或此前摘要</small>}</div>)}</section>)}
          <div className="workspace-memory-toolbar"><button disabled={!!busy} onClick={() => setSummaryText(SECTIONS.flatMap(([key, label]) => (summary.sections[key] || []).map(item => label + '：' + item.text)).join('\n'))}><Pencil size={15} aria-hidden="true" />纠正摘要</button><button disabled={!!busy} onClick={() => setClearSummary(true)}><Trash2 size={15} aria-hidden="true" />清空摘要</button></div>
          {clearSummary && <div className="workspace-memory-delete"><p>清空后，这份摘要对应的此前对话不再自动带入，原始聊天记录保留。</p><div><button disabled={!!busy} onClick={() => setClearSummary(false)}>取消</button><button className="is-danger" disabled={!!busy} onClick={() => void updateSummary(true)}>确认清空</button></div></div>}
        </>}
      </>}
    </div>}
  </div>;
}
