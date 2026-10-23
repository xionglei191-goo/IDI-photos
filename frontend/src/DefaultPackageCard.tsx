import { useEffect, useRef, useState } from 'react';
import { Download, LoaderCircle, RefreshCw } from 'lucide-react';
import { api, message, post } from './api';
import { ErrorBox } from './common';
import type { DefaultPackage, Notify } from './types';

const LABELS: Record<string, string> = { waiting: '修复成果提交并经官网确认后，自动准备默认资料。', pending: '正在等待自动准备…', preparing: '默认资料正在后台生成，完成后可直接下载。', blocked: '默认资料暂未生成，请处理下方提示。', failed: '默认资料生成未完成，可重新准备。', deleted: '默认资料已移除，可按需重新准备。' };

export default function DefaultPackageCard({ caseId, revision, refresh, notify }: { caseId: string; revision: string; refresh: () => void; notify: Notify }) {
  const [value, setValue] = useState<DefaultPackage | null>(null), [error, setError] = useState(''), [busy, setBusy] = useState(false);
  const refreshRef = useRef(refresh), packageRef = useRef('');
  refreshRef.current = refresh;
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    const abort = new AbortController();
    async function read() {
      try {
        const incoming = await api<DefaultPackage>(`/api/cases/${caseId}/default-package`, { signal: abort.signal });
        if (active) {
          setValue(incoming); setError('');
          if (incoming.package && packageRef.current !== incoming.package.id) {
            packageRef.current = incoming.package.id; refreshRef.current();
          }
        }
      } catch (e) { if (active) setError(message(e)); }
      if (active) timer = setTimeout(() => void read(), 4000);
    }
    void read();
    return () => { active = false; abort.abort(); clearTimeout(timer); };
  }, [caseId, revision]);
  async function retry() {
    setBusy(true); setError('');
    try { setValue(await post<DefaultPackage>(`/api/cases/${caseId}/default-package`)); refresh(); notify('已重新检查默认资料，准备完成后可直接下载'); }
    catch (e) { setError(message(e)); } finally { setBusy(false); }
  }
  return <section className="default-package-card" aria-label="默认资料">
    <div className="row between wrap"><div><strong>默认资料{value ? ` · ${value.template_name}` : ''}</strong><p className="muted">{value?.state === 'ready' ? '已提前准备，可直接下载。' : LABELS[value?.state || 'pending']}</p></div>
      {value?.state === 'ready' && value.package && <a className="button primary" href={value.package.download_url}><Download size={15} />下载默认{value.template_id === 'picc-record-v1' ? '记录表' : '资料'} v{value.package.version}</a>}
      {value && ['pending', 'preparing'].includes(value.state) && <LoaderCircle className="spin" size={18} aria-label="准备中" />}
      {value?.eligible && ['failed', 'blocked', 'deleted'].includes(value.state) && <button disabled={busy} onClick={() => void retry()}><RefreshCw size={15} />重新准备默认资料</button>}
    </div><ErrorBox error={error || value?.error || ''} />
  </section>;
}
