import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { ArrowDown, ArrowLeft, Bot, ChevronDown, ChevronRight, MessageSquare, Newspaper, Search, Users } from 'lucide-react';
import { api, message, queryString } from './api';
import { Empty, ErrorBox, Loading, useDebounce } from './common';
import { TASK_DELIVERY, TASK_KINDS, TASK_STATES, needsAttention, taskTitle, type FeishuTask } from './FeishuTaskHistory';
import './feishu-sessions.css';

interface Session {
  id: string; connector_id: string; chat_id: string; session_kind: 'group' | 'brief';
  title: string; subtitle: string; turn_count: number; member_count: number;
  attention_count: number; active_count: number; image_turns: number;
  first_at: string; last_at: string; latest_sequence: number; latest: FeishuTask;
}
interface SessionList { sessions: Session[]; total: number; counts: { group: number; brief: number }; offset: number; limit: number; has_more: boolean }
interface Turn extends FeishuTask { sequence: number; source_text: string; source_raw: string; reply_text: string; conversation_notice: string; photos: { id: string }[] }
interface Transcript { session: Session; turns: Turn[]; has_more: boolean; before: number; latest_sequence: number }
const sessionDate = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
const dayDate = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: 'long', day: 'numeric' });
const timeDate = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
function readableDate(value: string) { return sessionDate.format(new Date(value)); }

function SessionTranscript({ session, revision, selectTask, back }: { session: Session; revision: number; selectTask: (task: FeishuTask) => void; back: () => void }) {
  const [data, setData] = useState<Transcript | null>(null), [error, setError] = useState(''), [loadingOlder, setLoadingOlder] = useState(false);
  const [hasNew, setHasNew] = useState(false), [gap, setGap] = useState(false), [refresh, setRefresh] = useState(0);
  const viewport = useRef<HTMLDivElement>(null), dataRef = useRef<Transcript | null>(null), olderController = useRef<AbortController | null>(null);
  const scrollPlan = useRef<{ bottom?: boolean; height?: number; top?: number } | null>(null);
  const nearBottom = () => !viewport.current || viewport.current.scrollHeight - viewport.current.scrollTop - viewport.current.clientHeight < 100;
  useEffect(() => {
    const controller = new AbortController(); let inFlight = false;
    setLoadingOlder(false);
    async function load() {
      if (inFlight) return;
      inFlight = true;
      try {
        const value = await api<Transcript>(`/api/feishu-agent/sessions/${session.id}`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        const previous = dataRef.current;
        // Do not silently join two separated windows after a burst of messages.
        // Keep the user's reading position and offer an explicit latest view.
        if (previous && value.turns.length && previous.turns.length && !value.turns.some(turn => previous.turns.some(old => old.id === turn.id)) && value.turns[0].sequence > previous.latest_sequence) {
          setGap(true); setError(''); return;
        }
        const byId = new Map((previous?.turns || []).map(turn => [turn.id, turn]));
        value.turns.forEach(turn => byId.set(turn.id, turn));
        const merged = { ...value, turns: [...byId.values()].sort((a, b) => a.sequence - b.sequence),
          before: previous ? Math.min(previous.before, value.before) : value.before,
          has_more: previous && previous.before < value.before ? previous.has_more : value.has_more };
        const newTurn = previous && value.latest_sequence > previous.latest_sequence;
        if (!previous || nearBottom()) scrollPlan.current = { bottom: true };
        else if (newTurn) setHasNew(true);
        dataRef.current = merged; setData(merged); setError('');
      } catch (e) { if (!controller.signal.aborted) setError(message(e)); }
      finally { inFlight = false; }
    }
    void load(); const timer = window.setInterval(() => void load(), 5000);
    return () => { controller.abort(); window.clearInterval(timer); olderController.current?.abort(); };
  }, [session.id, revision, refresh]);
  useLayoutEffect(() => {
    const panel = viewport.current, plan = scrollPlan.current;
    if (!panel || !plan) return;
    panel.scrollTop = plan.bottom ? panel.scrollHeight : (plan.top || 0) + panel.scrollHeight - (plan.height || 0);
    scrollPlan.current = null;
  }, [data]);
  async function loadOlder() {
    const current = dataRef.current;
    if (!current || loadingOlder) return;
    const controller = new AbortController(); olderController.current = controller; setLoadingOlder(true); setError('');
    try {
      const older = await api<Transcript>(`/api/feishu-agent/sessions/${session.id}?before=${current.before}`, { signal: controller.signal });
      if (controller.signal.aborted) return;
      // Merge against the latest state, because a poll may have completed while
      // the older-page request was in flight.
      const latest = dataRef.current!;
      const byId = new Map(older.turns.map(turn => [turn.id, turn])); latest.turns.forEach(turn => byId.set(turn.id, turn));
      if (viewport.current) scrollPlan.current = { height: viewport.current.scrollHeight, top: viewport.current.scrollTop };
      const merged = { ...latest, turns: [...byId.values()].sort((a, b) => a.sequence - b.sequence), before: older.before, has_more: older.has_more };
      dataRef.current = merged; setData(merged);
    } catch (e) { if (!controller.signal.aborted) setError(message(e)); }
    finally { if (!controller.signal.aborted) setLoadingOlder(false); }
  }
  function latest() {
    setHasNew(false);
    if (gap) { dataRef.current = null; setData(null); setGap(false); setRefresh(value => value + 1); }
    else if (viewport.current) viewport.current.scrollTo({ top: viewport.current.scrollHeight, behavior: 'instant' });
  }
  const info = data?.session || session;
  return <section className="feishu-session-detail" aria-label={`${session.title}会话内容`}>
    <header className="feishu-session-detail-heading"><button className="feishu-session-back" onClick={back}><ArrowLeft size={16} aria-hidden="true" />会话列表</button><div><h4>{info.title}</h4><p>{info.subtitle} · {info.turn_count} 轮记录{info.member_count ? ` · ${info.member_count} 位发言成员` : ''}</p></div><details className="feishu-session-info"><summary>会话信息<ChevronDown size={13} aria-hidden="true" /></summary><div><strong>Session</strong><code>{info.id}</code><span>开始于 {readableDate(info.first_at)}</span><span>最近更新 {readableDate(info.last_at)}</span><span>按群归档显示，问答上下文仍按成员和回复话题接续。</span></div></details></header>
    <ErrorBox error={error} />
    {!data && !error && <Loading />}
    <div className="feishu-session-messages" ref={viewport} tabIndex={0} role="region" aria-label="会话消息时间线" onScroll={() => { if (nearBottom()) setHasNew(false); }}>
      {data && <>
        <div className="feishu-session-history-edge">{data.has_more ? <button disabled={loadingOlder} onClick={() => void loadOlder()}>{loadingOlder ? '正在加载…' : '加载更早记录'}</button> : <span>会话记录起点</span>}<small>已显示 {data.turns.length} / {data.session.turn_count} 轮 · 北京时间</small></div>
        {data.turns.map((turn, index) => {
          const date = dayDate.format(new Date(turn.created_at));
          const newDate = !index || dayDate.format(new Date(data.turns[index - 1].created_at)) !== date;
          return <div className="feishu-session-turn" key={turn.id} data-turn-id={turn.id}>
            {newDate && <h5 className="feishu-session-day">{date}</h5>}
            {index > 0 && turn.binding_revision !== data.turns[index - 1].binding_revision && <p className="feishu-session-boundary">群助手配置已更新，后续记录按新的配置处理</p>}
            {turn.kind !== 'brief' && <article className="feishu-session-message from-member"><header><strong>{turn.sender_label || '群成员'}</strong><time dateTime={turn.created_at}>{timeDate.format(new Date(turn.created_at))}</time>{turn.conversation_mode === 'topic' && <span title={turn.conversation_key}>回复话题</span>}{turn.conversation_mode === 'isolated' && <span>独立消息</span>}</header><div className="feishu-message-bubble"><p>{turn.source_text || taskTitle(turn)}</p>{!!turn.photos.length && <div className="feishu-session-photos">{turn.photos.map((photo, number) => <a key={photo.id} href={`/api/photos/${photo.id}/original`} target="_blank" rel="noreferrer"><img src={`/api/photos/${photo.id}/thumbnail`} alt={`来源图片 ${number + 1}`} loading="lazy" /></a>)}</div>}</div></article>}
            <article className={`feishu-session-message from-assistant ${needsAttention(turn) ? 'needs-attention' : ''}`}><header><Bot size={16} aria-hidden="true" /><strong>{turn.kind === 'brief' ? '每日简报' : '群助手'}</strong><span>{TASK_KINDS[turn.kind]}</span><time dateTime={turn.finished_at || turn.created_at}>{timeDate.format(new Date(turn.finished_at || turn.created_at))}</time></header><div className="feishu-message-bubble"><p>{turn.reply_text || (['queued', 'running'].includes(turn.state) ? '正在处理，等待助手答复…' : turn.error || '本次未生成答复，请查看处理详情。')}</p>{turn.conversation_notice && <small className="feishu-session-notice">{turn.conversation_notice}</small>}</div><footer><span className={`feishu-task-state state-${turn.state}`}>{TASK_STATES[turn.state] || turn.state}</span><span className={['failed', 'unknown'].includes(turn.delivery_state || '') ? 'feishu-task-error' : ''}>{turn.delivery_state ? TASK_DELIVERY[turn.delivery_state] || turn.delivery_state : '尚无群内回复'}</span><button onClick={() => selectTask(turn)}>处理详情<ChevronRight size={14} aria-hidden="true" /></button></footer>{(turn.error || turn.delivery_error) && <p className="feishu-task-error">{turn.error || turn.delivery_error}</p>}</article>
          </div>;
        })}
      </>}
    </div>
    {(hasNew || gap) && <button className="feishu-session-new" onClick={latest}><ArrowDown size={16} aria-hidden="true" />{gap ? '有较多新记录，查看最新消息' : '有新消息，查看最新'}</button>}
    <div className="feishu-session-footer"><span>仅显示已触发助手的记录，普通群聊不在此列表中。</span><button onClick={latest}><ArrowDown size={15} aria-hidden="true" />回到最新</button></div>
  </section>;
}

export default function FeishuSessionHistory({ revision, selectTask }: { revision: number; selectTask: (task: FeishuTask) => void }) {
  const [kind, setKind] = useState('group'), [query, setQuery] = useState(''), [attention, setAttention] = useState(false), [offset, setOffset] = useState(0);
  const search = useDebounce(query, 250);
  const [data, setData] = useState<SessionList | null>(null), [error, setError] = useState(''), [selected, setSelected] = useState(''), [reading, setReading] = useState(false);
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    const controller = new AbortController(); let inFlight = false;
    setLoading(true);
    async function load() {
      if (inFlight) return;
      inFlight = true;
      try {
        const value = await api<SessionList>(`/api/feishu-agent/sessions?${queryString({ q: search, kind, attention, offset })}`, { signal: controller.signal });
        if (!controller.signal.aborted) { setData(value); setError(''); setSelected(current => value.sessions.some(session => session.id === current) ? current : value.sessions[0]?.id || ''); }
      } catch (e) { if (!controller.signal.aborted) setError(message(e)); }
      finally { inFlight = false; if (!controller.signal.aborted) setLoading(false); }
    }
    void load(); const timer = window.setInterval(() => void load(), 5000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [search, kind, attention, offset, revision]);
  const activeSession = data?.sessions.find(session => session.id === selected);
  return <section className="feishu-sessions" aria-labelledby="feishu-sessions-heading">
    <header className="feishu-sessions-heading"><div><h3 id="feishu-sessions-heading">会话记录 <span>Sessions</span></h3><p>同一个群的问答和图片处理集中查看</p></div><div className="feishu-session-tabs" role="group" aria-label="会话类型">{[['group', '群会话'], ['brief', '每日简报']].map(([value, label]) => <button key={value} aria-pressed={kind === value} onClick={() => { setKind(value); setOffset(0); setReading(false); }}>{label}{data && <span>{data.counts[value as 'group' | 'brief']}</span>}</button>)}</div></header>
    <div className="feishu-session-filterbar"><label><Search size={16} aria-hidden="true" /><input aria-label="搜索会话" type="search" value={query} onChange={event => { setQuery(event.target.value); setOffset(0); setReading(false); }} placeholder="搜索群名、项目或会话内容" /></label><button aria-pressed={attention} onClick={() => { setAttention(value => !value); setOffset(0); setReading(false); }}>只看待核对</button><span role="status">{loading ? '正在读取…' : `${data?.total || 0} 个会话`}</span></div>
    <ErrorBox error={error} />
    {!data && !error && <Loading />}
    {data && (data.sessions.length ? <div className={`feishu-sessions-layout ${reading ? 'is-reading' : ''}`}>
      <aside className="feishu-session-list-pane"><nav aria-label="群会话列表">{data.sessions.map(session => {
        const Icon = session.session_kind === 'brief' ? Newspaper : MessageSquare;
        return <button className="feishu-session-item" key={session.id} aria-pressed={selected === session.id} onClick={() => { setSelected(session.id); setReading(true); }} aria-label={`打开会话：${session.title}${session.session_kind === 'brief' ? ` · ${session.subtitle}` : ''}`}><span className="feishu-session-list-title"><Icon size={17} aria-hidden="true" /><strong>{session.title}</strong></span><span className="feishu-session-list-subtitle">{session.subtitle}</span><span className="feishu-session-list-preview">{session.latest.source_excerpt || session.latest.reply_excerpt || '等待处理结果'}</span><span className="feishu-session-list-meta"><span><Users size={12} aria-hidden="true" />{session.turn_count} 轮{session.member_count ? ` · ${session.member_count} 人` : ''}</span><time>{readableDate(session.last_at)}</time></span>{!!(session.attention_count || session.active_count) && <span className="feishu-session-list-flags">{!!session.attention_count && <span>待核对 {session.attention_count}</span>}{!!session.active_count && <span>处理中 {session.active_count}</span>}</span>}</button>;
      })}</nav>{(offset > 0 || data.has_more) && <div className="feishu-session-list-pages"><button disabled={!offset} onClick={() => { setOffset(Math.max(0, offset - 20)); setReading(false); }}>上一页</button><button disabled={!data.has_more} onClick={() => { setOffset(offset + 20); setReading(false); }}>下一页</button></div>}</aside>
      {activeSession && <SessionTranscript key={activeSession.id} session={activeSession} revision={revision} selectTask={selectTask} back={() => setReading(false)} />}
    </div> : <Empty action={query || attention ? <button onClick={() => { setQuery(''); setAttention(false); setOffset(0); }}>清空会话筛选</button> : undefined}>{query || attention ? '没有符合条件的会话。' : kind === 'group' ? '新的群内问答和图片处理会按群汇总在这里。' : '尚无每日简报会话。'}</Empty>)}
  </section>;
}
