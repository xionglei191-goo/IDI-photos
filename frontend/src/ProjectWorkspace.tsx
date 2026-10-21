import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { Archive, ArrowLeft, FileImage, RefreshCw, Upload } from 'lucide-react';
import { api, message } from './api';
import { useAuth, PersonMenu } from './Auth';
import { dateTime, Empty, ErrorBox, Loading, Modal, PHOTO_GROUPS, place, useData, WORKFLOW_STAGES } from './common';
import { PhotoEditor, CaseEditor } from './editors';
import DeletePhotosDialog from './DeletePhotosDialog';
import PlatformWorkspace from './PlatformWorkspace';
import { WebsiteOperations } from './AccessSettings';
import type { Case, PhotoPage, Project } from './types';
import type { PlatformStage } from './platformTypes';
import './workflow.css';

export default function ProjectWorkspace() {
  const { person } = useAuth();
  const [revision, setRevision] = useState(0), [projectId, setProjectId] = useState(''), [caseId, setCaseId] = useState('');
  const [tab, setTab] = useState('photos'), [query, setQuery] = useState(''), [photoId, setPhotoId] = useState('');
  const [upload, setUpload] = useState(false), [deleteIds, setDeleteIds] = useState<string[] | null>(null), [editingCase, setEditingCase] = useState(false);
  const [toast, setToast] = useState(''), [error, setError] = useState('');
  const [importJob, setImportJob] = useState('');
  const [photoPage, setPhotoPage] = useState(1);
  const projects = useData<Project[]>('/api/projects', revision), cases = useData<Case[]>('/api/cases', revision);
  const photos = useData<PhotoPage>(caseId ? `/api/photos?case_id=${encodeURIComponent(caseId)}&page=${photoPage}&page_size=60&sort=created_at&direction=desc` : null, revision);
  useEffect(() => setPhotoPage(1), [caseId]);
  const leaveRef = useRef<(() => Promise<boolean>) | null>(null);
  const registerLeave = useCallback((value: (() => Promise<boolean>) | null) => { leaveRef.current = value; }, []);
  const refresh = useCallback(() => setRevision(v => v + 1), []);
  const notify = useCallback((value: string, failed = false) => { if (failed) setError(value); else setToast(value); }, []);
  const selected = cases.data?.find(c => c.id === caseId);
  const manager = !!selected && person.projects[selected.project_id] === 'manager';
  const managedCases = (cases.data || []).filter(c => person.projects[c.project_id] === 'manager');
  const managedProjects = (projects.data || []).filter(p => person.projects[p.id] === 'manager');
  const stage: PlatformStage = ({ survey: 'survey', repair: 'repair', acceptance: 'acceptance', settlement: 'acceptance', closed: 'acceptance' } as Record<string, PlatformStage>)[selected?.workflow_stage || ''] || 'intake';
  const visible = (cases.data || []).filter(c => (!projectId || c.project_id === projectId) && (!query || [c.title, c.short_code, c.platform_report_code, c.room, c.project_name].some(v => v?.includes(query))));
  async function navigate(action: () => void) { if (!leaveRef.current || await leaveRef.current()) { setError(''); action(); } }
  useEffect(() => { if (!toast) return; const timer = window.setTimeout(() => setToast(''), 6500); return () => window.clearTimeout(timer); }, [toast]);
  return <div className="project-workspace">
    <header className="app-header"><a className="brand" href="/"><Archive size={22} /><span>IDI 项目工作台</span></a><div className="header-end"><a className="button" href="/camera/download">安卓 APP</a><PersonMenu /></div></header>
    <main><div className="project-toolbar"><label>项目<select value={projectId} onChange={e => void navigate(() => { setProjectId(e.target.value); setCaseId(''); setTab('photos'); })}><option value="">我的全部项目</option>{projects.data?.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label><label>查找案件<input value={query} onChange={e => setQuery(e.target.value)} placeholder="编号、房号或案件内容" /></label><button onClick={refresh}><RefreshCw size={15} />刷新</button></div>
      <ErrorBox error={error || projects.error || cases.error} />{toast && <p className="notice" role="status">{toast}</p>}
      {importJob && <ImportProgress key={importJob} id={importJob} done={refresh} />}
      {!selected ? <section className="settings-card"><h1>我的项目案件</h1><p className="muted">选择案件后上传照片。项目成员可以查看和上传；项目管理员可管理照片并核对正式提交。</p>{cases.loading ? <Loading /> : !visible.length ? <Empty>当前没有分配给你的项目案件，请联系系统管理员分配。</Empty> : <div className="table-wrap"><table><thead><tr><th>项目 / 案件号</th><th>位置 / 问题</th><th>办理阶段</th><th>本项目权限</th><th /></tr></thead><tbody>{visible.map(c => <tr key={c.id}>
        <td>{c.project_name}<small>{c.platform_report_code || c.short_code || '待关联'}</small></td><td>{place(c)}<small>{c.title}</small></td><td>{WORKFLOW_STAGES[c.workflow_stage]}</td><td>{person.projects[c.project_id] === 'manager' ? '项目管理员' : '项目成员'}</td><td><button onClick={() => { setCaseId(c.id); setTab('photos'); }}>打开案件</button></td>
      </tr>)}</tbody></table></div>}<WebsiteOperations projectId={projectId} revision={revision} /></section> : <>
        <button className="text-button" onClick={() => void navigate(() => setCaseId(''))}><ArrowLeft size={15} />返回案件列表</button>
        <div className="page-heading"><div><h1>{selected.platform_report_code || selected.short_code || selected.title}</h1><p>{selected.project_name} · {place(selected)} · {selected.title}</p></div><div className="row"><button className="primary" onClick={() => setUpload(true)}><Upload size={15} />上传本案照片</button>{manager && <button onClick={() => setEditingCase(true)}>编辑案件</button>}</div></div>
        <p className="project-member-hint">照片收到后进入自动处理和官网保存队列；正式提交由项目管理员核对确认。</p>
        <CaseUploadStatus caseId={caseId} />
        <nav className="tabs"><button className={tab === 'photos' ? 'active' : ''} onClick={() => void navigate(() => setTab('photos'))}>本案照片</button>{manager && <button className={tab === 'website' ? 'active' : ''} onClick={() => void navigate(() => setTab('website'))}>官网资料与正式提交</button>}<button className={tab === 'history' ? 'active' : ''} onClick={() => void navigate(() => setTab('history'))}>官网操作记录</button></nav>
        {tab === 'photos' && <><ErrorBox error={photos.error} />{photos.loading ? <Loading /> : !photos.data?.items.length ? <Empty>本案还没有照片，可以使用 APP 拍摄或上传已有照片。</Empty> : <><div className="project-photo-grid">{photos.data.items.map(p => <article className="project-photo" key={p.id}>
          <a href={p.original_url} target="_blank" rel="noreferrer" aria-label={`查看原图 ${p.original_name}`}>{p.thumbnail_url ? <img src={p.thumbnail_url} alt={p.original_name} loading="lazy" /> : <FileImage size={40} />}</a>
          <div><p>{p.original_name}</p><small>{PHOTO_GROUPS[p.photo_group] || '待分类'} · {p.part || '部位待补'}</small><small>{dateTime(p.watermark_time || p.created_at)}</small><small>上传人员：{p.uploaded_by_name || '历史记录 / 系统收图'}</small><div className="row"><a href={`${p.original_url}&download=true`}>下载原件</a>{manager && <><button onClick={() => setPhotoId(p.id)}>编辑</button><button onClick={() => setDeleteIds([p.id])}>删除</button></>}</div></div>
        </article>)}</div>{photos.data.total > 60 && <nav className="row space-top" aria-label="照片分页"><button disabled={photoPage === 1} onClick={() => setPhotoPage(v => v - 1)}>上一页</button><span>第{photoPage}页 / 共{Math.ceil(photos.data.total / 60)}页 · {photos.data.total}项</span><button disabled={photoPage * 60 >= photos.data.total} onClick={() => setPhotoPage(v => v + 1)}>下一页</button></nav>}</>}</>}
        {tab === 'website' && manager && <PlatformWorkspace key={selected.id} cases={managedCases} initialCaseId={selected.id} initialStage={stage} embedded revision={revision} notify={notify} registerLeave={registerLeave} importFiles={() => setUpload(true)} onChanged={refresh} />}
        {tab === 'history' && <WebsiteOperations projectId={selected.project_id} revision={revision} />}
        {upload && <UploadToCase value={selected} close={() => setUpload(false)} saved={job => { setImportJob(job); refresh(); }} />}
        {photoId && manager && <PhotoEditor photoId={photoId} cases={managedCases} projects={managedProjects} close={() => setPhotoId('')} saved={refresh} notify={notify} />}
        {deleteIds && manager && <DeletePhotosDialog ids={deleteIds} close={() => setDeleteIds(null)} deleted={() => { setDeleteIds(null); refresh(); }} notify={notify} />}
        {editingCase && manager && <CaseEditor value={selected} projects={managedProjects} close={() => setEditingCase(false)} saved={refresh} notify={notify} />}
      </>}
    </main>
  </div>;
}

function CaseUploadStatus({ caseId }: { caseId: string }) {
  const [revision, setRevision] = useState(0);
  useEffect(() => { const timer = window.setInterval(() => setRevision(v => v + 1), 5000); return () => window.clearInterval(timer); }, []);
  const status = useData<{ state: string; detail: { reason?: string; saved_count?: number } }>(`/api/workflow/uploads/cases/${caseId}`, revision);
  return <div role="status" className="muted"><ErrorBox error={status.error} />{status.data?.detail?.reason || '上传状态：等待本案新资料。'}</div>;
}

function ImportProgress({ id, done }: { id: string; done: () => void }) {
  const [revision, setRevision] = useState(0), finished = useRef(false);
  const job = useData<{ state: string; error: string; result: { failed?: number; imported?: number } }>(`/api/jobs/${id}`, revision);
  const terminal = !!job.data && ['completed', 'partial', 'failed'].includes(job.data.state);
  useEffect(() => {
    if (terminal) { if (!finished.current) { finished.current = true; done(); } return; }
    const timer = window.setInterval(() => setRevision(v => v + 1), 2000);
    return () => window.clearInterval(timer);
  }, [terminal, done]);
  return <div className="notice" role="status"><ErrorBox error={job.error || job.data?.error || ''} />
    {job.data?.state === 'completed' ? '照片归档完成，官网保存进度将在本案显示。'
      : job.data?.state === 'partial' ? `部分文件未能归档：成功 ${job.data.result.imported || 0} 项，失败 ${job.data.result.failed || 0} 项。请检查失败文件后重新上传。`
        : job.data?.state === 'failed' ? '本次归档失败，原上传未标记为成功。'
          : '照片已收到，正在等待归档处理…'}</div>;
}

function UploadToCase({ value, close, saved }: { value: Case; close: () => void; saved: (job: string) => void }) {
  const [files, setFiles] = useState<File[]>([]), [busy, setBusy] = useState(false), [error, setError] = useState('');
  async function submit(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError('');
    try {
      const data = new FormData(); data.set('project_id', value.project_id); data.set('case_id', value.id); data.set('auto_ocr', 'true');
      files.forEach(file => data.append('files', file)); data.set('paths', JSON.stringify(files.map(f => f.name)));
      const result = await api<{ job: { id: string } }>('/api/import/upload', { method: 'POST', body: data }); saved(result.job.id); close();
    } catch (e) { setError(message(e)); }
    finally { setBusy(false); }
  }
  return <Modal title="上传本案照片" close={() => { if (!busy) close(); }}><p>{value.project_name} · {value.platform_report_code || value.short_code} · {place(value)}</p><ErrorBox error={error} /><form className="access-form" onSubmit={submit}><label>选择照片或附件<input type="file" required multiple accept="image/*,video/*,.pdf" disabled={busy} onChange={e => setFiles(Array.from(e.target.files || []))} /></label><p className="muted">原件保留。APP 拍摄资料含结构化案件与用途信息；普通照片会先进行识别和归类。</p><button className="primary" disabled={busy || !files.length}>{busy ? '正在上传…' : `上传${files.length || ''}个文件`}</button></form></Modal>;
}
