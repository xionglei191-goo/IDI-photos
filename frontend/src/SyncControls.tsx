import { useEffect, useRef, useState } from 'react';
import { Clock3, LoaderCircle, RefreshCw } from 'lucide-react';
import { message, put } from './api';
import type { Job } from './types';
import type { SyncStatus } from './workflowTypes';
import usePermissions from './usePermissions';

export default function SyncControls({ value, activeSync, sync, refresh, notify, mode = 'manual' }: {
  value?: SyncStatus; activeSync?: Job; sync: () => Promise<void>; refresh: () => void;
  notify: (text: string, error?: boolean) => void;
  mode?: 'manual' | 'full' | 'policy';
}) {
  const { isAdmin } = usePermissions();
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
    if (dispatching.current || activeSync) return;
    dispatching.current = true; setStarting(true);
    try { await sync(); }
    catch (e) { notify(message(e), true); }
    finally { dispatching.current = false; setStarting(false); }
  }

  return <div className="sync-actions" role="group" aria-label="官网同步">
    {mode === 'policy' && <button className="auto-sync-button" disabled={!isAdmin || !schedule || saving} aria-pressed={schedule?.enabled ?? false} aria-busy={saving} title={isAdmin ? autoTitle : '同步策略由系统管理员维护'} onClick={() => void toggle()}>
      {saving ? <LoaderCircle size={16} className="spin" aria-hidden="true" /> : <Clock3 size={16} aria-hidden="true" />}
      自动同步 <small>{saving ? '保存中' : !schedule ? '读取中' : schedule.enabled ? '已开启' : '已暂停'}</small>
    </button>}
    {mode !== 'policy' && <button className="primary" disabled={starting || !!activeSync} aria-busy={starting || !!activeSync} title={mode === 'full' ? '完整核对理赔受理、查勘维修两张清单和全部案件详情' : '增量同步昨天、今天的受理记录及全部在办维修案件；漏跑日期自动补查'} onClick={() => void manual()}>
      <RefreshCw size={16} className={starting || activeSync ? 'spin' : ''} aria-hidden="true" />
      {starting ? '正在启动' : activeSync ? activeSync.state === 'queued' ? '等待同步' : '同步中' : !isAdmin ? '刷新清单' : mode === 'full' ? '手动完整同步' : '手动同步'}
    </button>}
  </div>;
}
