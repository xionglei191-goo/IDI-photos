import { useEffect, useState } from 'react';
import { CheckCircle2, FileText, RefreshCw, Save, Send } from 'lucide-react';
import { api, message, post } from './api';
import { dateTime, ErrorBox, Modal, PHOTO_GROUPS, place } from './common';
import type { Notify } from './types';
import type { ReviewBatch } from './workflowTypes';

export default function ReviewBatchDialog({ initial, close, changed, notify }: { initial: ReviewBatch; close: () => void; changed: () => void; notify: Notify }) {
  const [batch, setBatch] = useState(initial), [acknowledged, setAcknowledged] = useState(false), [busy, setBusy] = useState(false), [error, setError] = useState('');
  const plan = batch.snapshot, executed = ['submitting', 'submitted', 'verified', 'uncertain', 'saved', 'save_verified'].includes(batch.state);
  const working = busy || batch.active;
  const mayWrite = acknowledged && batch.write_enabled && batch.connected && batch.state !== 'stale' && !working;
  const isSave = !!batch.save_only || batch.result.action === 'save';
  const uploaded = Object.keys(batch.result.uploaded || {}).length;
  const progress = batch.result.phase === 'checking' ? '正在核对官网最新资料…' : batch.result.phase === 'uploading' ? `正在上传原件 ${uploaded}/${batch.result.file_total || 0}${batch.result.current_file ? ` · ${batch.result.current_file}` : ''}` : batch.result.phase === 'saving' ? '正在保存到官网，不推进办理环节…' : batch.result.phase === 'submitting' ? '正在正式提交官网…' : batch.result.phase === 'readback' ? '正在回读官网核对结果…' : '正在处理…';
  useEffect(() => {
    if (!busy && batch.state !== 'submitting') return;
    let alive = true, fetching = false;
    const timer = window.setInterval(async () => {
      if (fetching) return;
      fetching = true;
      try { const next = await api<ReviewBatch>(`/api/workflow/reviews/${batch.id}`); if (alive) setBatch(next); }
      catch { /* the write request or manual readback reports the actionable error */ }
      finally { fetching = false; }
    }, 1200);
    return () => { alive = false; window.clearInterval(timer); };
  }, [busy, batch.id, batch.state]);
  function outcome(next: ReviewBatch) {
    return next.state === 'save_verified' ? '已保存到官网并回读核对一致，尚未正式提交' : next.state === 'verified' ? '官网已接收提交，回读核对一致' : next.result.action === 'save' ? '官网已保存，仍需回读核对' : '官网已返回提交结果，仍需回读核对';
  }
  async function confirm(localOnly: boolean, action: 'save' | 'submit' = 'submit') {
    setBusy(true); setError('');
    try { const next = await post<ReviewBatch>(`/api/workflow/reviews/${batch.id}/confirm`, { acknowledged, fingerprint: batch.fingerprint, local_only: localOnly, action }); setBatch(next); changed(); notify(localOnly ? '本次核对记录已保存到本机' : outcome(next)); }
    catch (e) { setError(message(e)); try { setBatch(await api<ReviewBatch>(`/api/workflow/reviews/${batch.id}`)); } catch { /* keep the original error visible */ } changed(); } finally { setBusy(false); }
  }
  async function readback() { setBusy(true); setError(''); try { const next = await post<ReviewBatch>(`/api/workflow/reviews/${batch.id}/readback`); setBatch(next); changed(); if (['save_verified', 'verified'].includes(next.state)) notify(outcome(next)); } catch (e) { setError(message(e)); } finally { setBusy(false); } }
  function display(key: string, value: unknown) {
    const field = plan.fields.find(f => f.key === key);
    if (field?.kind === 'personnel') return plan.personnel.filter(p => String(value).split(',').includes(p.userId)).map(p => p.name).join('、');
    if (field?.name_key && plan.business_fields[field.name_key]) return String(plan.business_fields[field.name_key]);
    return field?.options?.find(o => o.code === String(value))?.name || String(value ?? '');
  }
  return <Modal title={`${plan.label} · ${batch.save_only ? "自动保存记录" : "预提交审核"}`} close={() => { if (!working) close(); }} wide footer={<>
    <span className="grow muted review-status" role="status">{working ? progress : batch.state === 'save_verified' ? '已保存官网 · 回读一致 · 未提交' : batch.state === 'verified' ? '已正式提交 · 官网回读一致' : batch.state === 'stale' ? '资料已变化，请关闭后重新打开审核' : !batch.write_enabled ? '官网只读 · 可保存本地核对记录' : !batch.connected ? '官网未连接，请先连接后再操作' : batch.state === 'uncertain' ? '官网结果待核实，请先回读' : ['saved', 'submitted'].includes(batch.state) ? '官网已响应，请回读核对' : !acknowledged ? '请先勾选下方核对确认' : '保存官网不推进环节；正式提交后进入下一步'}</span>
    <button disabled={working} onClick={close}>关闭</button>{executed ? <button disabled={working || !batch.connected} onClick={() => void readback()}><RefreshCw size={15} />回读官网结果</button> : !batch.access.editable ? null : <><button disabled={working || !acknowledged || batch.state === 'stale'} onClick={() => void confirm(true, isSave ? 'save' : 'submit')}><CheckCircle2 size={15} />保存本地核对记录</button>{plan.actions.includes('save') && <button disabled={!mayWrite || (batch.state === 'failed' && !isSave)} onClick={() => void confirm(false, 'save')}><Save size={15} />{batch.state === 'failed' && isSave ? '重试保存到官网' : '保存到官网（不提交）'}</button>}{plan.actions.includes('submit') && <button className="primary" disabled={!mayWrite || (batch.state === 'failed' && isSave)} onClick={() => void confirm(false, 'submit')}><Send size={15} />{batch.state === 'failed' && !isSave ? '重试正式提交' : '确认并提交'}</button>}</>}
  </>}>
    <div className="review-identity"><strong>{plan.case.short_code || plan.case.platform_report_code}</strong><span>{plan.case.project_name} · {place(plan.case)}</span><small>报修编号：{plan.link.platform_report_code} · 保单：{plan.case.policy_number || '未提供'} · 报案编号：{plan.link.insurer_report_number || '未提供'}</small></div>
    <ErrorBox error={error || batch.result.error || ''} />
    {batch.state === 'save_verified' && <p className="notice success-note">本次字段和附件已保存到官网，仍处于当前办理环节。补齐资料后可核对拟提交信息。</p>}
    {batch.state === 'failed' && <p className="notice">本次业务操作未完成。已取得的 {uploaded} 份上传回执会保留，修正错误后可重试。</p>}
    {!batch.access.editable && <p className="notice">{batch.access.reason}以下保留当时的核对快照。</p>}
    {!!batch.result.mismatches?.length && <p className="notice">官网回读仍有 {batch.result.mismatches.length} 项待核对：{batch.result.mismatches.map(key => plan.fields.find(f => f.key === key)?.label || ({ workflow_stage: '办理进度', draft_state: '官网保存状态' }[key]) || key).join('、')}。请回读官网结果。</p>}
    <h3 className="space-top">本次全部业务字段</h3><dl className="review-fields">{plan.fields.filter(f => f.kind !== 'files' && plan.business_fields[f.key] !== undefined).map(f => <div key={f.key}><dt>{f.label}</dt><dd>{display(f.key, plan.business_fields[f.key]) || '—'}{plan.field_sources?.[f.key]?.origin === 'automatic' && <small className="platform-prefill-source">自动填写依据：{plan.field_sources[f.key].source}</small>}</dd></div>)}</dl>
    {plan.file_fields.map(f => <section className="review-file-section" key={f.key}><h3>{f.label}<small>官网已有 {f.existing.length} · 本次 {f.selected.length}</small></h3><div className="review-photo-grid">{f.selected.map(p => <a href={p.original_url} target="_blank" rel="noreferrer" className="review-photo" key={p.id}>{p.thumbnail_url ? <img src={p.thumbnail_url} alt={p.original_name} /> : <div className="review-file-icon"><FileText size={30} /></div>}<strong>{p.original_name}</strong><small>{PHOTO_GROUPS[p.photo_group] || '文档资料'} · {dateTime(p.watermark_time)}</small><small>{p.part || p.process || '查看原件'}</small></a>)}</div>{!!f.existing.length && <p className="muted">保留官网已有文件：{f.existing.map(v => v.fileName || v.oldFileName || v.name || '已有附件').join('、')}</p>}{!f.selected.length && !f.existing.length && <p className="muted">此项未选文件</p>}</section>)}
    <label className="check review-ack"><input type="checkbox" checked={acknowledged} disabled={working || executed || !batch.access.editable} onChange={e => setAcknowledged(e.target.checked)} />我已核对当前案件、全部字段、实际业务时间和所选原件</label>
  </Modal>;
}
