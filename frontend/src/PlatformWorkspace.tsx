import PhotoReuseNote from './PhotoReuseNote';
import { useAuth } from './Auth';
import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { AlertCircle, CheckCircle2, Download, ExternalLink, FileText, FolderOpen, Link2, LockKeyhole, RefreshCw, Save, Search, X } from 'lucide-react';
import { api, ApiError, message, post, put, queryString } from './api';
import { bytes, dateTime, Empty, ErrorBox, Loading, Modal, place, PHOTO_GROUPS, ROLES, STAGES, useData, useDebounce } from './common';
import type { Case, Notify, Photo } from './types';
import type { PlatformContract, PlatformField, PlatformFileField, PlatformLink, PlatformPackage, PlatformPlan, PlatformSession, PlatformStage, RemoteCase } from './platformTypes';
import './platform.css';
import ReviewBatchDialog from './ReviewBatchDialog';
import AttachmentGallery, { officialAttachments, selectedAttachments, type Attachment } from './AttachmentGallery';
import RemoveAttachmentDialog, { RemovalAlerts, type RemovalTarget } from './RemoveAttachmentDialog';
import type { AttachmentRemoval } from './platformTypes';
import type { ReviewBatch } from './workflowTypes';

export function LoginDialog({ close, connected, configure }: { close: () => void; connected: (session: PlatformSession) => void; configure?: () => void }) {
  const [username, setUsername] = useState(''), [password, setPassword] = useState(''), [busy, setBusy] = useState(false), [error, setError] = useState('');
  async function submit(e: FormEvent) {
    e.preventDefault(); setBusy(true); setError('');
    try { connected(await post<PlatformSession>('/api/platform/session', { username, password })); close(); }
    catch (e) {
      const detail = message(e);
      setError(e instanceof ApiError && [502, 504].includes(e.status) && detail === `请求失败 (${e.status})`
        ? `连接官网时网关未返回有效结果（${e.status}）。请稍后重试；也可先打开官网确认账号能否正常登录。` : detail);
    }
    finally { setPassword(''); setBusy(false); }
  }
  return <Modal title="连接官网" close={() => { if (!busy) close(); }} footer={<><button disabled={busy} onClick={close}>关闭</button><button className="primary" type="submit" form="platform-login" disabled={busy || !username || !password}>{busy ? '正在通过官网正常登录…' : '连接官网'}</button></>}>
    <p className="notice">这里用于临时登录。需要重启后自动连接时，请在“系统设置 → 连接与收图”保存系统账号。临时登录会暂停已设置的自动连接。</p>
    {configure && <button className="space-bottom" disabled={busy} onClick={configure}><LockKeyhole size={15} aria-hidden="true" />设置系统账号与自动连接</button>}
    <ErrorBox error={error} /><form id="platform-login" className="form-grid" onSubmit={submit} autoComplete="off">
      <label>官网账号<input value={username} onChange={e => setUsername(e.target.value)} disabled={busy} autoComplete="off" /></label>
      <label>官网密码<input type="password" value={password} onChange={e => setPassword(e.target.value)} disabled={busy} autoComplete="off" /></label>
    </form>
  </Modal>;
}

function LinkDialog({ value, current, session, close, saved }: { value: Case; current: PlatformLink; session: PlatformSession | null; close: () => void; saved: () => void }) {
  const [query, setQuery] = useState(current.platform_report_code || value.report_number || value.project_name), [scope, setScope] = useState(current.snapshot.scope || 'repair');
  const [result, setResult] = useState<{ items: RemoteCase[]; page: number; total: number } | null>(null), [selected, setSelected] = useState<RemoteCase | null>(null);
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  const [recordId, setRecordId] = useState(current.report_receiving_id), [platformCode, setPlatformCode] = useState(current.platform_report_code), [insurerCode, setInsurerCode] = useState(current.insurer_report_number);
  async function search(page = 1) {
    setBusy(true); setError(''); setSelected(null);
    try { setResult(await api(`/api/platform/remote-cases?${queryString({ query, page, scope })}`)); }
    catch (e) { setError(message(e)); } finally { setBusy(false); }
  }
  async function bind() {
    if (!selected) return; setBusy(true); setError('');
    try { await post(`/api/platform/cases/${value.id}/bind`, { report_receiving_id: String(selected.reportReceivingId), scope, revision: current.revision }); saved(); close(); }
    catch (e) { setError(message(e)); } finally { setBusy(false); }
  }
  async function record() {
    setBusy(true); setError('');
    try { await put(`/api/platform/cases/${value.id}/link`, { report_receiving_id: recordId, platform_report_code: platformCode, insurer_report_number: insurerCode, revision: current.revision }); saved(); close(); }
    catch (e) { setError(message(e)); } finally { setBusy(false); }
  }
  return <Modal title="关联官网案件" wide close={() => { if (!busy) close(); }} footer={<><span className="grow muted">只保存本地对应关系，不修改官网案件</span><button disabled={busy} onClick={close}>取消</button><button className="primary" disabled={busy || !selected} onClick={() => void bind()}>{busy ? '正在核对…' : '关联所选官网案件'}</button></>}>
    <p className="notice">本地案件：{value.project_name} · {place(value)} · {value.title}</p>
    <ErrorBox error={error} />
    {session?.connected ? <><div className="toolbar platform-search">
      <label className="platform-search-field">官网列表<select value={scope} onChange={e => { setScope(e.target.value); setResult(null); setSelected(null); }} disabled={busy}><option value="repair">查勘维修</option><option value="intake">理赔受理</option></select></label>
      <label className="platform-search-field">查询条件<span className="platform-search-input"><Search size={16} aria-hidden="true" /><input aria-label="查询官网案件" value={query} onChange={e => { setQuery(e.target.value); setSelected(null); }} placeholder="报案编号、保单编号、项目名称" disabled={busy} onKeyDown={e => { if (e.key === 'Enter') void search(); }} /></span></label>
      <button disabled={busy} onClick={() => void search()}>查询官网</button>
    </div>
      {result && <><p className="muted">共{result.total}条，当前第{result.page}页。请核对项目、地址和问题内容。</p><div className="table-wrap platform-remote-table"><table><thead><tr><th>项目 / 地址</th><th>报修编号（平台）</th><th>报案编号（保司）</th><th>问题内容</th><th /></tr></thead><tbody>{result.items.map(r => <tr key={r.reportReceivingId} className={selected?.reportReceivingId === r.reportReceivingId ? 'selected-row' : ''}><td>{r.communityName}<small>{r.communityDesc}</small></td><td>{r.reportCode}</td><td>{r.reportNumber || '—'}</td><td>{r.accidentDesc || r.issueDescription || '—'}</td><td><button disabled={busy} onClick={() => setSelected(r)}>{selected?.reportReceivingId === r.reportReceivingId ? '已选择' : '选择'}</button></td></tr>)}</tbody></table></div>{!result.items.length && <Empty>未找到对应案件。可调整关键词或切换官网列表。</Empty>}<div className="row space-top"><button disabled={busy || result.page <= 1} onClick={() => void search(result.page - 1)}>上一页</button><button disabled={busy || result.page * 12 >= result.total} onClick={() => void search(result.page + 1)}>下一页</button></div></>}
      {selected && <div className="notice">将关联：{selected.communityName} · {selected.communityDesc} · {selected.accidentDesc || selected.issueDescription}<br />报修编号：{selected.reportCode}</div>}
    </> : <p className="muted">连接官网后，可以查询并核对案件。现在也可先记录已有编号。</p>}
    <details className="space-top"><summary>手动记录编号（保留为未核对状态）</summary><div className="form-grid space-top"><label>报修编号（平台）<input value={platformCode} onChange={e => setPlatformCode(e.target.value)} disabled={busy} /></label><label>报案编号（保司）<input value={insurerCode} onChange={e => setInsurerCode(e.target.value)} disabled={busy} /></label><label className="full">官网记录 ID（可留空）<input value={recordId} onChange={e => setRecordId(e.target.value)} disabled={busy} placeholder="接口记录 ID，与两种业务编号不同" /></label><button disabled={busy} onClick={() => void record()}>仅记录编号</button></div></details>
  </Modal>;
}

const willCopyCheckin = (field: PlatformFileField, photo: Photo) => field.suggest?.[0] === 'work' && photo.stage === 'survey' && photo.photo_group === 'survey_checkin' && photo.role === field.suggest?.[1];

function FilePicker({ field, photos, selected, close, apply, importFiles }: { field: PlatformFileField; photos: Photo[]; selected: string[]; close: () => void; apply: (ids: string[]) => void; importFiles: () => void }) {
  const [ids, setIds] = useState(selected), [query, setQuery] = useState(''), [matching, setMatching] = useState(true);
  const rows = photos.filter(p => (!matching || field.extensions?.includes('.' + p.original_name.split('.').pop()?.toLowerCase())) && (!query || [p.original_name, p.part, p.process, ROLES[p.role], STAGES[p.stage]].join(' ').includes(query)));
  function toggle(id: string) { setIds(v => v.includes(id) ? v.filter(x => x !== id) : [...v, id]); }
  return <Modal title={`选择${field.label}`} wide close={close} footer={<><span className="grow muted">已选{ids.length}项 · 原文件保留</span><button onClick={() => { close(); importFiles(); }}>导入本案资料</button><button onClick={close}>取消</button><button className="primary" onClick={() => { apply(ids); close(); }}>使用所选资料</button></>}>
    <p className="muted">支持 {field.extensions?.join('、')}{field.max_bytes_exclusive ? '，单个文件需小于20MB' : ''}。{field.hint}</p>
    <div className="toolbar space-top"><label className="search-box"><Search size={15} /><input aria-label="搜索本案文件" value={query} onChange={e => setQuery(e.target.value)} placeholder="文件名、部位或施工内容" /></label><label className="check"><input type="checkbox" checked={matching} onChange={e => setMatching(e.target.checked)} />仅显示支持的格式</label></div>
    {field.suggest?.[0] === 'work' && ['building_checkin', 'door_checkin'].includes(field.suggest?.[1] || '') && <p className="notice">当天查勘后直接维修时，可选择对应的查勘打卡。保存草稿时会复制为独立维修打卡，原照与原水印保留。</p>}
    <div className="table-wrap platform-picker"><table><thead><tr><th>选择 / 缩略图</th><th>原文件名</th><th>阶段 / 用途</th><th>时间 / 部位</th><th>大小</th></tr></thead><tbody>{rows.map(p => <tr key={p.id}><td><label className="row"><input type="checkbox" aria-label={`选择${p.original_name}`} checked={ids.includes(p.id)} onChange={() => toggle(p.id)} />{p.thumbnail_url ? <img className="platform-thumb" src={p.thumbnail_url} alt="" /> : <FileText size={24} />}</label></td><td><a href={p.original_url} target="_blank" rel="noreferrer">{p.original_name}</a></td><td>{PHOTO_GROUPS[p.photo_group] || (p.media_type === 'image' ? '待分类' : '附件')}<PhotoReuseNote photo={p} willCopy={willCopyCheckin(field, p)} /></td><td>{dateTime(p.watermark_time)}<small>{p.part || '—'}</small>{p.visit_id && <small>{p.visit_date} · {p.visit_key || '进场'}</small>}</td><td>{bytes(p.size_bytes)}</td></tr>)}</tbody></table></div>
    {!rows.length && <Empty>本案没有符合条件的文件，可以先导入对应资料。</Empty>}
  </Modal>;
}

const isVisible = (field: PlatformField, values: Record<string, string | number>) => Object.entries(field.when || {}).every(([key, allowed]) => allowed.includes(String(values[key] ?? '')));
const displayValues = (plan: PlatformPlan) => plan.access.editable ? plan.draft.values : Object.fromEntries(Object.entries(plan.values).map(([key, value]) => [key, String(value ?? '')]));
const sameFieldValue = (field: PlatformField, a: string, b: string) => field.kind === 'datetime' ? a.replace('T', ' ').slice(0, 16) === b.replace('T', ' ').slice(0, 16) : a.trim() === b.trim();

export default function PlatformWorkspace({ cases, initialCaseId, initialStage = 'survey', embedded = false, revision, notify, importFiles, registerLeave, onChanged, onReadback }: {
  cases: Case[]; initialCaseId: string; revision: number; notify: Notify; importFiles: (caseId: string) => void;
  initialStage?: PlatformStage; embedded?: boolean; onChanged?: () => void; onReadback?: () => void;
  registerLeave: (callback: (() => Promise<boolean>) | null) => void;
}) {
  const { person } = useAuth();
  const isAdmin = person.system_role === 'admin';
  const [caseId, setCaseId] = useState(initialCaseId || cases[0]?.id || ''), [stage, setStage] = useState<PlatformStage>(initialStage);
  const [review, setReview] = useState<ReviewBatch | null>(null);
  const [removal, setRemoval] = useState<{ target: RemovalTarget; initial?: AttachmentRemoval } | null>(null);
  const [plan, setPlan] = useState<PlatformPlan | null>(null), [values, setValues] = useState<Record<string, string>>({}), [files, setFiles] = useState<Record<string, string[]>>({});
  const [loading, setLoading] = useState(true), [saving, setSaving] = useState(false), [dirty, setDirty] = useState(false), [error, setError] = useState(''), [reload, setReload] = useState(0);
  const [binding, setBinding] = useState(false), [picker, setPicker] = useState<PlatformFileField | null>(null);
  const [session, setSession] = useState<PlatformSession | null>(null), [downloads, setDownloads] = useState<PlatformPackage[]>([]);
  const schema = useData<PlatformContract>('/api/platform/contract', reload);
  const currentRef = useRef<PlatformPlan | null>(null), saveRef = useRef<Promise<PlatformPlan | null> | null>(null);
  const path = `/api/platform/cases/${caseId}/stages/${stage}`;
  const editSignature = JSON.stringify({ values, files });
  const delayedSignature = useDebounce(editSignature, 350);

  const projectId = cases.find(c => c.id === caseId)?.project_id;
  useEffect(() => { if (!projectId || (!isAdmin && person.projects[projectId] !== 'manager')) return; void api<{ session: PlatformSession; write_enabled: boolean }>(`/api/access/project-accounts/${projectId}`).then(v => setSession({ ...v.session, write_enabled: v.write_enabled })).catch(e => setError(message(e))); }, [reload, projectId, isAdmin, person.projects]);
  useEffect(() => {
    if (!caseId && cases.length) setCaseId(cases[0].id);
  }, [caseId, cases]);
  useEffect(() => {
    if (!caseId) { setLoading(false); return; }
    const abort = new AbortController(); setLoading(true); setError(''); setDirty(false); currentRef.current = null;
    api<PlatformPlan>(path, { signal: abort.signal }).then(p => { setPlan(p); currentRef.current = p; setValues(displayValues(p)); setFiles(p.access.editable ? p.draft.files : {}); setLoading(false); }).catch(e => { if (!abort.signal.aborted) { setError(message(e)); setLoading(false); } });
    return () => abort.abort();
  }, [path, reload, caseId]);
  useEffect(() => {
    if (!caseId) return;
    const abort = new AbortController();
    api<PlatformPackage[]>(`/api/platform/cases/${caseId}/packages`, { signal: abort.signal }).then(setDownloads).catch(() => {});
    return () => abort.abort();
  }, [caseId, reload]);
  useEffect(() => {
    if (!caseId || loading || saving || dirty || picker) return;
    const abort = new AbortController(); let fetching = false;
    const timer = window.setInterval(async () => {
      if (fetching) return;
      fetching = true;
      try {
        const next = await api<PlatformPlan>(path, { signal: abort.signal });
        if (abort.signal.aborted) return;
        if (next.draft.revision !== currentRef.current?.draft.revision || next.link.revision !== currentRef.current?.link.revision || next.access.editable !== currentRef.current?.access.editable) {
          setPlan(next); currentRef.current = next; setValues(displayValues(next)); setFiles(next.access.editable ? next.draft.files : {});
        }
      } catch { /* polling never replaces a user's visible operation error */ } finally { fetching = false; }
    }, 3000);
    return () => { abort.abort(); window.clearInterval(timer); };
  }, [caseId, path, loading, saving, dirty, picker]);
  useEffect(() => {
    if (loading || !currentRef.current || delayedSignature !== editSignature || saving) return;
    const abort = new AbortController();
    const editable = currentRef.current.access.editable;
    const previewEdits = editable && dirty;
    api<PlatformPlan>(path + (previewEdits ? '/preview' : ''), { method: previewEdits ? 'POST' : 'GET', ...(previewEdits ? { body: delayedSignature } : {}), signal: abort.signal }).then(p => {
      if (abort.signal.aborted) return;
      if (!p.access.editable) {
        if (currentRef.current?.access.editable && dirty) notify('办理进度已变化，本环节已锁定，未保存的改动未写入。');
        setValues(displayValues(p)); setFiles({}); setDirty(false); setPicker(null);
      } else if (!dirty) {
        setValues(displayValues(p)); setFiles(p.draft.files);
      }
      // Preview cannot grant an old editing form the revision of a newer draft.
      if (dirty && p.access.editable && currentRef.current) p.draft = currentRef.current.draft;
      setPlan(p); currentRef.current = p;
    }).catch(e => { if (!abort.signal.aborted) setError(message(e)); });
    return () => abort.abort();
  }, [delayedSignature, editSignature, path, revision, loading, saving]);

  const save = useCallback(async (showNotice = true): Promise<PlatformPlan | null> => {
    if (saveRef.current) return saveRef.current;
    if (!currentRef.current) return null;
    if (!currentRef.current.access.editable) return currentRef.current;
    if (!dirty && currentRef.current.draft.target_report_id === currentRef.current.link.report_receiving_id) return currentRef.current;
    setSaving(true); setError('');
    const task = put<PlatformPlan>(path, { values, files, revision: currentRef.current.draft.revision, link_revision: currentRef.current.link.revision }).then(p => {
      setPlan(p); currentRef.current = p; setValues(p.draft.values); setFiles(p.draft.files); setDirty(false);
      if (showNotice) notify('已保存本地官网资料草稿'); return p;
    }).catch(e => { setError(message(e)); return null; }).finally(() => { setSaving(false); saveRef.current = null; });
    saveRef.current = task; return task;
  }, [dirty, values, files, path, notify]);
  useEffect(() => { registerLeave(async () => !dirty || !!await save(false)); return () => registerLeave(null); }, [dirty, save, registerLeave]);
  useEffect(() => { const handler = (event: BeforeUnloadEvent) => { if (dirty) { event.preventDefault(); event.returnValue = ''; } }; window.addEventListener('beforeunload', handler); return () => window.removeEventListener('beforeunload', handler); }, [dirty]);

  async function changeCase(next: string) { if (!dirty || await save(false)) setCaseId(next); }
  async function changeStage(next: PlatformStage) { if (!dirty || await save(false)) setStage(next); }
  function edit(key: string, value: string) { if (!currentRef.current?.access.editable) return; setValues(v => ({ ...v, [key]: value })); setDirty(true); }
  function choose(key: string, ids: string[]) { if (!currentRef.current?.access.editable) return; setFiles(v => ({ ...v, [key]: ids })); setDirty(true); }
  async function removeAttachment(field: PlatformFileField, item: Attachment) {
    const saved = await save(false); if (!saved) return;
    setRemoval({ target: { caseId, stage, field: field.key, label: field.label, item, revision: saved.draft.revision } });
  }
  function openRemoval(record: AttachmentRemoval) {
    const item = officialAttachments([record.file], `/api/platform/cases/${encodeURIComponent(caseId)}/stages/${stage}/files/${encodeURIComponent(record.field_key)}`)[0];
    setRemoval({ initial: record, target: { caseId, stage, field: record.field_key, label: record.label, item, revision: plan?.draft.revision || 0 } });
  }
  async function prepare() {
    const p = await save(false); if (!p) return;
    setSaving(true); setError('');
    try { const result = await post<{ download_url: string; error_count: number }>(path + '/package', { revision: p.draft.revision });
      const a = document.createElement('a'); a.href = result.download_url; a.click();
      setDownloads(await api(`/api/platform/cases/${caseId}/packages`));
      notify(result.error_count ? '本地资料包已生成，包内附待补与核对清单' : '按官网栏位整理的本地资料包已生成');
    } catch (e) { setError(message(e)); } finally { setSaving(false); }
  }
  async function refreshLink() {
    if (dirty && !await save(false)) return;
    setSaving(true); setError('');
    try { await post(`/api/workflow/cases/${caseId}/refresh`); setReload(v => v + 1); (onReadback || onChanged)?.(); notify('官网资料与办理进度已刷新'); }
    catch (e) { setError(message(e)); } finally { setSaving(false); }
  }
  async function refreshOptions() { setSaving(true); setError(''); try { if (dirty && !await save(false)) return; await post('/api/platform/dictionaries/refresh'); setReload(v => v + 1); notify('官网选项已刷新'); } catch (e) { setError(message(e)); } finally { setSaving(false); } }
  async function openBinding() { if (!dirty || await save(false)) setBinding(true); }
  async function importForCase() { if (!dirty || await save(false)) importFiles(caseId); }
  async function prefill() {
    if (!plan?.access.editable) return;
    setSaving(true); setError('');
    try {
      const fresh = await post<PlatformPlan>(path + '/preview', { values, files });
      if (dirty && currentRef.current) fresh.draft = currentRef.current.draft;
      setPlan(fresh); currentRef.current = fresh;
      if (!fresh.access.editable) { setReload(v => v + 1); return; }
      const next = { ...values }; let count = 0;
      for (const field of fresh.fields) {
        const suggestion = fresh.field_suggestions?.[field.key];
        if (suggestion && !String(next[field.key] ?? '').trim() && isVisible(field, next)) {
          next[field.key] = suggestion.value; count += 1;
        }
      }
      if (count) { setValues(next); setDirty(true); }
      notify(count ? `已预填 ${count} 项空白字段，请核对后保存。` : '当前没有可预填的空白字段，已有填写内容已保留。');
    } catch (e) { setError(message(e)); } finally { setSaving(false); }
  }
  const displayedValues = { ...plan?.values, ...values };
  const canEdit = !!plan?.access.editable && (isAdmin || person.projects[plan.case.project_id] === 'manager');
  function readValue(field: PlatformField, value: string | number) {
    if (value === '' || value === null || value === undefined) return '未记录';
    if (field.name_key && plan?.values[field.name_key]) return String(plan.values[field.name_key]);
    if (field.kind === 'personnel') return String(plan?.values.repairPersonName || value);
    const options = field.options || schema.data?.dictionaries[field.dictionary || ''] || [];
    return options.find(option => option.code === String(value))?.name || String(value);
  }
  async function reviewDraft() {
    const p = await save(false); if (!p) return;
    setSaving(true); setError('');
    try { setReview(await post<ReviewBatch>(`/api/workflow/cases/${caseId}/review/${stage}`, { revision: p.draft.revision })); }
    catch (e) { setError(message(e)); } finally { setSaving(false); }
  }

  return <section className={`workspace platform-workspace ${embedded ? 'platform-embedded' : ''}`}>
    {!embedded && <div className="page-heading"><div><h1>官网资料</h1><p>按官网环节准备字段与附件，逐案保留本地草稿</p></div><div className="row wrap"><a className="button" target="_blank" rel="noreferrer" href="https://gzzb2021.com/idi/#/thirdPartyRepair"><ExternalLink size={14} />打开官网</a></div></div>}
    {!embedded && <div className="platform-mode"><LockKeyhole size={15} /><span>{session?.write_enabled ? '核对全部字段与选材后，可确认并提交官网。' : '官网只读 · 可整理资料、保存核对记录和下载资料包'}</span>{session?.connected && <span className="status status-completed">已连接 · {session.account}</span>}</div>}
    <div className="toolbar">{!embedded && <label className="inline-field">本地案件<select aria-label="官网资料案件" value={caseId} disabled={saving} onChange={e => void changeCase(e.target.value)}><option value="" disabled>选择案件</option>{cases.map(c => <option key={c.id} value={c.id}>{c.project_name} · {place(c)} · {c.title}</option>)}</select></label>}<span className="grow" />{canEdit ? <><span className="muted">{dirty ? '修改尚未保存' : plan?.draft.updated_at ? `草稿已保存 · ${dateTime(plan.draft.updated_at)}` : '尚无本地草稿'}</span><button disabled={saving || loading || (!dirty && plan?.draft.target_report_id === plan?.link.report_receiving_id)} className="primary" onClick={() => void save()}><Save size={14} />{saving ? '处理中…' : '保存本地草稿'}</button></> : plan && <span className="stage-view-label"><LockKeyhole size={14} />仅供查看 · {plan.view_source === 'official' ? '官网回读记录' : '本地办理记录'}</span>}</div>
    {!embedded && schema.data && <nav className="tabs platform-stages" aria-label="官网办理环节">{Object.entries(schema.data.stages).map(([id, item]) => <button key={id} disabled={saving || loading} className={stage === id ? 'active' : ''} onClick={() => void changeStage(id as PlatformStage)}><small>指南 {item.guide_page}</small>{item.label}</button>)}</nav>}
    <ErrorBox error={error || schema.error} /><p className="muted">本项目官网写入账号：{session?.account || "尚未连接，请由系统管理员在人员与权限中配置"}。照片保存与正式提交使用本项目账号。</p>
    {!caseId ? <Empty>先建立本地案件，再准备官网资料。</Empty> : loading ? <Loading /> : plan && <div className="platform-body">
      <section className="platform-binding"><div><strong>{plan.case.project_name} · {place(plan.case)}</strong><p>{plan.case.title}</p><dl><dt>报修编号（平台）</dt><dd>{plan.link.platform_report_code || '未记录'}</dd><dt>报案编号（保司）</dt><dd>{plan.link.insurer_report_number || '未记录'}</dd><dt>官网节点</dt><dd>{plan.link.node}</dd></dl></div><div className="row wrap">{canEdit && isAdmin && <button disabled={saving} onClick={() => void openBinding()}><Link2 size={14} />关联 / 核对案件</button>}{plan.link.report_receiving_id && <button disabled={saving} onClick={() => void refreshLink()}><RefreshCw size={14} />刷新官网资料</button>}</div></section>
      {!canEdit && <div className="stage-lock-notice" role="status"><LockKeyhole size={18} aria-hidden="true" /><div><strong>{plan.access.reason}</strong><p>{plan.view_source === 'official' ? '显示官网已保存的信息与附件，历史本地草稿不会覆盖这些内容。' : '保留此前的办理内容，当前不能修改字段或附件。'}</p></div></div>}
      <div className="platform-columns"><div className="platform-form"><section className="platform-card"><div className="row between wrap"><h2>{plan.label} · 业务信息</h2><div className="row wrap">{canEdit && ['survey', 'repair'].includes(stage) && <button className="platform-prefill-button" disabled={saving || !Object.keys(plan.field_suggestions || {}).some(key => !String(values[key] ?? '').trim())} onClick={() => void prefill()}>根据资料预填</button>}{canEdit && Object.keys(plan.remote_values).length > 0 && !['survey', 'repair'].includes(stage) && <button className="text-button" disabled={saving} onClick={() => { setValues(prev => { const next = { ...prev }; for (const [key, value] of Object.entries(plan.remote_values)) if (!next[key]) next[key] = value; return next; }); setDirty(true); }}>带入官网空白字段</button>}{canEdit && isAdmin && session?.connected && <button className="text-button" disabled={saving} onClick={() => void refreshOptions()}>刷新官网选项</button>}</div></div><p className="muted">{canEdit ? '带 * 的字段为该环节必填项。时间按实际记录填写，不使用导入或上传时间替代。' : '当前表单仅供核对，不提供修改或重新提交操作。'}</p>{canEdit && ['survey', 'repair'].includes(stage) && <p className="platform-prefill-intro">自动上传会按案件记录和照片水印补充空栏，并保留人工填写。每项可查看依据，确认前仍可修改。</p>}<fieldset disabled={saving || !canEdit} className="platform-fieldset"><div className="form-grid">{plan.fields.filter(f => f.kind !== 'files' && isVisible(f, displayedValues)).map(f => {
        const value = values[f.key] ?? String(plan.values[f.key] ?? '');
        const options = f.options || schema.data?.dictionaries[f.dictionary || ''] || [];
        const suggestion = plan.field_suggestions?.[f.key];
        const suggestedValue = suggestion?.value || '';
        const sameSuggestion = sameFieldValue(f, value, suggestedValue);
        const source = plan.field_sources?.[f.key];
        const automatic = canEdit && source?.origin === 'automatic' && sameFieldValue(f, value, source.value);
        const suggestionText = automatic ? source.stale ? '自动填写的依据已变化，正在重新核对；也可手动修正后保存。' : `已自动填写：${source.source}${source.needs_review && !source.source.includes('核对') ? '；请核对' : ''}` : canEdit && suggestion && (!value.trim() || sameSuggestion) ? `${value.trim() ? '建议依据' : '可预填'}：${suggestion.source}${suggestion.needs_review && !suggestion.source.includes('核对') ? '；请核对' : ''}` : '';
        const suggestionNote = canEdit && !value.trim() ? plan.field_suggestion_notes?.[f.key] : '';
        const hintId = suggestionText || suggestionNote ? `prefill-${stage}-${f.key}` : undefined;
        return <label key={f.key} className={f.kind === 'textarea' || f.kind === 'personnel' ? 'full' : ''}><span>{f.label}{f.required && <b className="required-star"> *</b>}</span>
          {!canEdit || f.kind === 'readonly' ? <output aria-label={f.label} className="platform-output">{readValue(f, !canEdit ? plan.values[f.key] : plan.values[f.key] || '')}</output> : f.kind === 'textarea' ? <textarea aria-label={f.label} aria-describedby={hintId} rows={3} value={value} onChange={e => edit(f.key, e.target.value)} /> : ['select', 'dictionary'].includes(f.kind) ? <select aria-label={f.label} aria-describedby={hintId} value={value} onChange={e => edit(f.key, e.target.value)}><option value="">请选择</option>{value && !options.some(o => o.code === value) && <option value={value}>旧选项（需核对）：{value}</option>}{options.map(o => <option value={o.code} key={o.code}>{o.name}</option>)}</select> : f.kind === 'personnel' ? <><select aria-label={f.label} aria-describedby={hintId} multiple value={value.split(',').filter(Boolean)} onChange={e => edit(f.key, Array.from(e.target.selectedOptions, o => o.value).join(','))}>{plan.personnel.map(p => <option value={p.userId} key={p.userId}>{p.name}{p.phone ? ` · ${p.phone}` : ''}</option>)}</select><small>需先在线关联案件；可多选，姓名与联系方式随人员记录带入。</small></> : <input aria-label={f.label} aria-describedby={hintId} type={f.kind === 'datetime' ? 'datetime-local' : f.kind === 'date' ? 'date' : f.kind === 'integer' ? 'number' : 'text'} min={f.kind === 'integer' ? 1 : undefined} step={f.kind === 'integer' ? 1 : undefined} value={f.kind === 'datetime' ? value.replace(' ', 'T').slice(0, 16) : value} onChange={e => edit(f.key, e.target.value)} />}
          {f.hint && <small>{f.hint}</small>}
          {(suggestionText || suggestionNote) && <small className="platform-prefill-source" id={hintId}>{suggestionText || suggestionNote}</small>}
        </label>;
      })}</div></fieldset></section>
      <section className="platform-card"><div className="row between wrap"><h2>对应栏位的文件</h2>{canEdit && <div className="row wrap"><button disabled={saving || !plan.file_fields.some(f => f.suggested_ids.length)} onClick={() => { setFiles(prev => { const next = { ...prev }; for (const f of plan.file_fields) if (!f.readonly && f.suggested_ids.length) next[f.key] = [...new Set([...(next[f.key] || []), ...f.suggested_ids])]; return next; }); setDirty(true); }}>采用本环节照片建议</button><button disabled={saving} onClick={() => void importForCase()}><FolderOpen size={14} />导入本案资料</button></div>}</div>
        <RemovalAlerts items={plan.attachment_removals} open={openRemoval} />
        {!plan.file_fields.length && <p className="muted space-top">此受理环节没有文件上传栏。</p>}
        {plan.file_fields.map(f => <article key={f.key} className="platform-file-field" data-field={f.key}><div className="row between wrap"><h3>{f.label}{f.required && <span className="required-star"> *</span>}</h3><div className="row"><span className="muted">官网已有 {f.existing.length} · 本地已选 {f.selected.length}</span>{canEdit && !f.readonly && <button disabled={saving} onClick={() => setPicker(f)}>选择文件</button>}</div></div><p className="muted">{f.readonly ? f.hint : `支持 ${f.extensions?.join('、')}${f.max_bytes_exclusive ? '，单个文件需小于20MB' : ''}`}</p>{f.hint && !f.readonly && <p className="platform-field-note">{f.hint}</p>}
          {f.selected.length ? <div className="table-wrap"><table className="platform-file-table"><thead><tr><th>缩略图</th><th>{canEdit ? '本地待上传文件' : '已保存本地文件'}</th><th>分类 / 时间</th><th /></tr></thead><tbody>{f.selected.map(p => <tr key={p.id}><td>{p.thumbnail_url ? <a href={p.original_url} target="_blank" rel="noreferrer"><img className="platform-thumb" src={p.thumbnail_url} alt={p.original_name} /></a> : <FileText size={24} />}</td><td><a href={p.original_url} target="_blank" rel="noreferrer">{p.original_name}</a><small>{p.part || p.process || '—'}</small></td><td>{PHOTO_GROUPS[p.photo_group] || (p.media_type === 'image' ? '待分类' : '附件')}<PhotoReuseNote photo={p} willCopy={willCopyCheckin(f, p)} /><small>{dateTime(p.watermark_time)}</small>{(p.shared_checkin_visit_id || p.visit_id) && <small>{p.shared_checkin_date || p.visit_date} · {p.shared_checkin_key || p.visit_key || '进场'}</small>}</td><td>{canEdit && !f.readonly && <button className="icon-button" disabled={saving} aria-label={`移除${p.original_name}`} onClick={() => void removeAttachment(f, selectedAttachments([p])[0])}><X size={15} /></button>}</td></tr>)}</tbody></table></div> : <p className="platform-empty-field">{!canEdit ? `官网已保存 ${f.existing.length} 项，本环节附件仅供查看` : f.readonly ? `从官网查勘记录带入 ${f.existing.length} 项` : f.existing.length ? '已有官网文件保留；可继续选择补充资料。' : '尚未选择本地文件'}</p>}
          {!!f.removed?.length && <div className="platform-removed-files" role="status"><strong>官网已删除 {f.removed.length} 项</strong><p>以下附件已不在官网当前列表中，本地已有原件保留。</p><ul>{f.removed.map(item => <li key={item.id}>{item.oldFileName || item.fileName || item.name || item.id}<small> · 核对于 {dateTime(item.removed_at)}</small></li>)}</ul></div>}
          {!!f.existing.length && <AttachmentGallery label={f.label} items={officialAttachments(f.existing, `/api/platform/cases/${encodeURIComponent(plan.case.id)}/stages/${stage}/files/${encodeURIComponent(f.key)}`)}
            onRemove={canEdit && !f.readonly && ['survey', 'repair'].includes(stage) ? item => void removeAttachment(f, item) : undefined}
            removeDisabled={saving || !session?.connected || !session?.write_enabled || !!plan.attachment_removals?.length} />}
        </article>)}
      </section></div><aside className="platform-checks">{!canEdit ? <section className="platform-card"><h2>环节记录 · 仅供查看</h2><p>{plan.access.reason}</p>{plan.link.verified_at && <p className="muted space-top">官网回读时间：{dateTime(plan.link.verified_at)}</p>}{plan.access.active_label && <p className="muted">当前可办理：{plan.access.active_label}</p>}</section> : <section className="platform-card"><h2>{plan.error_count ? `待补 / 核对 ${plan.error_count} 项` : '字段与选材已备齐'}</h2><p className="muted">当前只核对本环节；准备完成不代表官网接收或业务验收。</p><ul>{plan.issues.map((i, index) => <li key={`${i.field}-${index}`} className={i.severity === 'error' ? 'platform-issue-error' : 'platform-issue-warning'}><AlertCircle size={15} /><span>{i.message}</span></li>)}</ul>{!plan.issues.length && <p className="success-note"><CheckCircle2 size={15} />已满足当前字段规则</p>}<button disabled={saving || loading || !!plan.error_count || !canEdit} className="primary platform-download" onClick={() => void reviewDraft()}><CheckCircle2 size={15} />预提交审核</button><button disabled={saving || loading} className="platform-download" onClick={() => void prepare()}><Download size={15} />下载按栏位资料</button><p className="muted">ZIP 用于本地整理与核对，内含原件、字段清单和待补说明。</p></section>}
        <section className="platform-card"><h2>本地准备记录</h2>{downloads.length ? <ul className="platform-downloads">{downloads.map(p => <li key={p.id}><a href={p.download_url}>{p.label} · {p.file_count}项文件</a><small>{dateTime(p.created_at)} · {p.error_count ? `待核对${p.error_count}项` : '字段备齐'} · 未上传官网</small></li>)}</ul> : <p className="muted">生成按栏位资料包后在这里保留快照。</p>}</section>
      </aside></div><details className="platform-mapping"><summary>字段映射与规则依据</summary><p>官网版本 {plan.contract_version} · 选项核对时间 {dateTime(schema.data?.dictionary_verified_at)} · 操作指南第 {plan.guide_page} 页</p><p>此处仅展示接口字段对应关系；本地文件尚无官网上传 ID，不能直接作为完整提交请求。</p><pre>{JSON.stringify({ endpoint: plan.endpoint, business_fields: plan.business_fields, file_fields: plan.file_fields.map(f => ({ field: f.key, label: f.label, existing_count: f.existing.length, local_file_ids: f.selected.map(p => p.id) })), remote_written: false }, null, 2)}</pre></details>
    </div>}
    {review && <ReviewBatchDialog initial={review} close={() => setReview(null)} changed={() => { setReload(v => v + 1); onChanged?.(); }} notify={notify} />}
    {removal && <RemoveAttachmentDialog target={removal.target} initial={removal.initial} close={() => setRemoval(null)}
      changed={() => { setReload(v => v + 1); onChanged?.(); }} done={() => setRemoval(null)} notify={notify} />}
    {binding && plan && <LinkDialog value={plan.case} current={plan.link} session={session} close={() => setBinding(false)} saved={() => { setReload(v => v + 1); onChanged?.(); notify('本地案件对应关系已更新'); }} />}
    {picker && plan && <FilePicker field={picker} photos={plan.photos} selected={files[picker.key] || []} close={() => setPicker(null)} apply={ids => choose(picker.key, ids)} importFiles={() => void importForCase()} />}
  </section>;
}
