import { useEffect, useRef, useState } from 'react';
import { CloudUpload, FileCheck2 } from 'lucide-react';
import { api, message, post, put } from './api';
import { ErrorBox, Modal } from './common';
import ReviewBatchDialog from './ReviewBatchDialog';
import type { Case, Notify } from './types';
import type { ReviewBatch } from './workflowTypes';
import './automatic-uploads.css';

interface UploadItem {
  case_id: string; enabled: boolean; state: string; stage: string; workflow_stage?: string; short_code: string; platform_report_code: string;
  project_name: string; title: string; review_id: string | null; save_review_id: string | null;
  notification?: ReviewNotification | null;
  detail: { reason?: string; issues?: string[]; pending_count?: number; pending_reasons?: string[]; saved_count?: number; files?: { label: string; count: number }[];
    pending_fields?: { field: string; label: string; message: string }[];
    filled_fields?: { field: string; label: string; value: string; source: string; needs_review: boolean }[];
    field_notes?: { label: string; reason: string }[]; other_stage_count?: number };
}
interface ReviewNotification { id?: string; state: string; name?: string; chat_name?: string; error?: string; message?: string; sent_at?: string }
interface ProjectConnection { project_id: string; project_name: string; state: string; connected: boolean }
interface UploadStatus {
  policy: { enabled: boolean; notify_review_ready?: boolean }; items: UploadItem[]; counts: Record<string, number>; connected: boolean; write_enabled: boolean;
  account_scope?: 'project' | 'company'; company_connected?: boolean; project_connections?: ProjectConnection[]; can_configure_project_accounts?: boolean;
}
const PROJECT_CONNECTION: Record<string, string> = {
  unconfigured: '项目官网账号尚未配置', disabled: '项目官网账号未连接，自动连接已暂停',
  connecting: '项目官网账号正在连接', pending: '项目官网账号等待自动连接',
  retry_wait: '项目官网账号连接中断，等待自动重试', needs_attention: '项目官网账号连接需要处理',
  write_paused: '本项目的官网保存已暂停', server_read_only: '服务器尚未启用官网写入，请联系系统管理员',
};
function notificationText(value: ReviewNotification) {
  if (value.message) return value.message;
  if (value.state === 'sent') return `已在“${value.chat_name}” @ ${value.name} 提醒核对`;
  if (value.state === 'queued' || value.state === 'sending') return `正在向“${value.chat_name}”的 ${value.name} 发送核对提醒`;
  if (value.state === 'unknown') return '飞书提醒送达情况待核对，请先查看群内记录';
  if (value.state === 'failed') return `飞书提醒发送失败：${value.error || '请检查连接后重试'}`;
  return value.error || '飞书提醒已取消，等待资料重新核对';
}
function savedContent(item: UploadItem) {
  const files = (item.detail.files || []).filter(file => file.count > 0).map(file => `${file.label} × ${file.count}`);
  // Filled draft fields are only known to be saved after the save/readback cycle.
  const fields = item.save_review_id && ['needs_fields', 'awaiting_confirmation'].includes(item.state)
    ? (item.detail.filled_fields || []).map(field => field.label) : [];
  return [...files, ...fields].join(' · ');
}

export default function AutomaticUploads({ currentCase, cases = [], openCase, leave, refresh, notify, login, openProjectAccount, view = 'proposals', suppressed = false }: {
  currentCase?: Case; openCase: (id: string) => void; leave: () => Promise<boolean>; refresh: () => void; notify: Notify; login: () => void;
  openProjectAccount?: (projectId: string) => void;
  view?: 'proposals' | 'progress' | 'policy'; suppressed?: boolean; cases?: Case[];
}) {
  const [data, setData] = useState<UploadStatus | null>(null), [error, setError] = useState(''), [busy, setBusy] = useState(false);
  const [review, setReview] = useState<ReviewBatch | null>(null);
  const [delivery, setDelivery] = useState<ReviewNotification | null>(null);
  const [selectedCaseId, setSelectedCaseId] = useState(currentCase?.id || '');
  useEffect(() => setSelectedCaseId(currentCase?.id || ''), [currentCase?.id]);
  const seen = useRef(new Set<string>());
  async function load() { const next = await api<UploadStatus>('/api/workflow/uploads'); setData(next); setError(''); return next; }
  useEffect(() => {
    let alive = true, loading = false;
    async function poll() {
      if (loading) return; loading = true;
      try {
        const next = await api<UploadStatus>('/api/workflow/uploads'); if (!alive) return;
        setData(next); setError('');
        const fresh = view === 'proposals' ? next.items.filter(i => i.enabled && i.state === 'awaiting_confirmation' && i.review_id && !seen.current.has(i.review_id)) : [];
        fresh.forEach(i => seen.current.add(i.review_id!));
        if (fresh.length) notify(`${fresh[0].short_code || fresh[0].platform_report_code} 的${fresh[0].stage === 'survey' ? '查勘' : '修复成果'}照片已齐，请核对拟提交信息${fresh.length > 1 ? `，另有 ${fresh.length - 1} 案待确认` : ''}`);
      } catch (e) { if (alive) setError(message(e)); } finally { loading = false; }
    }
    void poll(); const timer = window.setInterval(() => void poll(), 3000);
    return () => { alive = false; window.clearInterval(timer); };
  }, [notify, view]);
  async function toggle() { setBusy(true); try { setData(await put<UploadStatus>('/api/workflow/uploads/policy', { enabled: !data?.policy.enabled })); } catch (e) { setError(message(e)); } finally { setBusy(false); } }
  async function toggleNotifications() { setBusy(true); setError(''); try { setData(await put<UploadStatus>('/api/workflow/uploads/policy', { notify_review_ready: !data?.policy.notify_review_ready })); } catch (e) { setError(message(e)); } finally { setBusy(false); } }
  async function resolveNotification(value: ReviewNotification, received?: boolean) {
    if (!value.id) return;
    setBusy(true); setError('');
    try {
      await post(`/api/workflow/uploads/notifications/${value.id}/${received === undefined ? 'retry' : 'confirm'}`, received === undefined ? undefined : { received });
      setDelivery(null); await load();
      notify(received === true ? '已记录群内收到核对提醒' : '核对提醒已重新排队');
    } catch (e) { setError(message(e)); } finally { setBusy(false); }
  }
  async function enroll(id: string, enabled = true) { setBusy(true); try { await post(`/api/workflow/uploads/cases/${id}`, { enabled }); await load(); notify(enabled ? '已加入自动上传，将检查本案已归档照片' : '本案自动上传已暂停'); } catch (e) { setError(message(e)); } finally { setBusy(false); } }
  async function openReview(id: string) { if (!await leave()) return; setBusy(true); try { setReview(await api<ReviewBatch>(`/api/workflow/reviews/${id}`)); } catch (e) { notify(message(e), true); } finally { setBusy(false); } }
  const selectedCase = cases.find(c => c.id === selectedCaseId) || (currentCase?.id === selectedCaseId ? currentCase : undefined);
  const current = data?.items.find(i => i.case_id === selectedCase?.id);
  const ready = data?.items.filter(i => i.enabled && i.state === 'awaiting_confirmation' && i.review_id) || [];
  const savedItems = (data?.items || []).map(item => ({ item, content: savedContent(item) })).filter(row => row.content);
  const accountIssues = (data?.project_connections || []).filter(account => account.state !== 'connected');
  function caseActions(item: UploadItem) {
    return <><button disabled={busy} onClick={() => void enroll(item.case_id, true)}>{item.enabled ? '重新检查' : '恢复本案'}</button>{!!item.enabled && <button className="text-button" disabled={busy} onClick={() => void enroll(item.case_id, false)}>暂停本案</button>}</>;
  }
  if (suppressed || (view === 'proposals' && !ready.length)) return null;
  if (view === 'policy') return <section aria-label="自动保存设置" className="settings-card">
    <h3><CloudUpload size={17} aria-hidden="true" />照片上传与官网草稿</h3>
    <p>按本环节逐步上传、保存并自动填写。照片齐全后显示拟提交事项，由管理人员核对、补齐待确认字段后确认提交。</p>
    {data ? <label className="check"><input type="checkbox" aria-label="启用自动上传" disabled={busy} checked={data.policy.enabled} onChange={() => void toggle()} />自动上传并保存官网草稿</label> : <p>正在读取设置…</p>}
    {data && <><label className="check"><input type="checkbox" aria-label="飞书核对提醒" disabled={busy} checked={!!data.policy.notify_review_ready} onChange={() => void toggleNotifications()} />资料齐备待核对时，在飞书群 @ 项目负责人</label><p className="muted">使用“项目跟进”中已绑定的群和负责人，同一份拟提交资料只提醒一次。请先启用自动上传；未绑定负责人的项目会显示待绑定提示。</p></>}
    <ErrorBox error={error} />
    <details className="settings-history"><summary>已有案件加入自动保存</summary><p>新归案照片会自动加入；历史案件按需选择启用。</p><label>选择已有案件<select aria-label="已有案件自动保存" value={selectedCaseId} onChange={e => setSelectedCaseId(e.target.value)}><option value="">选择案件</option>{cases.filter(c => c.platform_linked).map(c => <option key={c.id} value={c.id}>{c.short_code || c.platform_report_code} · {c.title}</option>)}</select></label>{selectedCase?.platform_linked && <div className="upload-case-enroll"><span>{current?.enabled ? '已加入自动保存' : current ? '本案已暂停' : '尚未加入自动保存'}</span><div className="upload-actions">{current ? caseActions(current) : <button disabled={busy} onClick={() => void enroll(selectedCase.id)}>启用本案自动上传</button>}</div></div>}</details>
  </section>;
  return <section className={`automatic-uploads${view === 'progress' ? ' upload-progress' : ''}`} aria-label={view === 'proposals' ? '拟提交事项' : '后台保存进度'}>
    {view === 'proposals' && <ErrorBox error={error} />}
    {view === 'proposals' && <><div className="upload-summary"><FileCheck2 size={18} aria-hidden="true" /><strong>拟提交</strong><span className="upload-summary-copy">资料已保存，等待管理人员确认提交</span><span className="grow" /><strong className="upload-ready-count">{ready.length} 案待确认</strong></div>
    {ready.map(item => <article key={item.case_id} className="upload-case upload-case-ready">
      <div className="upload-case-main"><div className="row wrap"><button className="text-button upload-case-code" onClick={() => openCase(item.case_id)}>{item.short_code || item.platform_report_code}</button><span className="upload-stage">{(item.stage || item.workflow_stage) === 'survey' ? '查勘' : '修复成果'}</span><span className="upload-state">待管理人员确认</span></div>
        <p>{item.detail.pending_fields?.length ? `照片已齐，待确认：${item.detail.pending_fields.map(f => f.label).join('、')}。` : '本环节资料已保存，核对后正式提交。'}</p>
        {!!item.detail.files?.length && <small>{item.detail.files.map(f => `${f.label} ${f.count}`).join(' · ')}</small>}
        {item.notification && <div className="upload-notification"><small role="status">{notificationText(item.notification)}</small>
          {item.notification.state === 'failed' && <button className="text-button" disabled={busy} onClick={() => void resolveNotification(item.notification!)}>重试飞书提醒</button>}
          {item.notification.state === 'unknown' && <button className="text-button" disabled={busy} onClick={() => { setError(''); setDelivery(item.notification!); }}>核对提醒送达情况</button>}
        </div>}
      </div>
      <div className="upload-actions"><button className="primary" disabled={busy} onClick={() => void openReview(item.review_id!)}><FileCheck2 size={15} aria-hidden="true" />核对拟提交信息</button></div>
    </article>)}</>}
    {view === 'progress' && <div className="upload-background">
      <ErrorBox error={error} />
      {data?.account_scope === 'project' && accountIssues.length > 0 && <div className="upload-project-connections" aria-label="项目官网账号状态">
        <p className="upload-account-help">公司官网账号用于同步台账；上传照片和保存草稿使用各项目的官网账号。</p>
        {accountIssues.map(account => <div className="upload-connection" key={account.project_id}>
          <div className="upload-account-message"><strong>{account.project_name}</strong><span role="status">{PROJECT_CONNECTION[account.state] || '项目官网账号尚未连接'}</span>
            {!data.can_configure_project_accounts && <small>请联系系统管理员处理本项目官网账号。</small>}
          </div>
          {data.can_configure_project_accounts && openProjectAccount && account.state !== 'server_read_only' && <button aria-label={`${account.state === 'unconfigured' ? '配置' : '查看'}项目官网账号：${account.project_name}`} onClick={() => openProjectAccount(account.project_id)}>{account.state === 'unconfigured' ? '配置项目账号' : '查看项目账号'}</button>}
        </div>)}
      </div>}
      {data?.account_scope === 'company' && !data.connected && <div className="upload-connection"><span>连接官网后继续上传保存，归档照片会保留。</span><button onClick={login}>连接官网</button></div>}
      {!data && !error && <p className="muted upload-empty">正在读取保存记录…</p>}
      {data && !savedItems.length && <p className="muted upload-empty">暂无自动保存内容</p>}
      {!!savedItems.length && <table className="upload-progress-table" aria-label="自动保存记录"><thead><tr><th scope="col">案件号</th><th scope="col">已保存内容</th></tr></thead><tbody>
        {savedItems.map(({ item, content }) => <tr key={item.case_id}><th scope="row"><button className="text-button upload-case-code" onClick={() => openCase(item.case_id)}>{item.short_code || item.platform_report_code}</button></th><td>{content}</td></tr>)}
      </tbody></table>}
    </div>}
    {review && <ReviewBatchDialog initial={review} close={() => setReview(null)} changed={() => { refresh(); void load().catch(e => setError(message(e))); }} notify={notify} />}
    {delivery && <Modal title="核对飞书提醒送达情况" close={() => { if (!busy) setDelivery(null); }} footer={<>
      <button disabled={busy} onClick={() => setDelivery(null)}>取消</button>
      <button disabled={busy} onClick={() => void resolveNotification(delivery, false)}>群内未收到，重新发送</button>
      <button className="primary" disabled={busy} onClick={() => void resolveNotification(delivery, true)}>群内已收到</button>
    </>}><ErrorBox error={error} /><p>请查看“{delivery.chat_name}”内 @ {delivery.name} 的本案核对提醒，确认是否已经收到。</p><p className="muted">发送结果尚未确认，系统会等待核对后再决定是否重发。</p></Modal>}
  </section>;
}
