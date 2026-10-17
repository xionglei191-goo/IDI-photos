import { useEffect, useRef, useState } from 'react';
import { Clock3, LoaderCircle, RefreshCw } from 'lucide-react';
import { message, put } from './api';
import type { Job } from './types';
import type { SyncStatus } from './workflowTypes';

export default function SyncControls({ value, fullSync, sync, refresh, notify, mode = 'manual' }: {
  value?: SyncStatus; fullSync?: Job; sync: () => Promise<void>; refresh: () => void;
  notify: (text: string, error?: boolean) => void;
  mode?: 'manual' | 'policy';
}) {
  const [schedule, setSchedule] = useState(value?.schedule);
  const [saving, setSaving] = useState(false), [starting, setStarting] = useState(false);
  const toggling = useRef(false), dispatching = useRef(false);
  useEffect(() => { if (!toggling.current) setSchedule(value?.schedule); }, [value]);
  const time = schedule ? `${String(schedule.hour).padStart(2, '0')}:${String(schedule.minute).padStart(2, '0')}` : '';
  const autoTitle = !schedule ? '正在读取自动同步设置' : `${schedule.enabled ? `每天 ${time}（北京时间）自动同步；点击暂停` : '自动同步已暂停；点击开启'}${!schedule.connected ? '。官网尚未连接，连接后执行' : ''}`;

  async function toggle() {
    if (!schedule || toggling.current) return;
    toggling.current = true; setSaving(true);
    try {
      const result = await put<SyncStatus>('/api/workflow/sync/policy', { enabled: !schedule.enabled });
      setSchedule(result.schedule);
      notify(result.schedule?.enabled ? `已开启自动同步，每天 ${time}（北京时间）执行${result.schedule.connected ? '' : '；当前等待官网连接'}` : '已暂停自动同步，正在执行的批次会完成');
    } catch (e) { notify(message(e), true); }
    finally { toggling.current = false; setSaving(false); refresh(); }
  }

  async function manual() {
    if (dispatching.current || fullSync) return;
    dispatching.current = true; setStarting(true);
    try { await sync(); }
    catch (e) { notify(message(e), true); }
    finally { dispatching.current = false; setStarting(false); }
  }

  return <div className="sync-actions" role="group" aria-label="官网同步">
    {mode === 'policy' && <button className="auto-sync-button" disabled={!schedule || saving} aria-pressed={schedule?.enabled ?? false} aria-busy={saving} title={autoTitle} onClick={() => void toggle()}>
      {saving ? <LoaderCircle size={16} className="spin" aria-hidden="true" /> : <Clock3 size={16} aria-hidden="true" />}
      自动同步 <small>{saving ? '保存中' : !schedule ? '读取中' : schedule.enabled ? '已开启' : '已暂停'}</small>
    </button>}
    {mode === 'manual' && <button className="primary" disabled={starting || !!fullSync} aria-busy={starting || !!fullSync} title="立即完整同步官网台账；自动同步进行中时会排队执行" onClick={() => void manual()}>
      <RefreshCw size={16} className={starting || fullSync ? 'spin' : ''} aria-hidden="true" />
      {starting ? '正在启动' : fullSync ? fullSync.state === 'queued' ? '等待同步' : '同步中' : '手动同步'}
    </button>}
  </div>;
}
