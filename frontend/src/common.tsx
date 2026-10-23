import { useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { X, LoaderCircle, Inbox } from 'lucide-react';
import { api, message } from './api';

export const STAGES: Record<string, string> = { unknown: '待分类', survey: '查勘', work: '施工', acceptance: '验收照片' };
export const ROLES: Record<string, string> = { unknown: '待分类', building_checkin: '楼栋打卡', door_checkin: '入户门打卡', problem: '问题照片', construction: '施工过程', finished: '完工效果', other: '其他资料' };
export const CASE_STATES: Record<string, string> = { pending: '待处理', survey: '查勘中', working: '施工中', completed: '已完工', accepted: '已验收', closed: '已结案' };
export const PHOTO_GROUPS: Record<string, string> = { survey_checkin: '查勘打卡', survey_problem: '查勘问题', work_checkin: '维修打卡', construction: '施工', completion: '完工验收' };
export const WORKFLOW_STAGES: Record<string, string> = { pending_intake: '待维保受理', insurer_intake: '待保司处理', insurer_review: '待保司复核', historical_unverified: '历史待核实', survey: '查勘中', repair: '施工中', acceptance: '待验收', settlement: '待结算', closed: '已结束', direct_payment: '直接赔付', noncoverage: '非维修流程' };
export const STATES: Record<string, string> = { starting: '正在连接', listening: '飞书已连接', reconnecting: '正在重连', pending: '待处理', queued: '排队中', running: '进行中', completed: '已完成', partial: '部分完成', failed: '失败', confirmed: '人工已确认', recognized: '已识别待核对', ready: '已生成', building: '生成中', submitted: '已提交', accepted: '已接收', returned: '退回补件', disabled: '未启用', waiting: '等待连接', error: '异常', saved: '文件已保存', none: '无附件', imported: '已导入', duplicate: '重复保留来源', preview: '待确认', deleted: '已删除', cancelled: '已取消' };
export const SOURCES: Record<string, string> = { feishu: '飞书群', folder: '文件夹', upload: '批量上传', zip: 'ZIP导入', wxauto: '微信群', feed: '采集接口', sample: '样例核对记录' };
export function State({ value }: { value: string }) { return <span className={`status status-${value}`}>{STATES[value] || value}</span>; }
export function dateTime(value: string | null | undefined) { if (!value) return '—'; if (/[Z]|[+]\d{2}:\d{2}$/.test(value)) return new Date(value).toLocaleString('zh-CN', { hour12: false }); return value.replace('T', ' ').replace(/(\d{2}:\d{2}):00$/, '$1'); }
export function bytes(n: number) { return n >= 1024 * 1024 ? `${(n / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`; }
export function place(value: { building?: string | null; unit?: string | null; room?: string | null; location_detail?: string | null }) { return [value.building ? `${value.building}栋` : '', value.unit ? `${value.unit}单元` : '', value.room || '', value.location_detail || ''].filter(Boolean).join(' ') || '位置待补充'; }
export function inputDate(value: string | null | undefined) { return value ? value.replace(' ', 'T').slice(0, 16) : ''; }
export function useData<T>(path: string | null, revision: number | string = 0) {
  const [data, setData] = useState<T | null>(null), [loading, setLoading] = useState(true), [error, setError] = useState('');
  useEffect(() => { if (!path) { setLoading(false); return; } const abort = new AbortController(); setLoading(true); setError(''); api<T>(path, { signal: abort.signal }).then(value => { setData(value); setLoading(false); }).catch(e => { if (!abort.signal.aborted) { setError(message(e)); setLoading(false); } }); return () => abort.abort(); }, [path, revision]);
  return { data, loading, error };
}
export function useDebounce<T>(value: T, delay = 250) { const [current, setCurrent] = useState(value); useEffect(() => { const timer = window.setTimeout(() => setCurrent(value), delay); return () => window.clearTimeout(timer); }, [value, delay]); return current; }
export function Loading() { return <div className="empty-state"><LoaderCircle className="spin" size={22} /><span>正在读取资料…</span></div>; }
export function Empty({ children, action }: { children: ReactNode; action?: ReactNode }) { return <div className="empty-state"><Inbox size={30} /><p>{children}</p>{action}</div>; }
export function ErrorBox({ error }: { error: string }) { return error ? <div className="error-box" role="alert">{error}</div> : null; }
export function Modal({ title, children, footer, close, wide = false }: { title: string; children: ReactNode; footer?: ReactNode; close: () => void; wide?: boolean }) {
  const ref = useRef<HTMLDialogElement>(null), titleId = useId();
  useEffect(() => { const dialog = ref.current; if (dialog && !dialog.open) dialog.showModal(); return () => { if (dialog?.open) dialog.close(); }; }, []);
  return <dialog ref={ref} className={`modal ${wide ? 'modal-wide' : ''}`} aria-labelledby={titleId} onCancel={e => { e.preventDefault(); e.stopPropagation(); close(); }} onClick={e => { if (e.target === e.currentTarget) { const rect = e.currentTarget.getBoundingClientRect(); if (e.clientX < rect.left || e.clientX > rect.right || e.clientY < rect.top || e.clientY > rect.bottom) close(); } }}><header className="modal-head"><h2 id={titleId}>{title}</h2><button type="button" className="icon-button" aria-label="关闭" onClick={close}><X size={20} /></button></header><div className="modal-body">{children}</div>{footer && <footer className="modal-foot">{footer}</footer>}</dialog>;
}
export function SelectOptions({ values, empty }: { values: Record<string, string>; empty?: string }) { return <>{empty !== undefined && <option value="">{empty}</option>}{Object.entries(values).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</>; }
