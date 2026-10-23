import { useRef, useState } from 'react';
import { Trash2 } from 'lucide-react';
import { api, message } from './api';
import { dateTime, ErrorBox, Loading, Modal, place, State, useData } from './common';
import type { Notify, Package, PackageDetail } from './types';
import './photo-deletion.css';

export default function DeletePackageDialog({ value, close, deleted, notify }: {
  value: Package; close: () => void; deleted: () => void; notify: Notify;
}) {
  const [revision, setRevision] = useState(0), [busy, setBusy] = useState(false), [error, setError] = useState('');
  const { data, loading, error: loadError } = useData<PackageDetail>(`/api/packages/${value.id}`, revision);
  const submitting = useRef(false);
  async function remove() {
    if (!data || submitting.current) return;
    submitting.current = true; setBusy(true); setError('');
    try {
      await api(`/api/packages/${value.id}`, { method: 'DELETE', body: JSON.stringify({ expected_token: data.deletion_token }) });
      deleted(); notify(`资料包 v${value.version} 已删除，原照片保留`); close();
    } catch (e) { setError(message(e)); }
    finally { submitting.current = false; setBusy(false); }
  }
  return <Modal title={`删除资料包 · v${value.version}`} close={() => { if (!submitting.current) close(); }} footer={<>
    <button disabled={busy} onClick={close}>取消</button>
    <button className="photo-delete-confirm" disabled={busy || loading || !data || !!error || !!loadError} onClick={() => void remove()}>
      <Trash2 size={16} aria-hidden="true" />{busy ? '删除中…' : '确认删除资料包'}
    </button>
  </>}>
    <p>删除后，此版本会从本案资料包和报送记录中移除，原下载链接停止使用。</p>
    <p className="muted photo-delete-note">本案原照片和其他版本不受影响。该包及已有回执留存备查，已发送的文件不会撤回；删除后无法在此界面恢复。</p>
    <ErrorBox error={error || loadError} />
    {(error || loadError) && <button disabled={busy || loading} onClick={() => { setError(''); setRevision(n => n + 1); }}>重新核对资料包</button>}
    {loading ? <Loading /> : data && <section className="detail-section" aria-label="即将删除的资料包">
      <h3>{data.manifest.case.title}</h3>
      <p className="muted">{data.manifest.case.project_name} · {place(data.manifest.case)}</p>
      <p className="space-top">v{data.version} · {data.manifest.photos.length} 项资料 · {dateTime(data.created_at)}</p>
      <div className="row space-top"><State value={data.state} /><span>{data.events.length ? `已有 ${data.events.length} 条提交或回执记录，删除后保留备查。` : '尚无提交或回执记录。'}</span></div>
    </section>}
  </Modal>;
}
