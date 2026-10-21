import { useEffect, useRef, useState } from 'react';
import { FileImage, Trash2 } from 'lucide-react';
import { message, post } from './api';
import { ErrorBox, Loading, Modal } from './common';
import type { Notify } from './types';
import './photo-deletion.css';

type DeletionPhoto = { id: string; revision: number; original_name: string; thumbnail_url: string | null; case_title: string | null; project_name: string | null; short_code: string | null };
type Preview = { items: DeletionPhoto[]; total: number };

export default function DeletePhotosDialog({ ids, close, deleted, notify }: {
  ids: string[]; close: () => void; deleted: (ids: string[]) => void; notify: Notify;
}) {
  const [preview, setPreview] = useState<Preview | null>(null), [error, setError] = useState('');
  const [busy, setBusy] = useState(false), [reload, setReload] = useState(0);
  const submitting = useRef(false);
  useEffect(() => {
    let active = true;
    setPreview(null); setError('');
    post<Preview>('/api/photos/delete-preview', { photo_ids: ids }).then(value => {
      if (active) setPreview(value);
    }).catch(e => { if (active) setError(message(e)); });
    return () => { active = false; };
  }, [ids, reload]);
  async function remove() {
    if (!preview || submitting.current) return;
    submitting.current = true; setBusy(true); setError('');
    try {
      const result = await post<{ deleted: string[]; deleted_count: number }>('/api/photos/delete', {
        photo_ids: ids, revisions: Object.fromEntries(preview.items.map(p => [p.id, p.revision])),
      });
      deleted(result.deleted);
      notify(`已删除 ${result.deleted_count} 张照片`);
      close();
    } catch (e) { setError(message(e)); }
    finally { submitting.current = false; setBusy(false); }
  }
  return <Modal title={`删除照片 · ${ids.length} 张`} close={() => { if (!submitting.current) close(); }} footer={<>
    <button disabled={busy} onClick={close}>取消</button>
    <button className="photo-delete-confirm" disabled={busy || !preview || !!error} onClick={() => void remove()}>
      <Trash2 size={16} aria-hidden="true" />{busy ? '删除中…' : `确认删除 ${ids.length} 张`}
    </button>
  </>}>
    <p>确认删除以下不用的照片？删除后会移出照片列表、更新异常数量，并停止后续处理。</p>
    <p className="muted photo-delete-note">原文件留存备查；已上传官网、已发送到群及已导出的资料不会撤回。撤回官网照片，请到对应办理环节使用“删除并撤回”。删除后无法在此界面恢复。</p>
    <ErrorBox error={error} />
    {error && <button disabled={busy} onClick={() => setReload(n => n + 1)}>重新核对所选照片</button>}
    {!preview && !error ? <Loading /> : preview && <ul className="photo-delete-list" aria-label="即将删除的照片">
      {preview.items.map(p => <li key={p.id}>
        {p.thumbnail_url ? <img src={p.thumbnail_url} alt="" loading="lazy" /> : <FileImage size={32} aria-hidden="true" />}
        <div><strong>{p.original_name || '未命名照片'}</strong><small>{[p.project_name, p.short_code, p.case_title].filter(Boolean).join(' · ') || '尚未归档到案件'}</small></div>
      </li>)}
    </ul>}
  </Modal>;
}
