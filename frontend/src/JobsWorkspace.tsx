import { useState } from 'react';
import { Download, RefreshCw } from 'lucide-react';
import { message, post } from './api';
import { dateTime, Empty, ErrorBox, Modal, State, useData } from './common';
import type { Batch, Job, Notify } from './types';

const KINDS: Record<string, string> = { import: '资料导入', ledger: '台账导入', recognize: '照片识别', export: '生成报送包', backup: '档案备份', archive_tree: '同步资料目录' };
function JobDetails({ job, close }: { job: Job; close: () => void }) {
  const batchId = typeof job.payload.batch_id === 'string' ? job.payload.batch_id : null;
  const batch = useData<Batch>(batchId ? `/api/imports/${batchId}` : null, job.progress);
  return <Modal title="任务结果" close={close} wide footer={<button onClick={close}>关闭</button>}><div className="row"><h3>{KINDS[job.kind] || job.kind}</h3><State value={job.state} /><span className="muted">{dateTime(job.created_at)}</span></div><ErrorBox error={job.error || batch.error} />{batch.data && <><p className="space-top">导入{batch.data.imported}项 · 重复{batch.data.duplicates}项 · 失败{batch.data.failed}项</p><div className="table-wrap space-top"><table><thead><tr><th>来源文件 / 行</th><th>结果</th><th>说明</th></tr></thead><tbody>{batch.data.items?.map(item => <tr key={item.id}><td className="break-all">{item.source_name}</td><td><State value={item.status} /></td><td>{item.error || '—'}</td></tr>)}</tbody></table></div></>}{job.result?.errors?.length ? <div className="table-wrap space-top"><table><thead><tr><th>失败原因</th></tr></thead><tbody>{job.result.errors.map((item, index) => <tr key={index}><td>{item.error}</td></tr>)}</tbody></table></div> : null}{job.result?.packages?.map(p => <p key={p.id} className="space-top"><a className="button" href={p.download_url}><Download size={14} />下载生成的资料包</a></p>)}{job.result?.download_url && <a className="button space-top" href={job.result.download_url}><Download size={14} />下载备份</a>}<details className="space-top"><summary>任务记录</summary><pre>{JSON.stringify({ attempts: job.attempts, progress: job.progress, total: job.total, result: job.result }, null, 2)}</pre></details></Modal>;
}

export default function JobsWorkspace({ jobs, refresh, notify }: { jobs: Job[]; refresh: () => void; notify: Notify }) {
  const [selected, setSelected] = useState<string | null>(null), [busy, setBusy] = useState<string | null>(null);
  const detail = jobs.find(j => j.id === selected);
  async function retry(id: string) { setBusy(id); try { await post(`/api/jobs/${id}/retry`); notify('任务已重新排队'); refresh(); } catch (e) { notify(message(e), true); } finally { setBusy(null); } }
  return <section className="workspace"><div className="page-heading"><div><h1>任务记录</h1><p>导入、识别、导出与备份的实际运行结果</p></div><button onClick={refresh}><RefreshCw size={15} />刷新</button></div>{!jobs.length ? <Empty>尚无任务</Empty> : <div className="table-wrap grow"><table><thead><tr><th>任务</th><th>状态</th><th>进度</th><th>创建时间</th><th>结果 / 错误</th><th>操作</th></tr></thead><tbody>{jobs.map(job => <tr key={job.id}><td>{KINDS[job.kind] || job.kind}</td><td><State value={job.state} /></td><td>{job.total ? <><progress value={job.progress} max={job.total} /><small>{job.progress} / {job.total}</small></> : '—'}</td><td>{dateTime(job.created_at)}</td><td className="error-cell">{job.error || (job.result?.failed ? `${job.result.failed}项失败` : job.state === 'completed' ? '处理完成' : '')}</td><td><div className="row"><button className="compact-button" onClick={() => setSelected(job.id)}>查看结果</button>{['failed', 'partial'].includes(job.state) && <button className="compact-button" disabled={busy === job.id} onClick={() => void retry(job.id)}>重试</button>}</div></td></tr>)}</tbody></table></div>}{detail && <JobDetails job={detail} close={() => setSelected(null)} />}</section>;
}
