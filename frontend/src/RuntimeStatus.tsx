import { useEffect, useState } from 'react';
import { Activity, RefreshCw } from 'lucide-react';
import { api, message, post, put } from './api';
import { dateTime, ErrorBox, PHOTO_GROUPS, ROLES, STAGES } from './common';
import type { Photo } from './types';
import './runtime-status.css';

export const AI_STATES: Record<string, string> = { queued: 'AI 已排队', waiting: 'AI 暂缓', running: 'AI 分析中', completed: 'AI 分析完成', failed: 'AI 待人工处理', stale: '资料已变更', cancelled: 'AI 已停止' };
const MODEL: Record<string, string> = { unconfigured: '未配置', disabled: '已停用', unverified: '待验证', healthy: '响应有效', degraded: '部分请求失败', paused: '暂缓', recovering: '等待试运行', probing: '恢复试运行', configuration_error: '需修正配置' };
const PIPELINE: Record<string, string> = { historical: '历史资料', queued: '等待规则检查', needs_check: '待核对归属或用途', local_draft: '本地草稿已准备', associated: '已按规则归案', storage_error: '原件需核验', stage_mismatch: '用途与环节不匹配', excluded: '已保留人工排除', already_saved: '已核对官网回执', unsupported_stage: '当前环节人工办理', not_applicable: '附件已归档' };
interface Runtime {
  at: string; worker_enabled: boolean; storage: { state: string; detail: string; free_bytes?: number };
  model: { state: string; error: string; retry_at: string | null; calls: number; active: number; last_transport_ok: string | null; last_response_ok: string | null };
  policy: { rules_enabled: boolean; auto_drafts: boolean };
  ocr: { enabled: boolean; installed: boolean; latest: { state: string; error: string | null; created_at: string } | null; pending: number };
  platform: { connected: boolean }; sync: { schedule: { last_success: { completed_at?: string } | null } };
  channels: { id: string; name: string; enabled: boolean; state: string; error: string | null; last_saved: string | null; counts: { pending: number; failed: number } }[];
  attachments_pending: number; ai_queue: Record<string, number>;
  ai_tasks: { id: string; photo_id: string; kind: string; state: string; attempts: number; error: string | null; short_code: string | null }[];
}

interface UploadStatus { policy: { enabled: boolean } }

function AutomaticSaveStatus({ openDetails }: { openDetails?: () => void }) {
  const [data, setData] = useState<UploadStatus | null>(null), [error, setError] = useState('');
  useEffect(() => {
    const abort = new AbortController(); let pending = false;
    const refresh = async () => {
      if (pending || document.hidden) return;
      pending = true;
      try { const next = await api<UploadStatus>('/api/workflow/uploads', { signal: abort.signal }); if (!abort.signal.aborted) { setData(next); setError(''); } }
      catch (e) { if (!abort.signal.aborted) setError(message(e)); }
      finally { pending = false; }
    };
    void refresh(); const timer = window.setInterval(() => void refresh(), 10000);
    return () => { abort.abort(); window.clearInterval(timer); };
  }, []);
  const label = error ? '状态读取失败' : !data ? '正在读取…' : data.policy.enabled ? '已开启' : '已暂停';
  const detail = error || '点击查看保存进度';
  return <button className={`runtime-save-state${error ? ' needs-attention' : ''}`} aria-label="查看自动保存状态" title={detail} onClick={openDetails}><span>自动保存：{label}</span></button>;
}

export default function RuntimeStatus({ login, settings, channels, mode = 'details', openDetails, openUploads }: { login: () => void; settings: () => void; channels: () => void; mode?: 'summary' | 'details' | 'policy'; openDetails?: () => void; openUploads?: () => void }) {
  const [data, setData] = useState<Runtime | null>(null), [error, setError] = useState(''), [busy, setBusy] = useState(false), [notice, setNotice] = useState('');
  async function load() { const next = await api<Runtime>('/api/operations'); setData(next); setError(''); }
  useEffect(() => {
    const abort = new AbortController(); let pending = false;
    const refresh = async () => { if (pending || document.hidden) return; pending = true; try { const next = await api<Runtime>('/api/operations', { signal: abort.signal }); if (!abort.signal.aborted) { setData(next); setError(''); } } catch (e) { if (!abort.signal.aborted) setError(message(e)); } finally { pending = false; } };
    void refresh(); const timer = window.setInterval(() => void refresh(), 10000);
    return () => { abort.abort(); window.clearInterval(timer); };
  }, []);
  async function act(path: string, text: string, payload?: Record<string, boolean>) {
    setBusy(true); setNotice(''); setError('');
    try { if (payload) await put(path, payload); else await post(path); await load(); setNotice(text); }
    catch (e) { setError(message(e)); } finally { setBusy(false); }
  }
  const queue = data?.ai_queue || {}, waiting = (queue.queued || 0) + (queue.waiting || 0) + (queue.running || 0);
  const capture = data?.channels.filter(c => c.enabled) || [], failed = capture.filter(c => c.error).length;
  const needsAttention = !!error || !!data && (data.storage.state !== 'available' || !data.worker_enabled || ['degraded', 'paused', 'configuration_error'].includes(data.model.state) || data.ocr.latest?.state === 'failed' || failed > 0 || data.attachments_pending > 0 || (queue.failed || 0) > 0);
  const overview = data ? <>
    <span>资料存储：{data.storage.state === 'available' ? '可用' : '需处理'}</span>
    <span>本地 OCR：{!data.ocr.enabled ? '已关闭' : !data.ocr.installed ? '未安装' : data.ocr.latest?.state === 'failed' ? '最近识别失败' : data.ocr.latest ? '最近识别成功' : '待首次识别'}</span>
    <span>AI：{MODEL[data.model.state] || '待核对'}{waiting > 0 && ` · ${waiting} 项待处理`}</span>
    <span>公司官网：{data.platform.connected ? '已连接' : '待登录'}</span>
    <span>收图入口：{failed ? `${failed} 项异常` : capture.length ? `${capture.length} 项启用` : '未启用'}</span>
  </> : <span>{error ? '运行状态暂时无法读取' : '正在读取运行状态…'}</span>;
  if (mode === 'summary') return <section className="runtime-status runtime-status-strip" aria-label="运行状态"><div className="runtime-summary">
    <button className={`runtime-heading${needsAttention ? ' needs-attention' : ''}`} aria-label={needsAttention ? '查看运行状态，有待处理事项' : '查看运行状态'} title="查看运行详情" onClick={openDetails}><Activity size={16} aria-hidden="true" /><strong>运行状态</strong></button>
    <div className="runtime-overview">{overview}<AutomaticSaveStatus openDetails={openUploads || openDetails} /></div>
  </div></section>;
  if (mode === 'policy') return <section className="settings-card" aria-label="资料整理设置"><h3>照片归档后自动整理</h3><p>按已核实的编号、归属和照片用途处理，保留人工修改和排除记录。</p>{data ? <div className="runtime-switches"><label><input type="checkbox" checked={data.policy.rules_enabled} disabled={busy} onChange={e => void act('/api/operations/policy', '规则整理开关已保存。', { rules_enabled: e.target.checked })} />自动规则整理</label><label><input type="checkbox" checked={data.policy.auto_drafts} disabled={busy} onChange={e => void act('/api/operations/policy', '自动准备草稿开关已保存。', { auto_drafts: e.target.checked })} />明确资料自动准备草稿</label></div> : <p>正在读取设置…</p>}{notice && <p role="status">{notice}</p>}<ErrorBox error={error} /></section>;
  return <section className="runtime-status" aria-label="运行详情">
    <div className="runtime-summary"><Activity size={16} aria-hidden="true" /><strong>运行概况</strong><span className="runtime-overview">{overview}</span></div>
    <div className="runtime-content">
      {!data?.worker_enabled && data && <p className="notice">后台处理当前未启用，队列不会自动执行。</p>}
      {data && <><p>资料接收、规则整理与 AI 分析分别运行。附件待下载或失败 {data.attachments_pending} 项；分析记录已完成 {queue.completed || 0} 项、待人工处理 {queue.failed || 0} 项。</p>
        <div className="runtime-grid"><section><h3>模型与恢复</h3><p>{data.model.error || '未发出额外健康探测。有效响应表示结构检查通过，识别内容仍需核对。'}</p>
          {data.model.retry_at && <p>最早恢复时间：{dateTime(data.model.retry_at)}</p>}
          <p>最近接口响应：{data.model.last_transport_ok ? dateTime(data.model.last_transport_ok) : '尚无记录'}<br />最近有效结果：{data.model.last_response_ok ? dateTime(data.model.last_response_ok) : '尚无记录'}</p>
          <div className="runtime-actions"><button disabled={busy || ['disabled', 'unconfigured'].includes(data.model.state)} onClick={() => void act('/api/operations/model/retry', '已允许下一次模型调用；未重跑旧对话或发送消息。')}>允许重新尝试</button><button onClick={settings}>模型设置</button></div>
        </section><section><h3>资料与外部入口</h3><p>{data.storage.detail}{data.storage.free_bytes != null && `；剩余 ${(data.storage.free_bytes / 1024 ** 3).toFixed(1)} GB`}</p>
          <p>{data.ocr.latest?.error || '本地 OCR 与外部 AI 独立。'}未有成功 OCR 记录 {data.ocr.pending} 项。</p>
          <p>最近增量同步：{data.sync.schedule.last_success?.completed_at ? dateTime(data.sync.schedule.last_success.completed_at) : '尚无成功记录'}</p>
          <div className="runtime-actions"><button disabled={busy || !data.ocr.enabled} onClick={() => void act('/api/operations/ocr/retry', '有效且未完成的识别已排队，每批最多 100 项。')}>重试未完成识别</button><button onClick={login}>连接公司官网</button><button onClick={channels}>收图入口与回执</button></div>
        </section></div>
        {!!data.channels.length && <ul className="runtime-task-list">{data.channels.map(channel => <li key={channel.id}><div><strong>{channel.name} · {channel.enabled ? '已启用' : '已暂停'}</strong><p>{channel.error || '当前未记录接收异常'} · 附件待处理 {(channel.counts.pending || 0) + (channel.counts.failed || 0)} 项 · 最近归档 {channel.last_saved ? dateTime(channel.last_saved) : '尚无记录'}</p></div></li>)}</ul>}
        <p className="muted">官网保存仍使用原群的授权；正式提交由人工确认。模型恢复只补有效照片分析，不补发旧对话或日报。</p>
        {!!data.ai_tasks.length && <details><summary>最近分析记录与重试</summary><ul className="runtime-task-list">{data.ai_tasks.map(task => <li key={task.id}><div><strong>{task.short_code || '待归案资料'} · {task.kind === 'quality' ? '内容检查' : '分类建议'}</strong><p>{AI_STATES[task.state]} · 已尝试 {task.attempts} / 3 次{task.error && ` · ${task.error}`}</p></div>{['failed', 'waiting'].includes(task.state) && <button disabled={busy} onClick={() => void act(`/api/operations/ai/${task.id}/retry`, '已发起新的只读分析，原记录继续保留。')}>重新分析</button>}</li>)}</ul></details>}
      </>}
      <div className="runtime-actions"><button disabled={busy} onClick={() => { setBusy(true); void load().catch(e => setError(message(e))).finally(() => setBusy(false)); }}><RefreshCw size={14} aria-hidden="true" />刷新状态</button>{data && <small>读取于 {dateTime(data.at)}</small>}</div>
      {notice && <p role="status">{notice}</p>}<ErrorBox error={error} />
    </div>
  </section>;
}

export function PhotoProcessing({ photo, compact = false }: { photo: Photo; compact?: boolean }) {
  const value = photo.processing;
  if (!value) return null;
  const ai = value.ai;
  const fields = ai?.result?.fields, findings = ai?.result?.findings;
  const labels: Record<string, string> = { raw_text: '图片文字', summary: '内容说明', photo_group: '用途建议', role: '角色建议', stage: '阶段建议', platform_report_code: '案件编号', short_code: '短编号', part: '部位', process: '施工内容', watermark_time: '水印时间' };
  if (compact) return <small className="photo-processing-line">{PIPELINE[value.pipeline.state || ''] || '已归档'}{ai && ` · ${AI_STATES[ai.state]}`}</small>;
  return <section className="photo-processing" aria-label="照片处理进度"><strong>原件已归档 · {photo.case_id ? '已归案' : '待归案'}</strong><p>{PIPELINE[value.pipeline.state || ''] || '规则状态待更新'}{value.pipeline.reason && `：${value.pipeline.reason}`}</p>
    <p>本地检查：{value.rule ? value.rule.error || (value.rule.state === 'needs_review' ? '有待核对事项' : '已完成规则检查') : '尚无记录'}<br />{ai ? `${AI_STATES[ai.state]} · ${ai.attempts} 次尝试${ai.error ? ` · ${ai.error}` : ''}` : 'AI：尚无后台分析记录'}</p>
    {ai?.state === 'completed' && ai.result && <details><summary>查看分析建议与范围</summary><p>以下建议基于本张原图，未自动覆盖人工字段。</p>
      {Array.isArray(findings) && (findings.length ? <ul>{findings.map((finding, index) => <li key={index}><strong>{String(finding.title || '待核对事项')}</strong><p>{String(finding.detail || '')}</p><p>{String(finding.action || '')}</p></li>)}</ul> : <p>本次未报告可见问题，仍需按业务要求核对。</p>)}
      {!!fields && typeof fields === 'object' && <dl>{Object.entries(fields).filter(([key]) => labels[key]).map(([key, item]) => { const text = String(item || '待核对'); return <div key={key}><dt>{labels[key]}</dt><dd>{key === 'photo_group' ? PHOTO_GROUPS[text] || text : key === 'role' ? ROLES[text] || text : key === 'stage' ? STAGES[text] || text : text}</dd></div>; })}</dl>}
    </details>}
  </section>;
}
