import usePermissions from './usePermissions';
import { useEffect, useState } from 'react';
import { Bot, CalendarClock, FileCheck2, MessageSquare, RefreshCw, Settings, UserRound } from 'lucide-react';
import { api, message, post, put } from './api';
import { dateTime, Empty, ErrorBox, Loading, Modal } from './common';
import type { Case, Notify } from './types';
import type { Contact } from './ProjectFollowup';
import { FeishuTaskHistory, TASK_STATES as STATES, TASK_DELIVERY as DELIVERY, TASK_KINDS as KINDS, type FeishuTask as Task } from './FeishuTaskHistory';
import FeishuSessionHistory from './FeishuSessionHistory';
import './feishu-agent.css';

interface ProjectOption { id: string; name: string; case_count?: number }
interface BindingConfig { ai_enabled: number; enabled: number; chat_enabled: number; images_enabled: number; save_drafts: number; daily_brief: number; brief_time: string; activated_at: string; project_ids: string[] }
interface Binding { connector_id: string; connector_name: string; chat_name: string; project_id: string | null; project_ids: string[]; projects: (ProjectOption & { contact: Contact | null })[]; project_name: string; config: BindingConfig | null; contact: Contact | null; ready: boolean; error: string; next_brief_at: string | null }
interface TaskDetail extends Task { input: { text: string; total_images?: number; source_time?: string; conversation?: { mode: 'topic' | 'member' | 'isolated'; notice?: string } }; result: { text?: string }; usage: { requests?: number }; model: string; images: { photo_id: string; state: string; summary: string; suggestion: { reason?: string } }[]; drafts: { case_id: string; title: string; platform_report_code?: string; short_code?: string; result: { state: string; reason?: string; issues?: string[]; uploaded_count?: number } }[]; steps: { step: number; tool: string; outcome: string; created_at: string }[]; delivery: { state: string; text: string; error: string | null; remote_message_id: string | null } | null }
interface Status { bindings: Binding[]; projects: ProjectOption[]; model_ready: boolean; platform: { connected: boolean; write_enabled: boolean }; worker_enabled: boolean; tasks: Task[] }
interface Editable { ai_enabled: boolean; enabled: boolean; chat_enabled: boolean; images_enabled: boolean; save_drafts: boolean; daily_brief: boolean; brief_time: string; project_ids: string[] }
const DRAFTS: Record<string, string> = { local_draft: '本地草稿', save_verified: '官网草稿已保存并回读', saved: '官网已保存，待回读', uncertain: '官网结果未知，先回读核对', awaiting_login: '待连接官网', needs_fields: '待补全字段', read_only: '官网只读', unsupported_stage: '当前环节无草稿保存动作', failed: '保存失败', stale: '资料已变化', submitting: '保存过程待核对', stage_mismatch: '图片用途与当前环节不匹配，待核对', already_saved: '已在官网保存，未重复上传' };
const TOOLS: Record<string, string> = { list_projects: '读取项目范围', read_project_summary: '读取项目进展', search_cases: '查询案件', read_case: '读取案件与资料', read_case_records: '读取办理记录', read_requirements: '读取资料要求', read_images: '查看图片', organize_image: '分析与整理图片', prepare_official_draft: '准备本地草稿', save_official_draft: '保存官网草稿', finish_reply: '生成答复' };

function ConfigDialog({ binding, projects, close, saved }: { binding: Binding; projects: ProjectOption[]; close: () => void; saved: () => void }) {
  const old = binding.config;
  const [value, setValue] = useState<Editable>({ ai_enabled: old ? !!old.ai_enabled : true, enabled: !!old?.enabled, chat_enabled: old ? !!old.chat_enabled : true, images_enabled: old ? !!old.images_enabled : true, save_drafts: old ? !!old.save_drafts : true, daily_brief: old ? !!old.daily_brief : true, brief_time: old?.brief_time || '18:00', project_ids: old?.project_ids || binding.project_ids });
  const [filter, setFilter] = useState('');
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  async function save() { setBusy(true); setError(''); try { await put(`/api/feishu-agent/bindings/${binding.connector_id}`, value); saved(); close(); } catch (e) { setError(message(e)); } finally { setBusy(false); } }
  return <Modal title={`群助手设置 · ${binding.chat_name}`} close={close} footer={<><button disabled={busy} onClick={close}>取消</button><button className="primary" disabled={busy || (value.enabled && !value.project_ids.length)} onClick={() => void save()}>{busy ? '保存中…' : '保存设置'}</button></>}><div className="followup-form feishu-agent-form">
    <ErrorBox error={error} />
    <fieldset className="feishu-agent-scope"><legend>本群可查询和整理的项目 · 已选 {value.project_ids.length} 个</legend><label>筛选项目<input value={filter} onChange={e => setFilter(e.target.value)} placeholder="输入项目名称" /></label><div className="followup-actions"><button onClick={() => setValue({ ...value, project_ids: projects.map(p => p.id) })}>选择当前全部项目</button><button onClick={() => setValue({ ...value, project_ids: [] })}>清空选择</button></div><div className="feishu-agent-scope-options">{projects.filter(p => p.name.includes(filter.trim())).map(p => <label className="check" key={p.id}><input type="checkbox" checked={value.project_ids.includes(p.id)} onChange={e => setValue({ ...value, project_ids: e.target.checked ? [...value.project_ids, p.id] : value.project_ids.filter(id => id !== p.id) })} /><span>{p.name}<small>{p.case_count ?? 0} 个案件</small></span></label>)}</div></fieldset>
    <label className="check"><input type="checkbox" checked={value.enabled} onChange={e => setValue({ ...value, enabled: e.target.checked })} />启用本群 Agent 助手</label>
    <label className="check"><input type="checkbox" checked={value.ai_enabled} onChange={e => setValue({ ...value, ai_enabled: e.target.checked })} />启用 AI 分析与对话（关闭后保留规则整理和统计日报）</label>
    <label className="check"><input type="checkbox" checked={value.chat_enabled} onChange={e => setValue({ ...value, chat_enabled: e.target.checked })} />群成员 @ 机器人时对话、查询所选项目案件</label>
    <label className="check"><input type="checkbox" checked={value.images_enabled} onChange={e => setValue({ ...value, images_enabled: e.target.checked })} />按规则整理新图片，启用 AI 时补充内容分析</label>
    <label className="check"><input type="checkbox" checked={value.save_drafts} onChange={e => setValue({ ...value, save_drafts: e.target.checked })} />资料明确且齐备时，上传并保存官网草稿</label>
    <p className="muted">支持查勘、修复成果草稿。案件号不清楚、识别冲突或业务字段缺失时留待核对；正式提交始终由用户在案件页确认。</p>
    <label className="check"><input type="checkbox" checked={value.daily_brief} onChange={e => setValue({ ...value, daily_brief: e.target.checked })} />每天在本群 @ 已绑定负责人发送项目简报</label>
    <label>简报时间（北京时间）<input type="time" value={value.brief_time} onChange={e => setValue({ ...value, brief_time: e.target.value })} required /></label>
    <p className="muted">日报按各项目负责人已绑定的群发送。同一项目每日一份，未绑定负责人的项目暂不发送。</p>
    <div className="feishu-agent-note">设置保存后从新消息开始处理。图片和相关案件资料会发送给已配置的模型。日报需要本机服务在线；错过时段会保留记录，过期日报不补发。</div>
  </div></Modal>;
}

export default function FeishuAgentPanel({ cases, notify, openCase, openSettings, onContact }: { cases: Case[]; notify: Notify; openCase: (value: Case) => void; openSettings: () => void; onContact: (projectId: string, contact: Contact | null) => void }) {
  const [status, setStatus] = useState<Status | null>(null), [error, setError] = useState(''), [busy, setBusy] = useState('');
  const { isAdmin } = usePermissions();
  const [editing, setEditing] = useState<Binding | null>(null), [selected, setSelected] = useState(''), [detail, setDetail] = useState<TaskDetail | null>(null), [detailError, setDetailError] = useState('');
  const [preview, setPreview] = useState<{ text: string } | null>(null), [revision, setRevision] = useState(0);
  const [historyMode, setHistoryMode] = useState<'sessions' | 'records'>('sessions'), [selectedRecord, setSelectedRecord] = useState<Task | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    async function load() { try { const data = await api<Status>('/api/feishu-agent', { signal: controller.signal }); if (!controller.signal.aborted) { setStatus(data); setError(''); } } catch (e) { if (!controller.signal.aborted) setError(message(e)); } }
    void load(); const timer = window.setInterval(() => void load(), 5000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [revision]);
  useEffect(() => {
    if (!selected) { setDetail(null); return; }
    const controller = new AbortController(); setDetail(null); setDetailError('');
    async function load() { try { const data = await api<TaskDetail>(`/api/feishu-agent/tasks/${selected}`, { signal: controller.signal }); if (!controller.signal.aborted) { setDetail(data); setDetailError(''); } } catch (e) { if (!controller.signal.aborted) setDetailError(message(e)); } }
    void load(); const timer = window.setInterval(() => void load(), 5000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [selected, revision]);
  async function action(key: string, work: () => Promise<unknown>, success: string) { setBusy(key); setDetailError(''); setError(''); try { await work(); setRevision(v => v + 1); notify(success); } catch (e) { if (selected) setDetailError(message(e)); else setError(message(e)); } finally { setBusy(''); } }
  async function previewBrief(projectId: string) { setBusy(projectId); setError(''); try { setPreview(await api<{ text: string }>(`/api/feishu-agent/projects/${projectId}/brief-preview`)); } catch (e) { setError(message(e)); } finally { setBusy(''); } }
  async function openDraft(caseId: string) { setBusy('case'); setDetailError(''); try { const value = cases.find(c => c.id === caseId) || await api<Case>(`/api/cases/${caseId}`); openCase(value); } catch (e) { setDetailError(message(e)); } finally { setBusy(''); } }
  const nextTime = (value: string) => new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(value));
  const record = status?.tasks.find(task => task.id === selected) || selectedRecord;
  return <div className="feishu-agent">
    <header className="feishu-agent-heading"><div><h2><Bot size={22} aria-hidden="true" />飞书群助手</h2><p>群内查询案件、整理收图，并为负责人提供每日进展。</p></div><button onClick={() => setRevision(v => v + 1)}><RefreshCw size={16} aria-hidden="true" />刷新</button></header>
    <ErrorBox error={error} />{!status && !error && <Loading />}
    {status && <>
      {(!status.platform.connected || !status.worker_enabled) && <p className="feishu-agent-runtime-note">{!status.worker_enabled ? '后台处理未开启，当前可查看配置与记录。' : '官网尚未连接，官网保存暂待处理。群内记录仍可查看。'}</p>}
      <div className="feishu-history-mode" role="group" aria-label="记录显示方式"><button aria-pressed={historyMode === 'sessions'} onClick={() => setHistoryMode('sessions')}>按会话查看</button><button aria-pressed={historyMode === 'records'} onClick={() => setHistoryMode('records')}>逐条记录</button></div>
      {historyMode === 'sessions' ? <FeishuSessionHistory revision={revision} selectTask={task => { setSelectedRecord(task); setSelected(task.id); }} /> : <FeishuTaskHistory tasks={status.tasks} select={id => { setSelectedRecord(null); setSelected(id); }} />}
      <details className="feishu-agent-configuration" open={!status.tasks.length}><summary><span>群配置与使用说明</span><small>{status.bindings.length} 个群 · {status.bindings.filter(binding => binding.config?.enabled).length} 个已启用{status.bindings.some(binding => binding.error) ? ' · 配置有异常，请查看' : ''}</small></summary>
      <div className="feishu-agent-notices"><div><MessageSquare size={18} aria-hidden="true" /><span>群成员使用 <strong>@ 机器人 + 案件号／问题</strong> 查询；新收图片按本群设置自动处理。</span></div><div><FileCheck2 size={18} aria-hidden="true" /><span><strong>官网仅保存草稿</strong> · 正式提交由用户在案件页确认。</span></div>
      <p>多人协作时，回复同一条消息或在同一话题内 <strong>@ 机器人</strong>，即可接续大家已收录的讨论。没有回复话题时，按发言人分别接续；个人偏好始终只用于本人。</p>
      {!status.platform.connected && <p>官网尚未连接。图片仍可分析和整理，官网上传保存会保留为待处理；连接官网后可重试对应任务。</p>}
      {!status.model_ready && <p>AI 尚未配置或已关闭；规则整理与统计日报可继续。<button onClick={openSettings}>配置模型</button></p>}
      {!status.worker_enabled && <p>此实例的后台处理未开启，当前可查看配置与记录。</p>}</div>
      <div className="feishu-agent-bindings">{status.bindings.map(binding => <article className="feishu-agent-card" key={binding.connector_id}>
        <header><div><h3>{binding.chat_name || binding.connector_name}</h3><p>{binding.project_name || '尚未绑定项目'}</p></div><span className="feishu-agent-state">{binding.config?.enabled ? binding.error ? '待处理' : '已启用' : '未启用'}</span></header>
        {binding.error && <p className="error-text">{binding.error}</p>}
        <dl><div><dt>群对话</dt><dd>{binding.config?.enabled && binding.config.chat_enabled ? '@ 机器人时响应' : '暂停'}</dd></div><div><dt>新图片</dt><dd>{binding.config?.enabled && binding.config.images_enabled ? '自动分析、整理' : '未开启自动整理'}</dd></div><div><dt>官网保存</dt><dd>{binding.config?.enabled && binding.config.save_drafts ? '适用环节保存草稿' : '保留本地草稿'}</dd></div><div><dt>每日简报</dt><dd>{binding.config?.enabled && binding.config.daily_brief ? `每天 ${binding.config.brief_time}（北京时间）` : '尚未启用'}</dd></div><div><dt>负责人覆盖</dt><dd>本群已绑定 {binding.projects.filter(p => p.contact).length} / {binding.projects.length} 个项目</dd></div></dl>
        {binding.next_brief_at && <p className="feishu-agent-next"><CalendarClock size={16} aria-hidden="true" />下次简报：{nextTime(binding.next_brief_at)}（北京时间）</p>}
        <details className="feishu-agent-projects" open={binding.projects.length <= 3}><summary>项目范围与负责人（{binding.projects.length}）</summary><div>{binding.projects.map(project => <article key={project.id}><div><strong>{project.name}</strong><small>{project.contact ? `@ ${project.contact.name}` : '负责人未绑定到本群'}</small></div><div className="followup-actions"><button disabled={!!busy || !isAdmin} onClick={() => onContact(project.id, project.contact)}><UserRound size={16} aria-hidden="true" />负责人</button><button disabled={!!busy} onClick={() => void previewBrief(project.id)}>预览日报</button></div></article>)}</div></details><div className="followup-actions"><button disabled={!!busy || !isAdmin} onClick={() => setEditing(binding)}><Settings size={16} aria-hidden="true" />设置</button>{!!binding.config?.enabled && <button disabled={!!busy || !isAdmin} onClick={() => void action(binding.connector_id, () => put(`/api/feishu-agent/bindings/${binding.connector_id}`, { enabled: false }), '已暂停此群助手')}>暂停</button>}</div>
      </article>)}</div>
      {!status.bindings.length && <Empty>先在收图入口配置飞书群，再选择群助手的项目范围。</Empty>}
      </details>
    </>}
    {editing && <ConfigDialog binding={editing} projects={status?.projects || []} close={() => setEditing(null)} saved={() => { setRevision(v => v + 1); notify('群助手设置已保存，从新消息开始处理'); }} />}
    {preview && <Modal title="每日简报预览" close={() => setPreview(null)} wide footer={<button onClick={() => setPreview(null)}>关闭预览</button>}><div className="feishu-agent-detail"><p className="muted">当前台账统计预览。定时发送时会重新读取数据，并调用模型整理进展说明。</p><pre>{preview.text}</pre></div></Modal>}
    {selected && <Modal title={`${detail ? KINDS[detail.kind] : '群助手'}处理记录`} close={() => setSelected('')} wide footer={<button onClick={() => setSelected('')}>关闭</button>}><div className="feishu-agent-detail"><ErrorBox error={detailError} />{!detail && !detailError && <Loading />}{detail && <>
      <div className="feishu-agent-detail-status" aria-live="polite"><strong>{STATES[detail.state]}</strong><span>{detail.delivery ? DELIVERY[detail.delivery.state] : '尚无群内回复'}</span></div>
      <small>{record?.chat_name || record?.connector_name}{record?.sender_label && ` · ${record.sender_label}`} · {nextTime(detail.created_at)}（北京时间）</small>
      {detail.kind !== 'brief' && <p className="muted">对话范围：{detail.input.conversation?.mode === 'topic' ? '本群共享回复话题' : detail.input.conversation?.mode === 'isolated' ? '本条消息单独处理' : '本群内当前发言人'}{detail.input.conversation?.notice ? `。${detail.input.conversation.notice}` : ''}</p>}
      {detail.kind !== 'brief' && <section className="feishu-detail-source"><h3>来源消息</h3><pre>{detail.input.text || (detail.input.total_images ? `收到 ${detail.input.total_images} 张图片，未附文字说明` : '未附文字说明')}</pre></section>}
      <ErrorBox error={detail.error || ''} />
      {detail.result.text && <section className="feishu-detail-reply"><h3>{detail.kind === 'brief' ? '简报内容' : '助手答复'}</h3><pre>{detail.result.text}</pre></section>}
      {!!detail.images.length && <section><h3>图片分析与归档</h3><div className="feishu-agent-image-list">{detail.images.map(image => <article key={image.photo_id}><a href={`/api/photos/${image.photo_id}/original`} target="_blank" rel="noreferrer"><img src={`/api/photos/${image.photo_id}/thumbnail`} alt="来源图片" loading="lazy" /></a><div><strong>{image.state === 'organized' ? '已整理，内容待人工核对' : '待核对'}</strong><p>{image.summary}</p>{image.suggestion.reason && <p>{image.suggestion.reason}</p>}</div></article>)}</div></section>}
      {!!detail.drafts.length && <section><h3>官网资料草稿</h3>{detail.drafts.map(draft => <article className="feishu-agent-draft" key={draft.case_id}><strong>{draft.platform_report_code || draft.short_code || draft.title}</strong><p>{DRAFTS[draft.result.state] || draft.result.state}{draft.result.uploaded_count !== undefined && ` · 上传回执 ${draft.result.uploaded_count} 份`}</p>{draft.result.reason && <p>{draft.result.reason}</p>}{!!draft.result.issues?.length && <ul>{draft.result.issues.map((issue, index) => <li key={index}>{issue}</li>)}</ul>}<button disabled={!!busy} onClick={() => void openDraft(draft.case_id)}>打开案件核对草稿</button></article>)}</section>}
      {detail.delivery?.error && <p className="error-text">{detail.delivery.error}</p>}
      <div className="followup-actions">{detail.kind !== 'brief' && ['failed', 'partial'].includes(detail.state) && !['queued', 'sending', 'unknown'].includes(detail.delivery?.state || '') && <button disabled={!!busy || !isAdmin} onClick={() => void action('retry-task', () => post(`/api/feishu-agent/tasks/${detail.id}/retry`), '已新建重试任务，保留原处理记录')}>重新处理</button>}
      {detail.delivery?.state === 'failed' && <button disabled={!!busy || !isAdmin} onClick={() => void action('retry-delivery', () => post(`/api/feishu-agent/tasks/${detail.id}/delivery/retry`), '已重新安排群内发送')}>重试群内发送</button>}
      {detail.delivery?.state === 'unknown' && <><p>请先查看群内是否收到本条消息，再选择核对结果。</p><button disabled={!!busy || !isAdmin} onClick={() => void action('confirm-delivery', () => post(`/api/feishu-agent/tasks/${detail.id}/delivery/confirm`, { received: true }), '已记录群内收到')}>群内已收到</button><button disabled={!!busy || !isAdmin} onClick={() => void action('confirm-delivery', () => post(`/api/feishu-agent/tasks/${detail.id}/delivery/confirm`, { received: false }), '已记录未收到，将按原消息编号重试')}>确认未收到，重试发送</button></>}</div>
      {!!detail.steps.length && <details><summary>查看处理步骤（{detail.steps.length}）</summary><small>{detail.model || '未调用模型'}{detail.usage.requests ? ` · 已调用 ${detail.usage.requests} 次` : ''}</small><ol>{detail.steps.map((step, index) => <li key={index}><strong>{TOOLS[step.tool] || step.tool}</strong><small>{dateTime(step.created_at)}</small><pre>{step.outcome}</pre></li>)}</ol></details>}
    </>}</div></Modal>}
  </div>;
}
