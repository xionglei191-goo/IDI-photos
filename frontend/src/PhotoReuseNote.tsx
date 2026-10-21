import type { Photo } from './types';

export default function PhotoReuseNote({ photo, willCopy = false }: { photo: Photo; willCopy?: boolean }) {
  if (photo.reuse_source_id) return <small>查勘复用副本 · {photo.reuse_source_exists
    ? <a href={`/api/photos/${photo.reuse_source_id}/original`} target="_blank" rel="noreferrer">查看查勘原照</a>
    : '查勘原照已移除，副本保留'}</small>;
  if (willCopy) return <small>{photo.reuse_copy_id ? '保存后使用已有维修打卡副本' : '保存后复制为维修打卡'}</small>;
  return photo.reuse_copy_id ? <small>已生成维修打卡副本</small> : null;
}
