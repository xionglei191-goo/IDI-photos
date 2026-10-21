import { useEffect, useState } from 'react';
import { RefreshCw, Trash2 } from 'lucide-react';
import { api, message, post } from './api';
import { ErrorBox, Modal } from './common';
import AttachmentGallery, { type Attachment } from './AttachmentGallery';
import type { AttachmentRemoval, PlatformStage } from './platformTypes';
import type { Notify } from './types';

export interface RemovalTarget {
  caseId: string; stage: PlatformStage; field: string; label: string; item: Attachment; revision: number;
}

export function RemovalAlerts({ items, open }: { items?: AttachmentRemoval[]; open: (record: AttachmentRemoval) => void }) {
  return <>{items?.map(record => <div className="notice attachment-removal-alert" key={record.id} role="status">
    <div><strong>{record.label} · 照片撤回待核实</strong><p>{record.error || '已发起撤回，等待官网回读。此环节的自动上传和提交已暂停。'}</p></div>
    <button type="button" onClick={() => open(record)}><RefreshCw size={15} aria-hidden="true" />核对撤回结果</button>
  </div>)}</>;
}

export default function RemoveAttachmentDialog({ target, initial, close, changed, done, notify }: {
  target: RemovalTarget; initial?: AttachmentRemoval; close: () => void; changed: () => void; done: () => void; notify: Notify;
}) {
  const [record, setRecord] = useState<AttachmentRemoval | null>(initial || null);
  const [busy, setBusy] = useState(!initial && target.item.official), [error, setError] = useState('');
  const [acknowledged, setAcknowledged] = useState(false);
  const path = `/api/platform/cases/${encodeURIComponent(target.caseId)}/stages/${target.stage}/files/${encodeURIComponent(target.field)}`;
  const pending = !!record && ['removing', 'saved', 'uncertain'].includes(record.state);
  useEffect(() => {
    if (initial || !target.item.official) return;
    let alive = true;
    post<AttachmentRemoval>(`${path}/official/${encodeURIComponent(target.item.fileId)}/removal`)
      .then(value => { if (alive) setRecord(value); })
      .catch(e => { if (alive) setError(message(e)); })
      .finally(() => { if (alive) setBusy(false); });
    return () => { alive = false; };
  }, [initial, path, target.item.fileId, target.item.official]);
  async function prepareAgain() {
    setBusy(true); setError(''); setAcknowledged(false);
    try { setRecord(await post<AttachmentRemoval>(`${path}/official/${encodeURIComponent(target.item.fileId)}/removal`)); }
    catch (e) { setError(message(e)); }
    finally { setBusy(false); }
  }
  async function act(readback = false) {
    if (busy) return;
    setBusy(true); setError('');
    try {
      if (target.item.official && record) {
        const next = await post<AttachmentRemoval>(`/api/platform/attachment-removals/${record.id}/${readback ? 'readback' : 'confirm'}`,
          readback ? {} : { fingerprint: record.fingerprint, acknowledged });
        setRecord(next); changed();
        if (next.state === 'verified') {
          notify('已从官网撤回照片，回读核对一致；本地原件保留。请重新核对本环节资料。');
          done();
        }
      } else if (!target.item.official) {
        await post(`${path}/local/${encodeURIComponent(target.item.fileId)}/remove`, { revision: target.revision });
        changed(); notify('已移除本环节选材，本地原件保留；不会自动重新加入。'); done();
      }
    } catch (e) {
      setError(message(e));
      if (record) {
        try { setRecord(await api<AttachmentRemoval>(`/api/platform/attachment-removals/${record.id}`)); }
        catch { /* Keep the original failure visible. */ }
      }
      changed();
    } finally { setBusy(false); }
  }
  const preview = record ? { ...target.item, name: record.file.oldFileName || record.file.fileName || target.item.name,
    thumbnail: target.item.image ? `/api/platform/attachment-removals/${record.id}/thumbnail` : null,
    original: `/api/platform/attachment-removals/${record.id}/original` } : target.item;
  return <Modal title={target.item.official ? '删除并从官网撤回' : '移除本环节选材'} close={() => { if (!busy) close(); }} footer={<>
    <span className="grow muted" role="status">{busy ? '正在核对官网与照片，请稍候…' : pending ? '撤回结果待核实，不会重复发送' : ''}</span>
    <button type="button" disabled={busy} onClick={close}>{pending ? '稍后核对' : '取消'}</button>
    {pending ? <button type="button" disabled={busy} onClick={() => void act(true)}><RefreshCw size={15} aria-hidden="true" />回读撤回结果</button>
      : target.item.official && (!record || ['failed', 'stale'].includes(record.state)) ? <button type="button" disabled={busy} onClick={() => void prepareAgain()}><RefreshCw size={15} aria-hidden="true" />重新核对照片</button>
      : <button type="button" className="danger" disabled={busy || !acknowledged || (target.item.official && record?.state !== 'prepared')} onClick={() => void act()}>
        <Trash2 size={15} aria-hidden="true" />{target.item.official ? '确认删除并撤回' : '确认移除选材'}</button>}
  </>}>
    <p><strong>{record?.short_code}</strong>{record ? ` · ${record.stage_label}` : ''} · {target.label}</p>
    <p>{target.item.official ? '将从官网当前环节的附件清单删除这张照片。其他照片和已保存字段保持不变，本地原件保留。' : '将这张照片从当前环节的待上传选材中移除，本地原件保留。'}</p>
    <p className="muted">同一照片不会自动重新加入。需要恢复时，可重新选择或导入原件。</p>
    <ErrorBox error={error || record?.error || ''} />
    <AttachmentGallery label={target.label} items={[preview]} />
    {!pending && <label className="check"><input type="checkbox" checked={acknowledged} disabled={busy || (target.item.official && record?.state !== 'prepared')} onChange={event => setAcknowledged(event.target.checked)} />我已核对照片及所属栏位，确认{target.item.official ? '删除并从官网撤回' : '移除选材'}</label>}
  </Modal>;
}
