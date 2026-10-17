import { useEffect, useRef, useState } from 'react';
import { CloudUpload, FileCheck2 } from 'lucide-react';
import { api, message, post, put } from './api';
import { ErrorBox } from './common';
import ReviewBatchDialog from './ReviewBatchDialog';
import type { Case, Notify } from './types';
import type { ReviewBatch } from './workflowTypes';
import './automatic-uploads.css';

interface UploadItem {
  case_id: string; enabled: boolean; state: string; stage: string; workflow_stage?: string; short_code: string; platform_report_code: string;
  project_name: string; title: string; review_id: string | null; save_review_id: string | null;
  detail: { reason?: string; issues?: string[]; pending_count?: number; pending_reasons?: string[]; saved_count?: number; files?: { label: string; count: number }[];
    filled_fields?: { field: string; label: string; value: string; source: string; needs_review: boolean }[];
    field_notes?: { label: string; reason: string }[]; other_stage_count?: number };
}
interface UploadStatus { policy: { enabled: boolean }; items: UploadItem[]; counts: Record<string, number>; connected: boolean; write_enabled: boolean }
function savedContent(item: UploadItem) {
  const files = (item.detail.files || []).filter(file => file.count > 0).map(file => `${file.label} × ${file.count}`);
  // Filled draft fields are only known to be saved after the save/readback cycle.
  const fields = item.save_review_id && ['needs_fields', 'awaiting_confirmation'].includes(item.state)
    ? (item.detail.filled_fields || []).map(field => field.label) : [];
  return [...files, ...fields].join(' · ');
}

export default function AutomaticUploads({ currentCase, cases = [], openCase, leave, refresh, notify, login, view = 'proposals', suppressed = false }: {
  currentCase?: Case; openCase: (id: string) => void; leave: () => Promise<boolean>; refresh: () => void; notify: Notify; login: () => void;
  view?: 'proposals' | 'progress' | 'policy'; suppressed?: boolean; cases?: Case[];
}) {
  const [data, setData] = useState<UploadStatus | null>(null), [error, setError] = useState(''), [busy, setBusy] = useState(false);
  const [review, setReview] = useState<ReviewBatch | null>(null);
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
        if (fresh.length) notify(`${fresh[0].short_code || fresh[0].platform_report_code} 的${fresh[0].stage === 'survey' ? '查勘' : '修复成果'}资料已备齐，请核对拟提交信息${fresh.length > 1 ? `，另有 ${fresh.length - 1} 案待确认` : ''}`);
      } catch (e) { if (alive) setError(message(e)); } finally { loading = false; }
    }
    void poll(); const timer = window.setInterval(() => void poll(), 3000);
    return () => { alive = false; window.clearInterval(timer); };
  }, [notify, view]);
  async function toggle() { setBusy(true); try { setData(await put<UploadStatus>('/api/workflow/uploads/policy', { enabled: !data?.policy.enabled })); } catch (e) { setError(message(e)); } finally { setBusy(false); } }
  async function enroll(id: string, enabled = true) { setBusy(true); try { await post(`/api/workflow/uploads/cases/${id}`, { enabled }); await load(); notify(enabled ? '已加入自动上传，将检查本案已归档照片' : '本案自动上传已暂停'); } catch (e) { setError(message(e)); } finally { setBusy(false); } }
  async function openReview(id: string) { if (!await leave()) return; setBusy(true); try { setReview(await api<ReviewBatch>(`/api/workflow/reviews/${id}`)); } catch (e) { notify(message(e), true); } finally { setBusy(false); } }
  const selectedCase = cases.find(c => c.id === selectedCaseId) || (currentCase?.id === selectedCaseId ? currentCase : undefined);
  const current = data?.items.find(i => i.case_id === selectedCase?.id);
  const ready = data?.items.filter(i => i.enabled && i.state === 'awaiting_confirmation' && i.review_id) || [];
  const savedItems = (data?.items || []).map(item => ({ item, content: savedContent(item) })).filter(row => row.content);
  function caseActions(item: UploadItem) {
    return <><button disabled={busy} onClick={() => void enroll(item.case_id, true)}>{item.enabled ? '重新检查' : '恢复本案'}</button>{!!item.enabled && <button className="text-button" disabled={busy} onClick={() => void enroll(item.case_id, false)}>暂停本案</button>}</>;
  }
  if (suppressed || (view === 'proposals' && !ready.length)) return null;
  if (view === 'policy') return <section aria-label="自动保存设置" className="settings-card">
    <h3><CloudUpload size={17} aria-hidden="true" />照片上传与官网草稿</h3>
    <p>按本环节逐步上传、保存。资料齐全后显示拟提交事项，由管理人员确认正式提交。</p>
    {data ? <label className="check"><input type="checkbox" aria-label="启用自动上传" disabled={busy} checked={data.policy.enabled} onChange={() => void toggle()} />自动上传并保存官网草稿</label> : <p>正在读取设置…</p>}
    <ErrorBox error={error} />
    <details className="settings-history"><summary>已有案件加入自动保存</summary><p>新归案照片会自动加入；历史案件按需选择启用。</p><label>选择已有案件<select aria-label="已有案件自动保存" value={selectedCaseId} onChange={e => setSelectedCaseId(e.target.value)}><option value="">选择案件</option>{cases.filter(c => c.platform_linked).map(c => <option key={c.id} value={c.id}>{c.short_code || c.platform_report_code} · {c.title}</option>)}</select></label>{selectedCase?.platform_linked && <div className="upload-case-enroll"><span>{current?.enabled ? '已加入自动保存' : current ? '本案已暂停' : '尚未加入自动保存'}</span><div className="upload-actions">{current ? caseActions(current) : <button disabled={busy} onClick={() => void enroll(selectedCase.id)}>启用本案自动上传</button>}</div></div>}</details>
  </section>;
  return <section className={`automatic-uploads${view === 'progress' ? ' upload-progress' : ''}`} aria-label={view === 'proposals' ? '拟提交事项' : '后台保存进度'}>
    {view === 'proposals' && <><div className="upload-summary"><FileCheck2 size={18} aria-hidden="true" /><strong>拟提交</strong><span className="upload-summary-copy">资料已保存，等待管理人员确认提交</span><span className="grow" /><strong className="upload-ready-count">{ready.length} 案待确认</strong></div>
    {ready.map(item => <article key={item.case_id} className="upload-case upload-case-ready">
      <div className="upload-case-main"><div className="row wrap"><button className="text-button upload-case-code" onClick={() => openCase(item.case_id)}>{item.short_code || item.platform_report_code}</button><span className="upload-stage">{(item.stage || item.workflow_stage) === 'survey' ? '查勘' : '修复成果'}</span><span className="upload-state">待管理人员确认</span></div>
        <p>本环节资料已保存，核对后正式提交。</p>
        {!!item.detail.files?.length && <small>{item.detail.files.map(f => `${f.label} ${f.count}`).join(' · ')}</small>}
      </div>
      <div className="upload-actions"><button className="primary" disabled={busy} onClick={() => void openReview(item.review_id!)}><FileCheck2 size={15} aria-hidden="true" />核对拟提交信息</button></div>
    </article>)}</>}
    {view === 'progress' && <div className="upload-background">
      <ErrorBox error={error} />
      {data && !data.connected && <div className="upload-connection"><span>连接官网后继续上传保存，归档照片会保留。</span><button onClick={login}>连接官网</button></div>}
      {!data && !error && <p className="muted upload-empty">正在读取保存记录…</p>}
      {data && !savedItems.length && <p className="muted upload-empty">暂无自动保存内容</p>}
      {!!savedItems.length && <table className="upload-progress-table" aria-label="自动保存记录"><thead><tr><th scope="col">案件号</th><th scope="col">已保存内容</th></tr></thead><tbody>
        {savedItems.map(({ item, content }) => <tr key={item.case_id}><th scope="row"><button className="text-button upload-case-code" onClick={() => openCase(item.case_id)}>{item.short_code || item.platform_report_code}</button></th><td>{content}</td></tr>)}
      </tbody></table>}
    </div>}
    {review && <ReviewBatchDialog initial={review} close={() => setReview(null)} changed={() => { refresh(); void load().catch(e => setError(message(e))); }} notify={notify} />}
  </section>;
}
