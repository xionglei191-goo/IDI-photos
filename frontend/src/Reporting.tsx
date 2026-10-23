import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Download, FileArchive, Plus, Search, Send, Trash2 } from 'lucide-react';
import { api, message, post } from './api';
import { dateTime, Empty, ErrorBox, Loading, Modal, place, PHOTO_GROUPS, ROLES, STAGES, State, useData, useDebounce } from './common';
import { packageReady, reusedPackageMessage } from './CasePackages';
import DeletePackageDialog from './DeletePackageDialog';
import PiccRecordForm from './PiccRecordForm';
import type { Case, ExportPreview, Job, Notify, Package, PackageDetail, Template } from './types';

export function ExportDialog({ ids, parentId, close, submitted, notify }: { ids: string[]; parentId?: string | null; close: () => void; submitted: () => void; notify: Notify }) {
  const templates = useData<Template[]>('/api/templates');
  const [template, setTemplate] = useState('insurer-v2'), [title, setTitle] = useState(''), [preview, setPreview] = useState<ExportPreview | null>(null), [busy, setBusy] = useState(false), [loading, setLoading] = useState(true), [error, setError] = useState('');
  const leave = useRef<(() => Promise<boolean>) | null>(null);
  const registerLeave = useCallback((callback: (() => Promise<boolean>) | null) => { leave.current = callback; }, []);
  const picc = template === 'picc-record-v1';
  async function dismiss() { if (!busy && (!leave.current || await leave.current())) close(); }
  async function changeTemplate(value: string) { if (!leave.current || await leave.current()) setTemplate(value); }
  useEffect(() => {
    const abort = new AbortController(); setLoading(true); setError(''); setPreview(null);
    api<ExportPreview>('/api/packages/preview', { method: 'POST', body: JSON.stringify({ photo_ids: ids, template_id: template }), signal: abort.signal })
      .then(result => { setPreview(result); setLoading(false); }).catch(e => { if (!abort.signal.aborted) { setError(message(e)); setLoading(false); } });
    return () => abort.abort();
  }, [ids.join(','), template]);
  async function submit() {
    setBusy(true); setError('');
    try { const job = await post<Job>('/api/packages', { photo_ids: ids, template_id: template, parent_id: parentId || null, title }); notify(reusedPackageMessage(job) || '资料包正在生成，完成后可从报送管理下载'); submitted(); close(); }
    catch (e) { setError(message(e)); } finally { setBusy(false); }
  }
  return <Modal title={parentId ? '生成补件资料包' : '提取所选资料'} close={() => void dismiss()} wide footer={<>
    <span className="grow muted">{picc ? 'Word 记录表 · 本案照片去重后生成 · 视频使用照片打包方案' : '照片≤400 KB · 视频原件 · 按示例目录分类'}</span><button disabled={busy} onClick={() => void dismiss()}>关闭</button>
    {!picc && <button className="primary" disabled={busy || loading || !preview || !!error} onClick={() => void submit()}><FileArchive size={15} />{busy ? '提交生成任务…' : '生成ZIP资料包'}</button>}
  </>}>
    <div className="form-grid"><label>资料要求<select value={template} onChange={e => void changeTemplate(e.target.value)}>{templates.data?.map(t => <option key={t.id} value={t.id}>{t.name}{t.insurer ? ` · ${t.insurer}` : ''}</option>)}</select></label><label>报送说明<input value={title} onChange={e => setTitle(e.target.value)} placeholder="可选，如首批报送、第二次补件" /></label></div>
    <ErrorBox error={error} />{loading ? <Loading /> : preview && (picc ? <>
      <p className="notice">人保方案将使用本案去重且未移除的 {preview.photo_count} 张照片，按时间插入记录表。</p>
      <PiccRecordForm key={preview.cases[0].case.id} caseId={preview.cases[0].case.id} parentId={parentId} title={title} notify={notify} registerLeave={registerLeave} generated={() => { submitted(); close(); }} />
    </> : <>
      <p className="notice">打包{preview.photo_count}张照片{preview.video_count > 0 ? `、${preview.video_count}个视频` : ''}，涉及{preview.cases.length}个案件。按案保留内容快照，资料有变化时生成新版本；未变化时复用已有资料包。</p>
      {preview.excluded_file_count > 0 && <p className="muted">已排除 {preview.excluded_file_count} 个 PDF 或其他资料，仅打包照片与视频。</p>}
      <p className="muted">目录：查勘照片（含查勘楼栋房号打卡照片）、施工照片（含施工楼栋房号打卡照片）、验收照片。</p>
      {preview.cases.map(group => <section className="detail-section" key={group.case.id}><h3>{group.case.title}</h3><p className="muted break-all">父文件夹：{group.root_directory}</p><p className="muted">{group.case.project_name} · {place(group.case)} · {group.photo_ids.length}项 · 报案号{group.case.report_number || '暂无'}</p>
        {group.completeness.missing_count ? <><p className="warning-text">当前选材有{group.completeness.missing_count}项数量要求未满足，缺项在此处核对，压缩包内不附说明文件。</p><div className="table-wrap"><table><thead><tr><th>阶段 / 用途</th><th>范围</th><th>要求</th><th>本次</th></tr></thead><tbody>{group.completeness.items.filter(r => r.missing).map((r, i) => <tr key={i}><td>{PHOTO_GROUPS[r.photo_group || ''] || `${STAGES[r.stage]} / ${ROLES[r.role]}`}</td><td>{r.scope}</td><td>{r.minimum}</td><td>{r.count}</td></tr>)}</tbody></table></div></> : <p className="success-note">满足当前模板的数量要求。</p>}
      </section>)}<p className="muted space-top">生成资料包后，可在报送记录中登记实际提交时间、回执和补件情况。</p>
    </>)}
  </Modal>;
}

function ReceiptDialog({ value, close, saved, notify }: { value: Package; close: () => void; saved: () => void; notify: Notify }) {
  const [state, setState] = useState('submitted'), [date, setDate] = useState(''), [note, setNote] = useState(''), [file, setFile] = useState<File | null>(null), [busy, setBusy] = useState(false), [error, setError] = useState('');
  async function save() { setBusy(true); setError(''); try { const form = new FormData(); form.set('state', state); form.set('event_at', date); form.set('note', note); if (file) form.set('file', file); await api(`/api/packages/${value.id}/receipt`, { method: 'POST', body: form }); notify('报送记录已追加，原资料快照保持不变'); saved(); close(); } catch (e) { setError(message(e)); } finally { setBusy(false); } }
  return <Modal title={`登记报送进展 · v${value.version}`} close={close} footer={<><button onClick={close}>取消</button><button className="primary" disabled={busy || !date} onClick={() => void save()}>{busy ? '保存中…' : '保存记录'}</button></>}><ErrorBox error={error} /><div className="form-grid"><label>进展<select value={state} onChange={e => setState(e.target.value)}><option value="submitted">已提交</option><option value="accepted">对方已接收</option><option value="returned">退回补件</option></select></label><label>实际时间<input type="datetime-local" value={date} onChange={e => setDate(e.target.value)} required /></label><label className="full">说明<textarea value={note} onChange={e => setNote(e.target.value)} rows={3} placeholder="接收人、反馈内容或补件要求" /></label><label className="full">回执附件<input type="file" accept=".png,.jpg,.jpeg,.pdf,.docx,.xlsx,.txt" onChange={e => setFile(e.target.files?.[0] || null)} /></label></div></Modal>;
}

function PackageViewer({ id, close, revision }: { id: string; close: () => void; revision: number }) {
  const { data, loading, error } = useData<PackageDetail>(`/api/packages/${id}`, revision);
  return <Modal title="报送快照与回执" close={close} wide footer={<><button onClick={close}>关闭</button>{data?.document_url && <a className="button primary" href={data.document_url}><Download size={15} />下载记录表</a>}{data && packageReady(data) && !data.document_url && <a className="button primary" href={data.download_url!}><Download size={15} />下载资料包</a>}</>}><ErrorBox error={error} />{loading ? <Loading /> : data && <><h3>{data.manifest.case.title}</h3><p className="muted">{data.manifest.case.project_name} · {place(data.manifest.case)} · 版本{data.manifest.version} · {dateTime(data.created_at)}</p><div className="table-wrap space-top"><table><thead><tr><th>阶段 / 用途</th><th>部位</th><th>水印时间</th><th>施工内容</th><th>原文件名</th></tr></thead><tbody>{data.manifest.photos.map(p => <tr key={p.id}><td>{STAGES[p.stage]} / {ROLES[p.role]}</td><td>{p.part || '—'}</td><td>{dateTime(p.watermark_time)}</td><td>{p.process || '—'}</td><td className="break-all">{p.original_name}</td></tr>)}</tbody></table></div><h3 className="space-top">提交与回执</h3>{data.events.length ? <div className="timeline">{data.events.map(event => <div key={event.id}><div className="row"><State value={event.state} /><span>{dateTime(event.event_at)}</span></div><p>{event.note || '未填写说明'}</p>{event.attachment_hash && <a href={`/api/receipts/${event.id}/attachment`}>下载回执附件</a>}</div>)}</div> : <p className="muted space-top">尚未登记实际提交或接收记录。</p>}<details className="space-top"><summary>快照校验信息</summary><p className="muted break-all">{data.document_url ? 'Word' : 'ZIP'} SHA-256：{data.zip_hash}</p><p className="muted">本页展示生成时的固定内容，后续案件信息修改不改变本版本。</p></details></>}</Modal>;
}

const EMPTY_FILTERS = { query: '', project: '', state: '', from: '', to: '' };
const PAGE_SIZE = 20;
function generatedDate(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value.slice(0, 10);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
}

export default function Reporting({ revision, cases, jobs, selected, createExport, supplement, refresh, notify }: { revision: number; cases: Case[]; jobs: Job[]; selected: string[]; createExport: () => void; supplement: (value: Package) => void; refresh: () => void; notify: Notify }) {
  const [filters, setFilters] = useState(EMPTY_FILTERS), [page, setPage] = useState(1);
  const [deleting, setDeleting] = useState<Package | null>(null);
  const [detail, setDetail] = useState<string | null>(null), [receipt, setReceipt] = useState<Package | null>(null);
  const jobStates = jobs.filter(job => job.kind === 'export').map(job => `${job.id}:${job.state}`).join('|');
  const { data, loading, error } = useData<Package[]>('/api/packages', `${revision}:${jobStates}`);
  const query = useDebounce(filters.query.trim().toLocaleLowerCase());
  const projects = useMemo(() => {
    const values = new Map<string, string>();
    data?.forEach(p => { if (p.project_id) values.set(p.project_id, p.project_name); });
    cases.forEach(c => values.set(c.project_id, c.project_name));
    return [...values].sort((a, b) => a[1].localeCompare(b[1], 'zh-CN'));
  }, [data, cases]);
  const invalidDates = !!(filters.from && filters.to && filters.from > filters.to);
  const filtered = useMemo(() => {
    if (invalidDates) return [];
    const words = query.split(/\s+/).filter(Boolean);
    return (data || []).filter(p => {
      const date = generatedDate(p.created_at);
      if (filters.project && p.project_id !== filters.project) return false;
      if (filters.state && p.state !== filters.state) return false;
      if (filters.from && date < filters.from || filters.to && date > filters.to) return false;
      const text = [p.case_title, p.title, p.project_name, p.short_code, p.platform_report_code, p.report_number,
        p.building, p.unit, p.room, p.location_detail, place(p)].filter(Boolean).join(' ').toLocaleLowerCase();
      return words.every(word => text.includes(word));
    });
  }, [data, query, filters.project, filters.state, filters.from, filters.to, invalidDates]);
  const pages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE)), currentPage = Math.min(page, pages);
  const active = Object.values(filters).some(Boolean);
  function update(key: keyof typeof filters, value: string) { setFilters(old => ({ ...old, [key]: value })); setPage(1); }
  function clear() { setFilters(EMPTY_FILTERS); setPage(1); }
  return <section className="workspace reporting-workspace">
    <div className="page-heading"><div><h1>报送管理</h1><p>查找资料包和历史版本，登记提交与回执</p></div><button className="primary" disabled={!selected.length} onClick={createExport}><Plus size={15} aria-hidden="true" />提取所选{selected.length ? `（${selected.length}项）` : ''}</button></div>
    <section className="reporting-filters" aria-label="报送记录筛选">
      <div className="reporting-filter-fields">
        <label className="reporting-search">查找资料<span className="reporting-search-input"><Search size={16} aria-hidden="true" /><input type="search" value={filters.query} onChange={e => update('query', e.target.value)} placeholder="短编号、报修/报案号、项目、房号或案件" /></span></label>
        <label>所属项目<select aria-label="所属项目" value={filters.project} onChange={e => update('project', e.target.value)}><option value="">全部项目</option>{projects.map(([id, name]) => <option value={id} key={id}>{name}</option>)}</select></label>
        <label>报送状态<select aria-label="报送状态" value={filters.state} onChange={e => update('state', e.target.value)}><option value="">全部状态</option><option value="ready">已生成</option><option value="submitted">已提交</option><option value="accepted">已接收</option><option value="returned">退回补件</option><option value="building">生成中</option><option value="failed">生成失败</option></select></label>
        <label>生成日期从<input type="date" value={filters.from} onChange={e => update('from', e.target.value)} max={filters.to || undefined} /></label>
        <label>生成日期至<input type="date" value={filters.to} onChange={e => update('to', e.target.value)} min={filters.from || undefined} /></label>
      </div>
      <div className="row between wrap reporting-filter-summary"><span className="muted" aria-live="polite">{loading ? '正在更新记录…' : `共 ${data?.length || 0} 条记录，符合条件 ${filtered.length} 条`}</span><button className="text-button" disabled={!active} onClick={clear}>清空筛选</button></div>
      {invalidDates && <ErrorBox error="开始日期不能晚于结束日期，请调整日期范围。" />}
    </section>
    <ErrorBox error={error} />
    {loading && !data ? <Loading /> : !error && !invalidDates && !filtered.length ? <Empty action={active ? <button onClick={clear}>清空筛选</button> : undefined}>{active ? '没有符合当前条件的报送记录，请调整关键词或筛选条件。' : '尚无报送记录。可在案件的保司资料页生成，或在照片列表勾选后提取。'}</Empty> : !invalidDates && !!filtered.length && <>
      <div className="table-wrap grow"><table className="reporting-table"><thead><tr><th>项目 / 案件</th><th>版本</th><th>资料</th><th>缺项</th><th>生成时间</th><th>状态</th><th>操作</th></tr></thead><tbody>{filtered.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE).map(p => <tr key={p.id}>
        <td><button className="text-button case-title" onClick={() => setDetail(p.id)}>{p.case_title}</button><small>{p.project_name}{p.parent_id ? ' · 补件版本' : ''}</small><small>{[p.short_code, place(p) === '位置待补充' ? '' : place(p)].filter(Boolean).join(' · ')}</small></td><td>v{p.version}</td><td>{p.photo_count}项</td><td>{p.missing_count ? <span className="warning-text">{p.missing_count}项</span> : '数量满足'}</td><td>{dateTime(p.created_at)}</td><td><State value={p.state} /></td><td><div className="row wrap">{packageReady(p) && <>{p.document_url && <a className="button compact-button" href={p.document_url}><Download size={13} />Word</a>}{!p.document_url && <a className="button compact-button" href={p.download_url!}><Download size={13} aria-hidden="true" />下载</a>}<button className="compact-button" onClick={() => setReceipt(p)}><Send size={13} aria-hidden="true" />登记进展</button><button className="compact-button" onClick={() => supplement(p)}>补件</button></>}<button className="compact-button photo-delete-button" aria-label={`删除资料包 v${p.version}`} onClick={() => setDeleting(p)}><Trash2 size={13} aria-hidden="true" />删除</button></div></td>
      </tr>)}</tbody></table></div>
      <div className="row between wrap reporting-pagination"><span className="muted">第 {(currentPage - 1) * PAGE_SIZE + 1}–{Math.min(currentPage * PAGE_SIZE, filtered.length)} 条 · 共 {filtered.length} 条</span><div className="row"><button disabled={currentPage <= 1} onClick={() => setPage(currentPage - 1)}>上一页</button><span>{currentPage} / {pages}</span><button disabled={currentPage >= pages} onClick={() => setPage(currentPage + 1)}>下一页</button></div></div>
    </>}
    {deleting && <DeletePackageDialog value={deleting} close={() => setDeleting(null)} deleted={() => { if (detail === deleting.id) setDetail(null); refresh(); }} notify={notify} />}
    {detail && <PackageViewer id={detail} close={() => setDetail(null)} revision={revision} />}{receipt && <ReceiptDialog value={receipt} close={() => setReceipt(null)} saved={refresh} notify={notify} />}
  </section>;
}
