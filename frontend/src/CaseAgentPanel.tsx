import usePermissions from './usePermissions';
import { useEffect, useState } from 'react';
import { CheckCircle2, FileSearch, LoaderCircle, RefreshCw, Square } from 'lucide-react';
import { api, message, post } from './api';
import { dateTime, Empty, ErrorBox, Loading, Modal, PHOTO_GROUPS } from './common';
import type { Case, Notify } from './types';
import './case-agent.css';

interface Finding {
  kind: 'visible_issue' | 'needs_check' | 'archive_note'; certainty: string;
  title: string; detail: string; action: string; photo_ids: string[]; message_ids: string[];
}
interface AgentRun {
  id: string; case_id: string; state: string; current: boolean; model: string; steps: number;
  error: string | null; created_at: string; finished_at: string | null; adopted: number[];
  case_context: { code: string; location: string; title: string; stage: string };
  photos: { id: string; name: string; photo_group: string; watermark_time: string | null }[];
  messages: { id: string; content: string; sent_at: string | null; received_at: string }[];
  trace: { step: number; tool: string; outcome: string; created_at: string }[];
  usage: { requests?: number; prompt_tokens?: number; completion_tokens?: number };
  result: { summary: string; findings: Finding[]; limitations: string[]; scope: string; viewed_photo_ids: string[]; total_images: number } | null;
}
interface AgentHistory {
  latest: AgentRun | null; history: { id: string; state: string; created_at: string }[];
  ready: boolean; image_count: number; limits: { steps: number; images: number; seconds: number };
}
const STATES: Record<string, string> = { queued: '等待审阅', running: '正在审阅', completed: '审阅完成', partial: '部分审阅', failed: '审阅未完成', cancelled: '已停止' };
const KINDS = { visible_issue: '可见问题', needs_check: '待核对', archive_note: '内部归档提示' };

export default function CaseAgentPanel({ value, notify, onDraft }: { value: Case; notify: Notify; onDraft?: (projectId: string) => void }) {
  const { canManage } = usePermissions();
  const manageable = canManage(value.project_id);
  const [data, setData] = useState<AgentHistory | null>(null), [run, setRun] = useState<AgentRun | null>(null);
  const [selectedRun, setSelectedRun] = useState(''), [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState(''), [error, setError] = useState(''), [selected, setSelected] = useState<number[]>([]);
  const [preview, setPreview] = useState(false), [formError, setFormError] = useState('');
  useEffect(() => {
    const controller = new AbortController(); let loading = false;
    async function load() {
      if (loading || controller.signal.aborted) return;
      loading = true;
      try {
        const history = await api<AgentHistory>(`/api/agent/cases/${value.id}`, { signal: controller.signal });
        const current = selectedRun && selectedRun !== history.latest?.id ? await api<AgentRun>(`/api/agent/runs/${selectedRun}`, { signal: controller.signal }) : history.latest;
        if (!controller.signal.aborted) { setData(history); setRun(current); setError(''); }
      } catch (e) { if (!controller.signal.aborted) setError(message(e)); }
      finally { loading = false; }
    }
    void load(); const timer = window.setInterval(() => { if (!document.hidden) void load(); }, 4000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [value.id, selectedRun, revision]);
  useEffect(() => { setSelected([]); setPreview(false); }, [run?.id]);
  const active = data?.history.find(r => ['queued', 'running'].includes(r.state));
  const imageCount = data?.image_count ?? value.photo_count ?? 0;
  const canAdopt = manageable && !!run && run.current && run.state === 'completed' && !['closed', 'direct_payment', 'noncoverage'].includes(value.workflow_stage);
  const findings = run?.result?.findings || [];
  function evidenceText(text: string) { return run ? run.photos.reduce((value, photo, index) => value.replaceAll(photo.id, `照片 ${index + 1}`), text) : text; }
  const chosen = selected.filter(index => findings[index]?.kind === 'visible_issue' && !run?.adopted.includes(index));
  async function start() {
    setBusy('start'); setError('');
    try {
      const result = await post<AgentRun & { cached?: boolean }>(`/api/agent/cases/${value.id}/review`, { force: !!run });
      setSelectedRun(''); setRun(result); setSelected([]); setRevision(n => n + 1);
      notify(result.cached ? '已载入当前资料的审阅记录' : '案件已进入审阅队列，可在此查看进展');
    } catch (e) { setError(message(e)); } finally { setBusy(''); }
  }
  async function cancel() {
    if (!active) return;
    setBusy('cancel'); setError('');
    try { await post(`/api/agent/runs/${active.id}/cancel`); setRevision(n => n + 1); notify('已停止后续审阅步骤'); }
    catch (e) { setError(message(e)); } finally { setBusy(''); }
  }
  async function adopt() {
    if (!run) return;
    setBusy('adopt'); setFormError('');
    try {
      const result = await post<{ count: number }>(`/api/agent/runs/${run.id}/adopt`, { finding_indexes: chosen });
      setPreview(false); setSelected([]); setRevision(n => n + 1);
      notify(`已将 ${result.count} 项建议加入项目反馈草稿，可预览后发送`);
    } catch (e) { setFormError(message(e)); } finally { setBusy(''); }
  }
  function feedbackText() {
    if (!run) return '';
    return `案件：${run.case_context.code || '编号待完善'}\n位置：${run.case_context.location || '位置待完善'}\n维修事项：${run.case_context.title}\n台账阶段：${run.case_context.stage}\n\n` + chosen.map((i, index) => {
      const f = findings[i];
      return `${index + 1}. ${evidenceText(f.title)}（AI 审阅建议）\n照片：${f.photo_ids.map(id => run.photos.find(p => p.id === id)?.name || '原图').join('、')}\n具体依据：${evidenceText(f.detail)}\n处理要求：${evidenceText(f.action)}`;
    }).join('\n\n');
  }
  return <section className="case-agent" aria-label="案件智能审阅">
    <div className="agent-heading"><div><h2><FileSearch size={20} aria-hidden="true" />案件智能审阅</h2><p>结合维修事项、照片和相关记录，形成可查看依据的审阅建议。</p></div><div className="row wrap">
      {active ? <button disabled={!!busy || !manageable} onClick={() => void cancel()}><Square size={14} aria-hidden="true" />停止审阅</button> : <button className="primary" disabled={!!busy || !manageable || !data?.ready || !imageCount} onClick={() => void start()}><RefreshCw size={16} aria-hidden="true" className={busy === 'start' ? 'spin' : ''} />{busy === 'start' ? '正在排队…' : run ? '重新审阅' : '开始智能审阅'}</button>}
    </div></div>
    <ErrorBox error={error} />
    {data && !data.ready && <p className="agent-notice">请先在“识别设置”配置并启用大模型接口，再审阅案件资料。</p>}
    {data && !imageCount && <p className="agent-notice">本案尚无归档照片。归档到本案后即可开始审阅。</p>}
    {active && <div className="agent-progress" role="status"><LoaderCircle size={18} className="spin" aria-hidden="true" /><div><strong>{STATES[active.state]}{run?.id === active.id && run.steps > 0 ? ` · 已完成 ${run.steps} 轮分析` : ''}</strong><p>{run?.id === active.id && run.trace.length ? run.trace[run.trace.length - 1].outcome : '任务在后台执行，可以离开页面后回来查看。'}</p></div></div>}
    {!!data?.history.length && <div className="agent-history"><label>审阅记录<select aria-label="审阅记录" value={selectedRun} onChange={e => { setSelectedRun(e.target.value); setSelected([]); }}><option value="">最新审阅</option>{data.history.map(r => <option key={r.id} value={r.id}>{dateTime(r.created_at)} · {STATES[r.state]}</option>)}</select></label>{run && <span>{STATES[run.state]} · {dateTime(run.finished_at || run.created_at)}</span>}</div>}
    {!data && !error ? <Loading /> : !run ? <Empty>选择“开始智能审阅”，使用已配置的大模型读取本案图片和关联记录。结果保存为建议，生成后可以选择具体问题形成反馈草稿。</Empty> : <>
      {!run.current && <div className="agent-stale" role="status">案件、照片、关联记录或模型配置已变化。这份记录保留备查，重新审阅后才能生成反馈。</div>}
      <ErrorBox error={run.error || ''} />
      {run.result && <>
        <div className="agent-summary"><h3>审阅意见</h3><p>{evidenceText(run.result.summary)}</p><small>已查看 {run.result.viewed_photo_ids.length}/{run.result.total_images} 张图片 · {run.result.scope}</small></div>
        {!!run.result.limitations.length && <div className="agent-notice"><strong>仍需注意的范围</strong><ul>{run.result.limitations.map((limit, i) => <li key={i}>{limit}</li>)}</ul></div>}
        <div className="agent-findings"><div className="row between wrap"><h3>发现与核对事项 · {findings.length} 项</h3><button disabled={!!busy || !canAdopt || !chosen.length} onClick={() => { setFormError(''); setPreview(true); }}>预览选中建议{chosen.length ? `（${chosen.length}）` : ''}</button></div>
          {!findings.length && <Empty>本次未形成具体问题。请结合已查看范围理解结果，审阅记录不代表验收通过。</Empty>}
          {findings.map((f, index) => <article className="agent-finding" key={index}>
            <header><span className={`agent-kind agent-kind-${f.kind}`}>{KINDS[f.kind]}</span>{f.kind === 'visible_issue' && <label className="agent-choice">{run.adopted.includes(index) ? <><CheckCircle2 size={15} aria-hidden="true" />已加入反馈草稿</> : <><input type="checkbox" aria-label={`选择反馈：${f.title}`} disabled={!canAdopt || !!busy} checked={selected.includes(index)} onChange={e => setSelected(prev => e.target.checked ? [...prev, index] : prev.filter(i => i !== index))} />加入反馈草稿</>}</label>}</header>
            <h4>{evidenceText(f.title)}</h4><p><strong>依据：</strong>{evidenceText(f.detail)}</p><p><strong>{f.kind === 'visible_issue' ? '处理建议：' : '核对建议：'}</strong>{evidenceText(f.action)}</p>
            {!!f.photo_ids.length && <div className="agent-evidence" aria-label="引用照片">{f.photo_ids.map(id => { const photo = run.photos.find(p => p.id === id); return <a key={id} href={`/api/photos/${id}/original`} target="_blank" rel="noreferrer"><img src={`/api/photos/${id}/thumbnail`} alt={`证据照片：${photo?.name || '原图'}`} loading="lazy" /><span>{photo?.name || '查看原图'}<small>{PHOTO_GROUPS[photo?.photo_group || ''] || '用途待核对'}</small></span></a>; })}</div>}
            {!!f.message_ids.length && <details><summary>查看引用的群消息 · {f.message_ids.length} 条</summary>{f.message_ids.map(id => { const m = run.messages.find(v => v.id === id); return <blockquote key={id}><small>{dateTime(m?.sent_at || m?.received_at)} · 来源陈述</small><p>{m?.content || '请在原始消息记录中核对'}</p></blockquote>; })}</details>}
            {f.kind === 'archive_note' && <small className="muted">本机归档情况仅供内部核对，不进入群反馈。</small>}
          </article>)}
        </div>
        {!!run.adopted.length && <div className="agent-notice row between wrap"><span>已选择的建议保存在项目反馈草稿中。</span>{onDraft ? <button onClick={() => onDraft(value.project_id)}>预览项目反馈</button> : <a href="/?view=followup">前往项目跟进预览反馈</a>}</div>}
      </>}
      <details className="agent-trace"><summary>审阅过程与用量 · {run.trace.length} 条记录</summary><ol>{run.trace.map((step, index) => <li key={index}><span>第 {step.step} 轮 · {step.outcome}</span><small>{dateTime(step.created_at)}</small></li>)}</ol><p>模型：{run.model} · 调用 {run.usage.requests || 0} 次 · 输入 {run.usage.prompt_tokens || 0} / 输出 {run.usage.completion_tokens || 0} Token（接口返回值）</p></details>
    </>}
    {preview && run && <Modal title="预览案件反馈草稿" close={() => setPreview(false)} wide footer={<><button onClick={() => setPreview(false)}>返回核对</button><button className="primary" disabled={!!busy || !canAdopt || !chosen.length} onClick={() => void adopt()}>{busy === 'adopt' ? '正在保存…' : '加入项目反馈'}</button></>}><ErrorBox error={formError} /><p>以下 {chosen.length} 项建议将进入本项目反馈草稿，接收人和群消息在项目反馈中预览、发送。</p><pre className="agent-preview">{feedbackText()}</pre></Modal>}
  </section>;
}
