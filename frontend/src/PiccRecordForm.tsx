import { useCallback, useEffect, useRef, useState } from 'react';
import { FileText, RefreshCw, Save, Trash2, Undo2 } from 'lucide-react';
import { api, ApiError, message, post, put } from './api';
import { ErrorBox, Loading } from './common';
import { reusedPackageMessage } from './CasePackages';
import type { Job, Notify, PiccRecord } from './types';

const FIELDS = [['project_name', '项目名称', 120], ['location', '楼栋房号', 120], ['survey_content', '查勘内容', 200]] as const;
const PAGE_SIZE = 20;

export default function PiccRecordForm({ caseId, revision = 0, generating = false, generated, notify, registerLeave, parentId, title, readOnly = false }: {
  readOnly?: boolean; caseId: string; revision?: number; generating?: boolean; generated: (job: Job) => void; notify: Notify;
  registerLeave?: (callback: (() => Promise<boolean>) | null) => void; parentId?: string | null; title?: string;
}) {
  const [record, setRecord] = useState<PiccRecord | null>(null), [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [page, setPage] = useState(1);
  const [conflict, setConflict] = useState(false);
  const dirtyRef = useRef(false);
  dirtyRef.current = dirty;
  useEffect(() => {
    if (dirtyRef.current) return;
    const abort = new AbortController();
    api<PiccRecord>(`/api/cases/${caseId}/picc-record`, { signal: abort.signal }).then(incoming => {
      if (!dirtyRef.current) setRecord(current => current && current.revision > incoming.revision ? current : incoming);
    }).catch(e => { if (!abort.signal.aborted) setError(message(e)); });
    return () => abort.abort();
  }, [caseId, revision]);
  const save = useCallback(async (showMessage = true, photoAction?: { id: string; excluded: boolean }) => {
    if (!record) return null;
    setBusy(true); setError('');
    try {
      const saved = await put<PiccRecord>(`/api/cases/${caseId}/picc-record`, { revision: record.revision, fields: record.fields, photo_action: photoAction });
      setRecord(saved); setDirty(false); dirtyRef.current = false; setConflict(false);
      if (showMessage) notify(photoAction ? photoAction.excluded ? '已从记录表移除，案件原照片保留，可在下方恢复' : '照片已恢复到记录表' : '记录表填写内容已保存');
      return saved;
    } catch (e) { setError(message(e)); setConflict(e instanceof ApiError && e.status === 409); return null; }
    finally { setBusy(false); }
  }, [caseId, record, notify]);
  useEffect(() => { registerLeave?.(async () => !busy && (!dirty || !!await save(false))); return () => registerLeave?.(null); }, [dirty, busy, save, registerLeave]);
  useEffect(() => {
    if (!dirty) return;
    const beforeUnload = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', beforeUnload);
    return () => window.removeEventListener('beforeunload', beforeUnload);
  }, [dirty]);
  function field(key: string, value: string) { dirtyRef.current = true; setRecord(old => old && ({ ...old, fields: { ...old.fields, [key]: value } })); setDirty(true); }
  const photos = record?.photos || [];
  const pages = Math.max(1, Math.ceil(photos.length / PAGE_SIZE)), currentPage = Math.min(page, pages);
  async function reload(discard = false) {
    if (!discard && dirty && !await save(false)) return;
    setBusy(true); setError('');
    try { setRecord(await api<PiccRecord>(`/api/cases/${caseId}/picc-record`)); setDirty(false); dirtyRef.current = false; setConflict(false); } catch (e) { setError(message(e)); } finally { setBusy(false); }
  }
  async function generate() {
    const saved = dirty ? await save(false) : record;
    if (!saved) return;
    if (saved.blockers.length) { setError(saved.blockers.join('；')); return; }
    setBusy(true); setError('');
    try {
      const job = await post<Job>('/api/packages', { photo_ids: [saved.photos[0].id], template_id: 'picc-record-v1', parent_id: parentId || null, title: title || '人保查勘及施工记录表' });
      notify(reusedPackageMessage(job) || '人保记录表已排队，完成后可在下方下载 Word'); generated(job);
    } catch (e) { setError(message(e)); } finally { setBusy(false); }
  }
  return <section className="picc-record-form" aria-label="人保记录表填写">
    <div className="row between wrap"><div><h3>人保查勘及施工记录表</h3><p className="muted">照片自动去重后按时间插入 Word，备注栏留空。移除仅影响本表，案件原照片保留。</p></div><button disabled={busy} onClick={() => void reload()}><RefreshCw size={14} />刷新照片</button></div>
    <ErrorBox error={error} />
    {conflict && <button disabled={busy} onClick={() => void reload(true)}>重新载入（放弃本页未保存填写）</button>}
    {!record ? !error && <Loading /> : <>
      <fieldset disabled={busy || generating} className="picc-fields">
        <div className="form-grid">{FIELDS.map(([key, label, limit]) => <label key={key} className={key === 'survey_content' ? 'full' : ''}>{label}<input readOnly={readOnly} required type="text" maxLength={limit} value={record.fields[key]} onChange={e => field(key, e.target.value)} /></label>)}<label>查勘日期<input aria-label="查勘日期" type="date" readOnly value={record.fields.survey_date} /><small className="muted">自动填写 · {record.survey_date_source}</small></label></div>
        <div className="picc-photo-heading"><strong>照片顺序 · 全部 {photos.length} 张</strong><span className="muted">优先按水印时间排序，没有水印时间时按上传时间排序。</span></div>
        {!!record.duplicate_count && <p className="muted">已合并 {record.duplicate_count} 张内容相同的重复照片。</p>}
        <div className="picc-photo-list">{photos.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE).map(p => <article className="picc-photo-row" key={p.id}>
          <a href={`/api/photos/${p.id}/original`} target="_blank" rel="noreferrer" aria-label={`查看原图 ${p.original_name}`}>{p.thumbnail_url ? <img src={p.thumbnail_url} alt={p.original_name} loading="lazy" /> : <FileText size={30} />}</a>
          <div><strong>{photos.indexOf(p) + 1}. {p.original_name}</strong><time dateTime={p.sort_time || undefined}>{p.sort_time?.slice(0, 19).replace('T', ' ') || '时间异常'}</time><br /><small className="muted">{p.time_label}</small></div>
          <button className="picc-remove" disabled={readOnly} aria-label={`从记录表移除 ${p.original_name}`} onClick={() => void save(true, { id: p.id, excluded: true })}><Trash2 size={14} />移除</button>
        </article>)}</div>
        {pages > 1 && <div className="row between"><span className="muted">第 {currentPage} / {pages} 页</span><div className="row"><button onClick={() => setPage(currentPage - 1)} disabled={currentPage <= 1}>上一页</button><button onClick={() => setPage(currentPage + 1)} disabled={currentPage >= pages}>下一页</button></div></div>}
        {!!record.excluded_photos?.length && <details className="picc-removed"><summary>已从本表移除 {record.excluded_photos.length} 张 · 可恢复</summary><div className="picc-photo-list">{record.excluded_photos.map(p => <article className="picc-photo-row" key={p.id}><a href={`/api/photos/${p.id}/original`} target="_blank" rel="noreferrer" aria-label={`查看已移除原图 ${p.original_name}`}>{p.thumbnail_url ? <img src={p.thumbnail_url} alt={p.original_name} loading="lazy" /> : <FileText size={30} />}</a><div><strong>{p.original_name}</strong><small className="muted">案件原照片已保留</small></div><button className="picc-remove" disabled={readOnly} aria-label={`恢复到记录表 ${p.original_name}`} onClick={() => void save(true, { id: p.id, excluded: false })}><Undo2 size={14} />恢复</button></article>)}</div></details>}
      </fieldset>
      <p className="muted">照片每张 ≤400 KB。{record.video_count ? `${record.video_count} 个视频请切换到原照片打包方案下载。` : '本方案生成可编辑的 Word 记录表。'}{record.excluded_file_count > 0 ? `另有 ${record.excluded_file_count} 个 PDF 或其他文件不插入本表。` : ''}</p>
      <div className="row wrap picc-actions"><span className="grow muted" role="status">{dirty ? '有未保存的填写内容' : record.revision ? '填写内容已保存；查勘日期与照片顺序自动更新。' : '核对表头后即可生成，查勘日期与照片顺序自动处理。'}</span><button disabled={readOnly || busy || generating || !dirty} onClick={() => void save()}><Save size={15} />保存填写</button><button className="primary" disabled={readOnly || busy || generating || !photos.length} onClick={() => void generate()}><FileText size={15} />{generating ? '记录表生成中…' : busy ? '处理中…' : '生成记录表'}</button></div>
    </>}
  </section>;
}
