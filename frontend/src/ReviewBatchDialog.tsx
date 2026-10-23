import { useEffect, useState } from 'react';
import { CheckCircle2, RefreshCw, Save, Send } from 'lucide-react';
import { api, message, post } from './api';
import { ErrorBox, Modal, place } from './common';
import AttachmentGallery, { officialAttachments, selectedAttachments } from './AttachmentGallery';
import RemoveAttachmentDialog, { type RemovalTarget } from './RemoveAttachmentDialog';
import type { Notify } from './types';
import type { ReviewBatch } from './workflowTypes';
import type { PlatformContract, PlatformPlan } from './platformTypes';
import ReviewBusinessFields, { visibleReviewField } from './ReviewBusinessFields';

export default function ReviewBatchDialog({ initial, close, changed, notify }: { initial: ReviewBatch; close: () => void; changed: () => void; notify: Notify }) {
  const [batch, setBatch] = useState(initial), [acknowledged, setAcknowledged] = useState(false), [busy, setBusy] = useState(false), [error, setError] = useState('');
  const [removal, setRemoval] = useState<RemovalTarget | null>(null);
  const [edits, setEdits] = useState<Record<string, string>>({}), [preview, setPreview] = useState<PlatformPlan | null>(null);
  const [dictionaries, setDictionaries] = useState<PlatformContract['dictionaries']>({});
  const [validating, setValidating] = useState(false), [previewError, setPreviewError] = useState('');
  const executed = ['submitting', 'submitted', 'verified', 'uncertain', 'saved', 'save_verified'].includes(batch.state);
  const canEdit = batch.access.editable && !executed && batch.state !== 'stale';
  const plan = canEdit && preview ? preview : batch.snapshot;
  const values = { ...plan.values, ...edits };
  const working = busy || batch.active;
  const invalid = !!previewError || validating || plan.issues.some(i => i.severity === 'error' && (!batch.save_only || i.code !== 'required'));
  const actions = batch.save_only ? ['save'] : plan.actions;
  const mayWrite = acknowledged && !invalid && batch.write_enabled && batch.connected && canEdit && !working;
  const isSave = !!batch.save_only || batch.result.action === 'save';
  const uploaded = Object.keys(batch.result.uploaded || {}).length;
  const progress = batch.result.phase === 'checking' ? '正在核对官网最新资料…' : batch.result.phase === 'uploading' ? `正在上传原件 ${uploaded}/${batch.result.file_total || 0}${batch.result.current_file ? ` · ${batch.result.current_file}` : ''}` : batch.result.phase === 'saving' ? '正在保存到官网，不推进办理环节…' : batch.result.phase === 'submitting' ? '正在正式提交官网…' : batch.result.phase === 'readback' ? '正在回读官网核对结果…' : '正在处理…';
  useEffect(() => {
    let alive = true;
    void api<PlatformContract>('/api/platform/contract').then(schema => { if (alive) setDictionaries(schema.dictionaries); }).catch(e => { if (alive) setError(message(e)); });
    return () => { alive = false; };
  }, []);
  useEffect(() => {
    if (!canEdit || working) return;
    let alive = true;
    setValidating(true);
    const timer = window.setTimeout(async () => {
      try {
        const next = await post<PlatformPlan>(`/api/workflow/reviews/${batch.id}/preview`, { fingerprint: batch.fingerprint, values: edits });
        if (!alive) return;
        setPreview(next); setPreviewError('');
        // Only existing, evidence-backed defaults are offered for the newly
        // relevant staff and cost fields. An explicit user clear stays empty.
        const defaults: Record<string, string> = {};
        for (const key of ['repairUserId', 'estimateRestorationCostCode']) {
          const field = next.fields.find(f => f.key === key), suggestion = next.field_suggestions?.[key];
          if (field && visibleReviewField(field, next.values) && suggestion && !String(next.values[key] ?? '').trim()
              && !(key in edits) && next.field_sources?.[key]?.origin !== 'manual') defaults[key] = suggestion.value;
        }
        if (Object.keys(defaults).length) { setEdits(old => ({ ...old, ...defaults })); setAcknowledged(false); }
        else setValidating(false);
      } catch (e) { if (alive) { setPreviewError(message(e)); setValidating(false); } }
    }, 250);
    return () => { alive = false; window.clearTimeout(timer); };
  }, [batch.id, batch.fingerprint, canEdit, working, edits]);
  function editField(key: string, value: string) {
    setEdits(old => ({ ...old, [key]: value })); setAcknowledged(false); setValidating(true); setError('');
  }
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
    try { const next = await post<ReviewBatch>(`/api/workflow/reviews/${batch.id}/confirm`, { acknowledged, fingerprint: batch.fingerprint, local_only: localOnly, action, values: edits }); setBatch(next); setPreview(null); setEdits({}); changed(); notify(localOnly ? '本次核对记录已保存到本机' : outcome(next)); }
    catch (e) { setError(message(e)); try { setBatch(await api<ReviewBatch>(`/api/workflow/reviews/${batch.id}`)); } catch { /* keep the original error visible */ } changed(); } finally { setBusy(false); }
  }
  async function readback() { setBusy(true); setError(''); try { const next = await post<ReviewBatch>(`/api/workflow/reviews/${batch.id}/readback`); setBatch(next); changed(); if (['save_verified', 'verified'].includes(next.state)) notify(outcome(next)); } catch (e) { setError(message(e)); } finally { setBusy(false); } }
  return <><Modal title={`${plan.label} · ${batch.save_only ? "自动保存记录" : "预提交审核"}`} close={() => { if (!working) close(); }} wide footer={<>
    <span className="grow muted review-status" role="status">{working ? progress : batch.state === 'save_verified' ? '已保存官网 · 回读一致 · 未提交' : batch.state === 'verified' ? '已正式提交 · 官网回读一致' : batch.state === 'stale' ? '资料已变化，请重新打开审核' : !batch.write_enabled ? '官网只读 · 可保存本地核对记录' : !batch.connected ? '官网未连接，请先连接后再操作' : batch.state === 'uncertain' ? '官网结果待核实，请先回读' : ['saved', 'submitted'].includes(batch.state) ? '官网已响应，请回读核对' : validating ? '正在检查字段…' : invalid ? '请修改标出的字段' : !acknowledged ? '请勾选核对确认' : '核对完成，可以提交'}</span>
    <button disabled={working} onClick={close}>关闭</button>{executed ? <button disabled={working || !batch.connected} onClick={() => void readback()}><RefreshCw size={15} />回读官网结果</button> : !batch.access.editable ? null : <><button disabled={working || invalid || !acknowledged || !canEdit} onClick={() => void confirm(true, isSave ? 'save' : 'submit')}><CheckCircle2 size={15} />保存本地核对记录</button>{actions.includes('save') && <button disabled={!mayWrite || (batch.state === 'failed' && !isSave)} onClick={() => void confirm(false, 'save')}><Save size={15} />{batch.state === 'failed' && isSave ? '重试保存到官网' : '保存到官网（不提交）'}</button>}{(actions.includes('submit') || batch.snapshot.review_only) && <button className="primary" disabled={!mayWrite || (batch.state === 'failed' && isSave)} onClick={() => void confirm(false, 'submit')}><Send size={15} />{batch.state === 'failed' && !isSave ? '重试正式提交' : '确认并提交'}</button>}</>}
  </>}>
    <div className="review-identity"><strong>{plan.case.short_code || plan.case.platform_report_code}</strong><span>{plan.case.project_name} · {place(plan.case)}</span><small>报修编号：{plan.link.platform_report_code} · 保单：{plan.case.policy_number || '未提供'} · 报案编号：{plan.link.insurer_report_number || '未提供'}</small></div>
    <ErrorBox error={error || previewError || batch.result.error || ''} />
    {batch.state === 'save_verified' && <p className="notice success-note">本次字段和附件已保存到官网，仍处于当前办理环节。补齐资料后可核对拟提交信息。</p>}
    {batch.state === 'failed' && <p className="notice">本次业务操作未完成。已取得的 {uploaded} 份上传回执会保留，修正错误后可重试。</p>}
    {!batch.access.editable && <p className="notice">{batch.access.reason}以下保留当时的核对快照。</p>}
    {!!batch.result.mismatches?.length && <p className="notice">官网回读仍有 {batch.result.mismatches.length} 项待核对：{batch.result.mismatches.map(key => plan.fields.find(f => f.key === key)?.label || ({ workflow_stage: '办理进度', draft_state: '官网保存状态' }[key]) || key).join('、')}。请回读官网结果。</p>}
    <ReviewBusinessFields plan={plan} values={executed ? plan.values : values} dictionaries={dictionaries} editable={canEdit} disabled={working} change={editField} />
    {plan.file_fields.map(f => <section className="review-file-section" key={f.key}><h3>{f.label}<small>官网已有 {f.existing.length} · 本次 {f.selected.length}</small></h3>
      <AttachmentGallery label={f.label} items={[...officialAttachments(f.existing, `/api/workflow/reviews/${encodeURIComponent(batch.id)}/files/${encodeURIComponent(f.key)}`), ...selectedAttachments(f.selected)]}
        onRemove={batch.access.editable && !f.readonly && !executed && batch.state !== 'stale' ? item => setRemoval({ caseId: batch.case_id, stage: plan.stage, field: f.key, label: f.label, item, revision: plan.draft.revision }) : undefined}
        canRemove={item => !item.official || ['survey', 'repair'].includes(plan.stage)}
        removeDisabled={item => working || item.official && (!batch.connected || !batch.write_enabled)} />
      {!f.selected.length && !f.existing.length && <p className="muted">此项未选文件</p>}</section>)}
    <label className="check review-ack"><input type="checkbox" checked={acknowledged} disabled={working || !canEdit || invalid} onChange={e => setAcknowledged(e.target.checked)} />我已核对当前案件、全部字段、实际业务时间和所选原件</label>
  </Modal>
    {removal && <RemoveAttachmentDialog target={removal} close={() => setRemoval(null)} notify={notify}
      changed={() => { setBatch(value => ({ ...value, state: 'stale' })); setAcknowledged(false); changed(); }}
      done={() => { setRemoval(null); close(); }} />}
  </>;
}
