import { useEffect, useState } from 'react';
import { Users } from 'lucide-react';
import { api, message, post } from './api';
import { dateTime, Empty, ErrorBox, Loading, Modal, place, WORKFLOW_STAGES } from './common';
import usePermissions from './usePermissions';
import type { Case, Notify } from './types';
import './case-workers.css';

type Worker = { id: string; display_name: string; username: string };
type Detail = { case_id: string; revision: number; workers: { user_id: string; display_name: string; enabled: boolean }[];
  history: { id: string; action: string; actor_name: string; created_at: string; before: { display_name: string }[] | null; after: { display_name: string }[] | { removed_user_ids: string[] } | null }[] };

export function WorkerCasePreview({ value, back }: { value: Case; back: () => void }) {
  return <section className="settings-card"><button onClick={back}>返回案件列表</button><h1>{value.platform_report_code || value.short_code || value.title}</h1>
    <p>{value.project_name} · {place(value)}</p><p>{value.title}</p><p>办理阶段：{WORKFLOW_STAGES[value.workflow_stage]}</p>
    <p className="notice">你是本项目的工人。请使用现有相机同步案件、拍摄和上传，并在相机内处理本人拍摄记录。</p>
  </section>;
}

export function WorkerNames({ value }: { value: Case }) {
  return <span className={value.workers?.length ? 'worker-names' : 'muted'}>{value.workers?.map(w => w.display_name).join('、') || '未分配工人'}</span>;
}

export default function CaseWorkers({ value, refresh, notify }: { value: Case; refresh: () => void; notify: Notify }) {
  const { canManage } = usePermissions();
  const [open, setOpen] = useState(false);
  return <section className="case-workers-bar" aria-label="现场工人"><span><Users size={16} /><b>现场工人</b><WorkerNames value={value} /></span>
    {canManage(value.project_id) && <button onClick={() => setOpen(true)}>分配工人</button>}
    {open && <WorkerAssignmentDialog cases={[value]} close={() => setOpen(false)} saved={() => { refresh(); notify('工人分配已保存，手机同步后更新案件清单'); }} />}
  </section>;
}

export function WorkerAssignmentDialog({ cases, close, saved }: { cases: Case[]; close: () => void; saved: () => void }) {
  const single = cases.length === 1, projectId = cases[0].project_id;
  const [workers, setWorkers] = useState<Worker[]>([]), [details, setDetails] = useState<Detail[]>([]);
  const [selected, setSelected] = useState<string[]>([]), [mode, setMode] = useState(single ? 'replace' : 'add');
  const [loading, setLoading] = useState(true), [busy, setBusy] = useState(false), [error, setError] = useState('');
  const [reload, setReload] = useState(0);
  useEffect(() => {
    const abort = new AbortController(); setLoading(true); setError('');
    Promise.all([api<Worker[]>(`/api/access/projects/${projectId}/workers`, { signal: abort.signal }),
      Promise.all(cases.map(c => api<Detail>(`/api/cases/${c.id}/workers`, { signal: abort.signal })))])
      .then(([available, latest]) => { if (!abort.signal.aborted) { setWorkers(available); setDetails(latest); setSelected(single ? latest[0].workers.map(w => w.user_id) : []); } })
      .catch(e => { if (!abort.signal.aborted) setError(message(e)); }).finally(() => { if (!abort.signal.aborted) setLoading(false); });
    return () => abort.abort();
  }, [projectId, reload]);
  const disabledWorkers = single ? (details[0]?.workers || []).filter(w => !workers.some(v => v.id === w.user_id)) : [];
  async function save() {
    setBusy(true); setError('');
    try {
      await post('/api/access/case-assignments', { case_ids: cases.map(c => c.id), user_ids: selected, mode,
        expected_revisions: Object.fromEntries(details.map(d => [d.case_id, d.revision])) });
      saved(); close();
    } catch (e) { setError(message(e)); } finally { setBusy(false); }
  }
  function toggle(id: string, checked: boolean) { setSelected(v => checked ? [...v, id] : v.filter(x => x !== id)); }
  return <Modal title={single ? '分配现场工人' : `批量分配工人 · ${cases.length} 案`} close={() => { if (!busy) close(); }}
    footer={<><button disabled={busy} onClick={close}>取消</button><button className="primary" disabled={busy || loading || details.length !== cases.length || mode !== 'replace' && !selected.length} onClick={() => void save()}>{busy ? '正在保存…' : '保存分配'}</button></>}>
    <p><b>{cases[0].project_name}</b> · {single ? cases[0].platform_report_code || cases[0].short_code || cases[0].title : `已选择 ${cases.length} 个案件`}</p>
    <p className="muted">工人使用现有相机同步案件。取消关联后无法继续上传，原有照片和拍摄记录保留。</p>
    <ErrorBox error={error} />{error && <button disabled={busy || loading} onClick={() => setReload(v => v + 1)}>重新读取分配状态</button>}
    {!single && <label className="inline-field space-top">分配方式<select aria-label="分配方式" value={mode} disabled={busy} onChange={e => setMode(e.target.value)}><option value="add">添加所选工人（保留原有工人）</option><option value="remove">移除所选工人</option><option value="replace">替换为所选工人</option></select></label>}
    {loading ? <Loading /> : <div className="worker-options" aria-label="选择现场工人">
      {workers.map(w => <label key={w.id}><input type="checkbox" checked={selected.includes(w.id)} disabled={busy} onChange={e => toggle(w.id, e.target.checked)} /><span>{w.display_name}<small>{w.username}</small></span></label>)}
      {disabledWorkers.map(w => <label key={w.user_id}><input type="checkbox" checked={selected.includes(w.user_id)} disabled={busy} onChange={e => toggle(w.user_id, e.target.checked)} /><span>{w.display_name}<small>账号已停用或角色已变化，请取消关联</small></span></label>)}
      {!workers.length && !disabledWorkers.length && <Empty>本项目还没有启用的工人，请由系统管理员在“人员与权限”中设置工人角色。</Empty>}
    </div>}
    {mode === 'replace' && !selected.length && <p className="notice">保存后将取消这些案件的全部工人关联。</p>}
    {single && !!details[0]?.history.length && <details className="worker-history"><summary>分配记录（最近 30 条）</summary>{details[0].history.map(row => <div key={row.id}><strong>{row.actor_name}</strong><small>{dateTime(row.created_at)}</small><p>{row.action === 'membership_removed' ? '人员项目权限变更，已取消相关关联' : `工人：${row.before?.map(w => w.display_name).join('、') || '未分配'} → ${Array.isArray(row.after) ? row.after.map(w => w.display_name).join('、') || '未分配' : '未分配'}`}</p></div>)}</details>}
  </Modal>;
}
