import { useState } from 'react';
import { Download, LoaderCircle, RefreshCw, Trash2 } from 'lucide-react';
import { dateTime, ErrorBox, Loading, State, useData } from './common';
import type { Job, Notify, Package } from './types';
import DeletePackageDialog from './DeletePackageDialog';
import DefaultPackageCard from './DefaultPackageCard';

export function reusedPackageMessage(job: Job) {
  const reused = job.result?.packages?.filter(p => p.reused) || [];
  if (job.state !== 'completed' || !reused.length) return '';
  return reused.length === 1 ? `资料未变化，已复用资料包 v${reused[0].version}` : `资料未变化，已复用 ${reused.length} 个已有资料包`;
}

export const packageReady = (value: Package) => !!value.download_url && ['ready', 'submitted', 'accepted', 'returned'].includes(value.state);
export function exportJobsForCase(jobs: Job[], caseId: string) {
  return jobs.filter(job => job.kind === 'export' && Array.isArray(job.payload.groups)
    && job.payload.groups.some(group => group?.case?.id === caseId));
}

export default function CasePackages({ caseId, revision, jobs, refresh, notify, readOnly = false }: { readOnly?: boolean; caseId: string; revision: number; jobs: Job[]; refresh: () => void; notify: Notify }) {
  const jobStates = jobs.map(job => `${job.id}:${job.state}`).join('|');
  const { data, loading, error } = useData<Package[]>(`/api/packages?case_id=${encodeURIComponent(caseId)}`, `${revision}:${jobStates}`);
  const [expanded, setExpanded] = useState(false);
  const [deleting, setDeleting] = useState<Package | null>(null);
  const pending = jobs.filter(job => ['queued', 'running'].includes(job.state));
  const latest = jobs[0];
  const failed = latest && ['failed', 'partial'].includes(latest.state) ? latest : null;
  return <section className="case-panel case-packages" aria-label="本案资料包">
    <div className="row between wrap"><div><h2>本案资料包</h2><p className="muted">普通方案下载分类照片与视频；人保方案只下载 Word 记录表。照片每张≤400 KB，视频保留原文件；内容未变时复用已有版本。</p></div><button onClick={refresh} disabled={loading}><RefreshCw size={14} aria-hidden="true" />刷新列表</button></div>
    <DefaultPackageCard readOnly={readOnly} caseId={caseId} revision={`${revision}:${jobStates}`} refresh={refresh} notify={notify} />
    {latest && reusedPackageMessage(latest) && data?.some(p => latest.result?.packages?.some(r => r.reused && r.id === p.id)) && <p className="success-note" role="status">{reusedPackageMessage(latest)}</p>}
    {pending.map(job => <div className="notice row" role="status" key={job.id}><LoaderCircle className="spin" size={17} aria-hidden="true" /><span>{job.state === 'queued' ? '资料包已排队，正在等待生成…' : '资料包正在生成…'}完成后下载链接会自动出现在下方。</span></div>)}
    {failed && <ErrorBox error={`最近一次生成未完成：${failed.error || '请重新生成资料包。'}`} />}
    <ErrorBox error={error} />
    {loading && !data ? <Loading /> : data?.length ? <div className="package-download-list">
      {(expanded ? data : data.slice(0, 3)).map(value => <div className="package-download-item" key={value.id}>
        <div><strong>{value.title || '保司资料包'} · v{value.version}{value.parent_id ? ' · 补件' : ''}</strong><small>{dateTime(value.created_at)} · {value.photo_count} 项资料{value.missing_count ? ` · 缺 ${value.missing_count} 项` : ''}</small></div>
        <div className="row wrap"><State value={value.state} />{packageReady(value) && value.document_url && <a className="button primary" href={value.document_url}><Download size={15} aria-hidden="true" />下载记录表 v{value.version}</a>}{packageReady(value) && !value.document_url && <a className="button primary" href={value.download_url!}><Download size={15} aria-hidden="true" />下载资料包 v{value.version}</a>}<button className="photo-delete-button" disabled={readOnly || pending.length > 0} onClick={() => setDeleting(value)} aria-label={`删除资料包 v${value.version}`}><Trash2 size={15} aria-hidden="true" />删除</button></div>
      </div>)}
      {data.length > 3 && <button className="text-button" onClick={() => setExpanded(!expanded)}>{expanded ? '收起历史版本' : `查看其余 ${data.length - 3} 个历史版本`}</button>}
    </div> : !error && !pending.length && <p className="muted space-top">本案尚无资料包。点击上方生成按钮后，下载链接将显示在这里。</p>}
    {deleting && <DeletePackageDialog value={deleting} close={() => setDeleting(null)} deleted={refresh} notify={notify} />}
  </section>;
}
