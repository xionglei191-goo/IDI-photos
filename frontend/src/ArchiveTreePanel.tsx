import { useEffect, useState } from 'react';
import { FolderOpen, RefreshCw } from 'lucide-react';
import { message, post } from './api';
import { dateTime, ErrorBox, useData } from './common';
import type { Notify } from './types';

interface TreeStatus {
  root: string;
  automatic: boolean;
  state: string;
  pending: boolean;
  last_synced_at: string | null;
  file_count: number;
  error_count: number;
  errors: { path: string; error: string }[];
}

const STATES: Record<string, string> = { pending: '等待同步', syncing: '正在同步', ready: '已同步', partial: '部分文件待处理', failed: '同步失败' };

export default function ArchiveTreePanel({ notify, refresh }: { notify: Notify; refresh: () => void }) {
  const [tick, setTick] = useState(0), [busy, setBusy] = useState(false);
  const { data, error } = useData<TreeStatus>('/api/archive-tree', tick);
  useEffect(() => { const timer = window.setInterval(() => setTick(n => n + 1), 2000); return () => window.clearInterval(timer); }, []);
  async function sync() {
    setBusy(true);
    try { await post('/api/archive-tree/sync'); refresh(); setTick(n => n + 1); notify('目录同步与校验已排队，可在任务记录查看结果'); }
    catch (e) { notify(message(e), true); }
    finally { setBusy(false); }
  }
  async function open() {
    try { await post('/api/archive-tree/open'); }
    catch (e) { notify(message(e), true); }
  }
  return <div className="settings-form">
    <section className="detail-section">
      <h3>项目资料目录</h3>
      <p className="muted space-top">按项目、案件和阶段保存独立原图副本，可直接用文件夹查看、复制和交接。</p>
      <ErrorBox error={error} />
      <dl className="system-info">
        <dt>保存位置</dt><dd className="break-all" data-testid="archive-tree-root">{data?.root || '—'}</dd>
        <dt>同步状态</dt><dd data-testid="archive-tree-state">{data ? STATES[data.state] || data.state : '读取中…'}{data && !data.automatic ? '（自动同步未启动）' : ''}</dd>
        <dt>已整理文件</dt><dd>{data?.file_count ?? '—'} 项（照片、报送包及回执）</dd>
        <dt>最近完成</dt><dd>{dateTime(data?.last_synced_at)}</dd>
      </dl>
      <div className="row"><button className="primary" onClick={() => void open()}><FolderOpen size={15} />打开资料目录</button><button disabled={busy || data?.state === 'syncing'} onClick={() => void sync()}><RefreshCw size={15} />重新同步目录</button></div>
    </section>
    <section className="detail-section">
      <h3>自动整理规则</h3>
      <p className="space-top">项目 → 案件位置与内容 → 查勘 / 施工 / 完工 / 待分类。未确定案件的文件进入“待归档”，报送包和回执按版本放入“报送资料”。完工文件夹不代表案件已经验收。</p>
      <p className="space-top">导入、识别归属、案件改名或重新分类后自动同步。名称末尾的短标识用于区分同名案件和照片。</p>
      <p className="muted space-top">在系统里修改分类即可更新目录。目录内手动新增的文件不会自动入库；手动修改的副本会保留并提示核对。需要保存到档案和备份的资料，请通过“导入资料”加入系统。</p>
    </section>
    {!!data?.error_count && <section className="detail-section"><h3>需要处理的文件（{data.error_count} 项）</h3><p className="muted space-top">先将需要保留的本地文件另存，再移走冲突文件并重新同步。此处最多显示 100 项。</p><div className="table-wrap space-top"><table><thead><tr><th>位置</th><th>说明</th></tr></thead><tbody>{data.errors.map((item, index) => <tr key={index}><td className="break-all">{item.path || data.root}</td><td>{item.error}</td></tr>)}</tbody></table></div></section>}
  </div>;
}
