import { useEffect, useRef, useState } from 'react';
import { ArrowLeft, Check, Clipboard, Download, FileImage, Pencil, RefreshCw } from 'lucide-react';
import { api, message, patch, post } from './api';
import { dateTime, Empty, ErrorBox, Loading, Modal, PHOTO_GROUPS, place, ROLES, STAGES, useData, WORKFLOW_STAGES } from './common';
import PlatformWorkspace from './PlatformWorkspace';
import { VisitDialog } from './CaseWorkspace';
import ReviewBatchDialog from './ReviewBatchDialog';
import CaseAgentPanel from './CaseAgentPanel';
import type { Case, Notify, PhotoPage, Template } from './types';
import type { PlatformStage } from './platformTypes';
import type { CaseDetailData, ReviewBatch } from './workflowTypes';

const FORMS: { id: PlatformStage; label: string; step: number }[] = [{ id: 'intake', label: '理赔受理', step: 0 }, { id: 'survey', label: '现场查勘', step: 2 }, { id: 'repair', label: '修复成果', step: 3 }, { id: 'acceptance', label: '维修成果验收', step: 4 }];
const STEP_INDEX: Record<string, number> = { historical_unverified: -1, insurer_intake: -1, pending_intake: 0, insurer_review: 1, survey: 2, repair: 3, acceptance: 4, settlement: 5, closed: 5, direct_payment: 5, noncoverage: 5 };
const FORM_BY_STAGE: Record<string, PlatformStage> = { pending_intake: 'intake', insurer_review: 'intake', survey: 'survey', repair: 'repair', acceptance: 'acceptance', settlement: 'acceptance', closed: 'acceptance' };

function ResultDialog({ value, close, saved, reviewKind }: { value: Case; reviewKind?: string; close: () => void; saved: () => void }) {
  const actions: Record<string, [string, string][]> = { pending_intake: [['accepted', '受理结果：现场查勘，进入查勘维修'], ['submitted_direct_payment', '受理结果：直接赔付，提交保司复核'], ['submitted_noncoverage', '受理结果：疑似非保险责任，提交保司复核']], insurer_review: [['insurance_returned', '保司退回，重新受理']], survey: [['survey_completed', '查勘完成，进入施工']], repair: [['repair_completed', '修复成果已提交，等待验收']], acceptance: [['acceptance_completed', '验收已完成，等待结算']], settlement: [['archived', '结算完成，归档']] };
  if (reviewKind === 'submitted_direct_payment') actions.insurer_review.unshift(['direct_payment_approved', '保司确认直接赔付']);
  if (reviewKind === 'submitted_noncoverage') actions.insurer_review.unshift(['noncoverage_approved', '保司确认非保险责任']);
  const options = actions[value.workflow_stage] || [];
  const [action, setAction] = useState(options[0]?.[0] || ''), [eventAt, setEventAt] = useState(''), [note, setNote] = useState(''), [busy, setBusy] = useState(false), [error, setError] = useState('');
  async function save() { setBusy(true); setError(''); try { await post(`/api/workflow/cases/${value.id}/result`, { action, event_at: eventAt, note, revision: value.revision }); saved(); close(); } catch (e) { setError(message(e)); } finally { setBusy(false); } }
  return <Modal title="记录外部办理结果" close={close} footer={<><button disabled={busy} onClick={close}>取消</button><button className="primary" disabled={busy || !note.trim() || !eventAt || !action} onClick={() => void save()}>保存办理记录</button></>}><p className="muted">用于尚未关联官网的案件，登记已在外部完成的实际办理结果。</p><ErrorBox error={error} /><div className="form-grid space-top"><label className="full">办理结果<select value={action} onChange={e => setAction(e.target.value)}>{options.map(([key, text]) => <option key={key} value={key}>{text}</option>)}</select></label><label className="full">实际办理时间<input type="datetime-local" value={eventAt} onChange={e => setEventAt(e.target.value)} /></label><label className="full">办理凭据或说明<textarea rows={4} value={note} onChange={e => setNote(e.target.value)} placeholder="填写实际审核或提交凭据、反馈内容" /></label></div></Modal>;
}

export default function CaseDetail({ board, value, cases, revision, notify, back, goWork, edit, refresh, showPhotos, importFiles, registerLeave, leave }: {
  board: 'intake' | 'work'; value: Case; cases: Case[]; revision: number; notify: Notify; back: () => void; goWork: () => void; edit: (value: Case) => void; refresh: () => void;
  showPhotos: (caseId: string, group?: string, review?: string) => void; importFiles: (caseId: string) => void;
  registerLeave: (callback: (() => Promise<boolean>) | null) => void; leave: () => Promise<boolean>;
}) {
  const [tab, setTab] = useState('platform'), [formStage, setFormStage] = useState<PlatformStage>(board === 'intake' ? 'intake' : FORM_BY_STAGE[value.workflow_stage] || 'intake');
  const [visitsOpen, setVisitsOpen] = useState(false);
  const [resultOpen, setResultOpen] = useState(false), [watermarkOpen, setWatermarkOpen] = useState(false), [busy, setBusy] = useState(false), [error, setError] = useState(''), [review, setReview] = useState<ReviewBatch | null>(null);
  const templates = useData<Template[]>('/api/templates', revision);
  const [data, setData] = useState<CaseDetailData | null>(null), [opening, setOpening] = useState(true), [readbackError, setReadbackError] = useState('');
  const openingTask = useRef<{ id: string; task: Promise<{ data: CaseDetailData; readbackError: string }> } | null>(null);
  useEffect(() => {
    let active = true;
    setOpening(true); setReadbackError(''); setError('');
    // One readback per actual case opening, including React's effect replay.
    if (openingTask.current?.id !== value.id) openingTask.current = { id: value.id, task: (async () => {
      const cached = await api<CaseDetailData>(`/api/workflow/cases/${value.id}`);
      if (cached.case.workflow_source !== 'platform') return { data: cached, readbackError: '' };
      try { return { data: await post<CaseDetailData>(`/api/workflow/cases/${value.id}/refresh`), readbackError: '' }; }
      catch (e) { return { data: cached, readbackError: `官网刷新未完成，当前显示上次缓存：${message(e)}` }; }
    })() };
    void openingTask.current.task.then(result => { if (active) { setData(result.data); setReadbackError(result.readbackError); } })
      .catch(e => { if (active) setError(message(e)); }).finally(() => { if (active) setOpening(false); });
    return () => { active = false; };
  }, [value.id]);
  useEffect(() => {
    if (opening) return;
    const abort = new AbortController();
    void api<CaseDetailData>(`/api/workflow/cases/${value.id}`, { signal: abort.signal }).then(setData)
      .catch(e => { if (!abort.signal.aborted) setError(message(e)); });
    return () => abort.abort();
  }, [value.id, revision, opening]);
  const current = data?.case || value, currentStep = STEP_INDEX[current.workflow_stage] ?? 0;
  const readOnlyEntry = board === 'intake' && current.workflow_stage !== 'pending_intake';
  const viewingHistory = data ? !data.stage_access[formStage].editable : FORMS.find(f => f.id === formStage)?.step !== currentStep;
  useEffect(() => { setFormStage(board === 'intake' ? 'intake' : FORM_BY_STAGE[current.workflow_stage] || 'intake'); }, [board, current.workflow_stage]);
  async function changeTab(next: string) { if (await leave()) setTab(next); }
  async function changeStage(next: PlatformStage) { if (await leave()) { setFormStage(next); setTab('platform'); } }
  async function refreshRemote() { if (!await leave()) return; setBusy(true); setError(''); try { setData(await post<CaseDetailData>(`/api/workflow/cases/${current.id}/refresh`)); setReadbackError(''); refresh(); notify('官网办理进度已回读'); } catch (e) { setReadbackError(`官网刷新未完成，当前显示上次缓存：${message(e)}`); } finally { setBusy(false); } }
  async function copyWatermark() { if (!data) return; try { await navigator.clipboard.writeText(data.watermark.text); notify('水印填写内容已复制'); } catch { notify('可在弹窗中选中并复制水印内容'); } }
  async function changeTemplate(templateId: string) { setBusy(true); setError(''); try { await patch(`/api/cases/${current.id}`, { insurer_template_id: templateId, revision: current.revision }); refresh(); } catch (e) { setError(message(e)); } finally { setBusy(false); } }
  async function exportInsurer() { setBusy(true); setError(''); try { const photos = await api<PhotoPage>(`/api/photos?case_id=${current.id}&page_size=500`); if (!photos.items.length) throw new Error('本案尚无可提取资料'); if (photos.total > 500) throw new Error('本案超过500项，请到全部照片中分批选择提取'); await post('/api/packages', { photo_ids: photos.items.map(p => p.id), template_id: current.insurer_template_id, title: '保司过程资料' }); refresh(); notify('保司资料包已排队，完成后在报送记录下载'); } catch (e) { setError(message(e)); } finally { setBusy(false); } }
  async function openReview(id: string) { setBusy(true); try { setReview(await api<ReviewBatch>(`/api/workflow/reviews/${id}`)); } catch (e) { setError(message(e)); } finally { setBusy(false); } }
  return <section className="case-detail">
    <div className="case-detail-heading"><button className="text-button back-button" onClick={back}><ArrowLeft size={16} />返回看板</button><div className="row between wrap"><div><div className="row wrap"><h1>{current.short_code || '短编号待补'}</h1><span className={`workflow-status workflow-${current.workflow_stage}`}>{WORKFLOW_STAGES[current.workflow_stage]}</span></div><p>{current.project_name} · {place(current)}<span className="case-problem">{current.title}</span></p></div><div className="row wrap"><button onClick={() => setWatermarkOpen(true)}><Clipboard size={15} />水印填写内容</button><button onClick={() => showPhotos(current.id)}><FileImage size={15} />本案照片</button>{!readOnlyEntry && <button onClick={() => setVisitsOpen(true)}>进场与维修记录</button>}{!readOnlyEntry && !viewingHistory && <button onClick={() => edit(current)}><Pencil size={15} />编辑案件</button>}</div></div><div className="case-identifiers"><span>报修编号 <b>{current.platform_report_code || '待关联'}</b></span><span>保单编号 <b>{current.policy_number || '待补充'}</b></span><span>报案编号 <b>{current.report_number || '待补充'}</b></span>{current.responsible && <span>负责人 <b>{current.responsible}</b></span>}</div>{!!current.short_code_aliases?.length && <details className="case-code-history"><summary>历史短编号 · {current.short_code_aliases.length} 个</summary><p className="muted break-all">{current.short_code_aliases.join('、')}</p><small>以上旧编号仍可用于照片识别与搜索。</small></details>}</div>
    <div className="workflow-steps" aria-label="案件办理流程">{FORMS.map((form, index) => { const canOpen = form.id === 'intake' || (form.step <= currentStep && board !== 'intake'); return <button key={form.id} aria-label={form.label} aria-current={formStage === form.id && tab === 'platform' ? 'step' : undefined} disabled={!canOpen} title={currentStep < 0 && form.id === 'intake' ? '当前记录仅供查看' : form.step > currentStep ? '尚未进入此环节' : board === 'intake' && form.id !== 'intake' ? '请从查勘维修清单查看或办理' : form.step < currentStep ? '已完成，仅供查看' : '当前办理环节'} onClick={() => canOpen && void changeStage(form.id)} className={`${currentStep > form.step ? 'step-done' : currentStep === form.step ? 'step-current' : ''} ${formStage === form.id && tab === 'platform' ? 'step-viewing' : ''}`}><span>{currentStep > form.step ? <Check size={14} /> : index + 1}</span>{form.label}</button>; })}</div>
    {readOnlyEntry && current.workflow_stage !== 'insurer_review' && <div className="case-waiting case-history-entry"><span>{data?.stage_access.intake.reason || '当前受理记录仅供查看。'}{data?.stage_access.intake.active_label && `当前办理环节：${data.stage_access.intake.active_label}。`}</span>{(current.platform_lists?.includes('repair') || (current.workflow_source !== 'platform' && currentStep >= 2 && currentStep < 5)) && <button onClick={goWork}>前往查勘维修</button>}</div>}
    {current.workflow_stage === 'insurer_review' && <div className="case-waiting"><span>本案的受理结果已提交保司复核，等待确认或退回。现场查勘结果直接进入查勘维修，无需经过此分支。</span><button disabled={busy || current.workflow_source !== 'platform'} onClick={() => void refreshRemote()}><RefreshCw size={14} />刷新复核结果</button></div>}
    <nav className="case-detail-tabs" aria-label="案件资料"><button className={tab === 'platform' ? 'active' : ''} onClick={() => void changeTab('platform')}>广州平台</button><button className={tab === 'insurer' ? 'active' : ''} onClick={() => void changeTab('insurer')}>保司资料{data && !['historical_unverified', 'insurer_intake'].includes(current.workflow_stage) && <small className={data.insurer.missing_count ? 'material-missing' : 'material-ready'}>{data.insurer.missing_count ? `缺 ${data.insurer.missing_count} 项` : '已备齐'}</small>}</button><button className={tab === 'history' ? 'active' : ''} onClick={() => void changeTab('history')}>办理记录</button>{current.followup_eligible && <button className={tab === 'agent' ? 'active' : ''} onClick={() => void changeTab('agent')}>智能审阅</button>}<span className="grow" /><span className="muted">{data?.official_read_at ? `官网附件核对 ${dateTime(data.official_read_at)}` : current.workflow_source === 'platform' ? '官网附件尚未核对' : '本地案件'}</span></nav>
    <ErrorBox error={error} />
    {opening && <div className="case-waiting" role="status">正在核对官网最新进度和附件…</div>}
    {readbackError && <div className="case-waiting"><ErrorBox error={readbackError} /><button disabled={busy} onClick={() => void refreshRemote()}><RefreshCw size={14} />重试官网刷新</button></div>}
    {tab === 'platform' && !opening && <PlatformWorkspace key={`${current.id}:${formStage}`} cases={cases} initialCaseId={current.id} initialStage={formStage} embedded revision={revision} notify={notify} registerLeave={registerLeave} importFiles={importFiles} onChanged={refresh} onReadback={() => { setReadbackError(''); refresh(); }} />}
    {tab === 'agent' && current.followup_eligible && <div className="case-tab-content"><CaseAgentPanel key={current.id} value={current} notify={notify} /></div>}
    {tab === 'insurer' && (!data ? <Loading /> : <div className="case-tab-content"><section className="case-panel"><div className="row between wrap"><div><h2>保司过程资料</h2>{!readOnlyEntry && <button className="text-button" onClick={() => setVisitsOpen(true)}>进场与维修记录</button>}<p className="muted">与广州平台共用本案原件，按所选保司要求单独检查与提取。</p></div><button className="primary" disabled={busy} onClick={() => void exportInsurer()}><Download size={15} />生成保司资料包</button></div><label className="inline-field space-top">资料要求<select aria-label="保司资料要求" value={current.insurer_template_id} disabled={busy || readOnlyEntry} onChange={e => void changeTemplate(e.target.value)}>{templates.data?.map(t => <option key={t.id} value={t.id}>{t.name}</option>)}</select></label><div className="table-wrap space-top"><table className="insurer-checks"><thead><tr><th>照片资料</th><th>部位 / 施工内容</th><th>范围</th><th>要求</th><th>已有</th><th>结果</th><th /></tr></thead><tbody>{data.insurer.items.map((r, i) => <tr key={i}><td>{PHOTO_GROUPS[r.photo_group || ''] || `${STAGES[r.stage]} · ${ROLES[r.role]}`}</td><td>{[r.part, r.process].filter(Boolean).join(' · ') || '不限'}</td><td>{r.scope}</td><td>{r.minimum} 张</td><td>{r.count} 张</td><td><span className={r.missing ? 'material-missing' : 'material-ready'}>{r.missing ? `缺 ${r.missing} 张` : '已备齐'}</span></td><td><button className="text-button" onClick={() => showPhotos(current.id, r.photo_group)}>查看照片</button></td></tr>)}</tbody></table></div></section></div>)}
    {tab === 'history' && (!data ? <Loading /> : <div className="case-tab-content"><section className="case-panel"><div className="row between wrap"><h2>办理进度与凭据</h2>{current.workflow_source === 'platform' ? <button disabled={busy} onClick={() => void refreshRemote()}><RefreshCw size={15} />回读官网进度</button> : (!readOnlyEntry || current.workflow_stage === 'insurer_review') && !['closed', 'noncoverage', 'direct_payment'].includes(current.workflow_stage) && <button onClick={() => setResultOpen(true)}>记录外部办理结果</button>}</div>{data.events.length ? <div className="timeline">{data.events.map(event => <div key={event.id}><strong>{WORKFLOW_STAGES[event.to_stage]}</strong><small>{event.source === 'platform' ? '官网回读' : '人工登记'} · {dateTime(event.event_at)}</small>{event.detail.note && <p>{event.detail.note}</p>}</div>)}</div> : <p className="muted space-top">尚无办理记录。保存照片、草稿或资料包不会自动改变案件进度。</p>}</section><section className="case-panel"><h2>预提交核对记录</h2>{data.reviews.length ? <div className="table-wrap space-top"><table><thead><tr><th>环节</th><th>核对时间</th><th>状态</th><th /></tr></thead><tbody>{data.reviews.map(r => <tr key={r.id}><td>{FORMS.find(f => f.id === r.stage)?.label || r.stage}</td><td>{dateTime(r.confirmed_at || r.created_at)}</td><td>{{ prepared: '待确认', reviewed: '已核对 · 未上传', failed: '操作未完成 · 可重试', saved: '官网已保存 · 待核对', save_verified: '已保存官网 · 未提交', stale: '资料已变化', submitting: '提交中 / 待回读', submitted: '官网已响应 · 待核对', verified: '官网回读一致', uncertain: '结果待核实' }[r.state] || r.state}</td><td><button onClick={() => void openReview(r.id)}>查看快照</button></td></tr>)}</tbody></table></div> : <Empty>尚无核对记录</Empty>}</section></div>)}
    {watermarkOpen && data && <Modal title="工人水印填写内容" close={() => setWatermarkOpen(false)} footer={<><button onClick={() => setWatermarkOpen(false)}>关闭</button><button className="primary" onClick={() => void copyWatermark()}><Clipboard size={15} />复制内容</button></>}><p className="muted">工人在自己的水印相机中填写。编号、项目和区域在同一案件内保持一致，施工内容按实际环节调整。</p><pre className="watermark-copy">{data.watermark.text}</pre><div className="watermark-choices">{data.watermark.contents.map(t => <span key={t}>{t}</span>)}</div><p className="muted space-top">时间使用水印相机自带时间。XXX 填实际问题、工序或完工部位。</p></Modal>}
    {visitsOpen && <VisitDialog value={current} close={() => setVisitsOpen(false)} notify={notify} refresh={refresh} />}
    {resultOpen && <ResultDialog value={current} reviewKind={data?.events.find(e => e.to_stage === 'insurer_review')?.kind} close={() => setResultOpen(false)} saved={refresh} />}
    {review && <ReviewBatchDialog initial={review} close={() => setReview(null)} changed={refresh} notify={notify} />}
  </section>;
}
