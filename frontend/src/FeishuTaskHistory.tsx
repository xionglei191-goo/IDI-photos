import { useState } from 'react';
import { ChevronRight, Image, MessageSquare, Newspaper, Search } from 'lucide-react';
import { Empty } from './common';

export interface FeishuTask {
  id: string; kind: string; state: string; project_id: string | null; project_name?: string;
  connector_id?: string; connector_name?: string; chat_name?: string;
  error: string | null; created_at: string; finished_at: string | null;
  delivery_state?: string | null; delivery_error?: string | null;
  source_excerpt?: string; reply_excerpt?: string; sender_label?: string; sender_name_known?: boolean;
  conversation_mode?: string; image_count?: number; brief_date?: string; mention_only?: boolean;
  sequence?: number; chat_id?: string; binding_revision?: number; conversation_key?: string; model?: string;
}
export const TASK_STATES: Record<string, string> = { queued: '等待处理', running: '处理中', completed: '处理完成', partial: '部分完成，待核对', failed: '处理失败', cancelled: '已停止', missed: '已错过发送时段' };
export const TASK_DELIVERY: Record<string, string> = { queued: '等待群内发送', sending: '正在发送', sent: '已发到群', failed: '群消息发送失败', unknown: '送达情况待核对', cancelled: '群消息已停止' };
export const TASK_KINDS: Record<string, string> = { chat: '群对话', images: '图片整理', brief: '每日简报' };
const icons = { chat: MessageSquare, images: Image, brief: Newspaper };
const PAGE_SIZE = 12;
const chinaDay = new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' });
const chinaTime = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
function day(value: string) { return chinaDay.format(new Date(value)); }
function dateLabel(value: string) {
  const today = new Date();
  const yesterday = new Date(today.getTime() - 86400000);
  return `${value === day(today.toISOString()) ? '今天 · ' : value === day(yesterday.toISOString()) ? '昨天 · ' : ''}${value.replaceAll('-', '/')}`;
}
export function needsAttention(task: FeishuTask) {
  return ['partial', 'failed', 'missed'].includes(task.state) || ['failed', 'unknown'].includes(task.delivery_state || '');
}
function inProgress(task: FeishuTask) { return ['queued', 'running'].includes(task.state) || ['queued', 'sending'].includes(task.delivery_state || ''); }
export function taskTitle(task: FeishuTask) {
  if (task.kind === 'brief') return `${task.project_name || '项目'} · ${task.brief_date || day(task.created_at)} 简报`;
  return task.source_excerpt || (task.image_count ? `收到 ${task.image_count} 张图片，未附文字说明` : task.mention_only ? '仅 @ 机器人，接续上下文' : '未记录文字问题，查看处理详情');
}
export function FeishuTaskHistory({ tasks, select }: { tasks: FeishuTask[]; select: (id: string) => void }) {
  const [kind, setKind] = useState(''), [query, setQuery] = useState(''), [group, setGroup] = useState(''), [state, setState] = useState('');
  const [page, setPage] = useState(0);
  const groups = new Map<string, string>();
  for (const task of tasks) if (task.connector_id) groups.set(task.connector_id, task.chat_name || task.connector_name || '未命名群组');
  const terms = query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  const matching = tasks.filter(task => {
    const text = [task.source_excerpt, task.reply_excerpt, task.chat_name, task.connector_name, task.project_name, task.sender_label, task.error, task.delivery_error].join(' ').toLocaleLowerCase();
    return (!group || task.connector_id === group) && terms.every(term => text.includes(term))
      && (!state || (state === 'attention' ? needsAttention(task) : state === 'running' ? inProgress(task) : task.state === 'completed' && task.delivery_state === 'sent'));
  });
  const filtered = matching.filter(task => !kind || task.kind === kind);
  const currentPage = Math.min(page, Math.max(0, Math.ceil(filtered.length / PAGE_SIZE) - 1));
  const visible = filtered.slice(currentPage * PAGE_SIZE, (currentPage + 1) * PAGE_SIZE);
  const sections = new Map<string, FeishuTask[]>();
  for (const task of visible) { const key = day(task.created_at); sections.set(key, [...(sections.get(key) || []), task]); }
  const active = !!(kind || query || group || state);
  function clear() { setKind(''); setQuery(''); setGroup(''); setState(''); setPage(0); }
  return <section className="feishu-agent-activity" aria-labelledby="feishu-history-heading">
    <header><div><h3 id="feishu-history-heading">处理记录 <span className="feishu-history-total">{tasks.length}</span></h3><p>最近 60 条 · 按时间倒序 · 北京时间</p></div>
      {!!tasks.filter(needsAttention).length && <button className="feishu-attention-filter" aria-pressed={state === 'attention'} onClick={() => { setKind(''); setQuery(''); setGroup(''); setState(state === 'attention' ? '' : 'attention'); setPage(0); }}>待核对 {tasks.filter(needsAttention).length}</button>}
    </header>
    <div className="feishu-history-tools">
      <div className="feishu-history-kinds" role="group" aria-label="记录类型">{[['', '全部'], ...Object.entries(TASK_KINDS)].map(([key, label]) => <button key={key} aria-pressed={kind === key} onClick={() => { setKind(key); setPage(0); }}>{label}<span>{key ? matching.filter(task => task.kind === key).length : matching.length}</span></button>)}</div>
      <div className="feishu-history-filters">
        <label className="feishu-history-search">搜索记录<div><Search size={16} aria-hidden="true" /><input type="search" value={query} onChange={e => { setQuery(e.target.value); setPage(0); }} placeholder="问题、回复摘要、案件号或成员" /></div></label>
        <label>来源群组<select value={group} onChange={e => { setGroup(e.target.value); setPage(0); }}><option value="">全部群组</option>{[...groups].map(([id, name]) => <option key={id} value={id}>{name}</option>)}</select></label>
        <label>处理情况<select value={state} onChange={e => { setState(e.target.value); setPage(0); }}><option value="">全部状态</option><option value="attention">待核对 / 异常</option><option value="running">处理中 / 待发送</option><option value="completed">处理完成且已发群</option></select></label>
      </div>
      <div className="feishu-history-scope"><span role="status">{active ? `筛选出 ${filtered.length} 条` : `共 ${tasks.length} 条记录`} · 搜索范围为最近 60 条的问题与回复摘要</span>{active && <button onClick={clear}>清空筛选</button>}</div>
    </div>
    {visible.length ? <div className="feishu-history-days">{[...sections].map(([date, items]) => <section key={date} className="feishu-history-day" aria-label={dateLabel(date)}><h4>{dateLabel(date)}</h4><div className="feishu-agent-task-list">{items.map(task => {
      const Icon = icons[task.kind as keyof typeof icons] || MessageSquare;
      return <button key={task.id} className={`feishu-agent-task ${needsAttention(task) ? 'needs-attention' : ''}`} onClick={() => select(task.id)} aria-label={`查看${TASK_KINDS[task.kind] || '处理'}记录：${taskTitle(task)}`}>
        <span className={`feishu-task-icon kind-${task.kind}`}><Icon size={18} aria-hidden="true" /></span>
        <span className="feishu-task-content"><span className="feishu-task-meta"><span>{TASK_KINDS[task.kind]}</span><span>{task.chat_name || task.connector_name || task.project_name}</span><span title={task.kind === 'brief' || task.sender_name_known ? undefined : '历史记录未保存姓名，此标识用于区分发言成员'}>{task.sender_label || '未记录发言人'}</span>{task.conversation_mode === 'topic' && <span>回复话题</span>}{!!task.image_count && <span>{task.image_count} 张图片</span>}</span>
          <strong className="feishu-task-title">{taskTitle(task)}</strong>
          <span className="feishu-task-reply">{task.reply_excerpt ? `回复：${task.reply_excerpt}` : task.error || (inProgress(task) ? '等待生成处理结果…' : '暂无答复，打开记录查看原因')}</span>
          {(task.error || task.delivery_error) && task.reply_excerpt && <span className="feishu-task-error">{task.error || task.delivery_error}</span>}
        </span>
        <span className="feishu-task-status"><time dateTime={task.created_at}>{chinaTime.format(new Date(task.created_at))}</time><span className={`feishu-task-state state-${task.state}`}>{TASK_STATES[task.state] || task.state}</span><span className={['failed', 'unknown'].includes(task.delivery_state || '') ? 'feishu-task-error' : ''}>{task.delivery_state ? TASK_DELIVERY[task.delivery_state] || task.delivery_state : '尚无群内回复'}</span></span>
        <ChevronRight className="feishu-task-chevron" size={18} aria-hidden="true" />
      </button>;
    })}</div></section>)}</div> : <Empty action={active ? <button onClick={clear}>清空筛选</button> : undefined}>{active ? '没有符合条件的记录，试试其他关键词或筛选条件。' : '开启群助手后，新的群内 @ 消息和图片将在这里显示。'}</Empty>}
    {filtered.length > PAGE_SIZE && <nav className="feishu-history-pages" aria-label="处理记录翻页"><span>第 {currentPage + 1} / {Math.ceil(filtered.length / PAGE_SIZE)} 页 · 每页 {PAGE_SIZE} 条</span><div><button disabled={!currentPage} onClick={() => setPage(currentPage - 1)}>上一页</button><button disabled={(currentPage + 1) * PAGE_SIZE >= filtered.length} onClick={() => setPage(currentPage + 1)}>下一页</button></div></nav>}
  </section>;
}
