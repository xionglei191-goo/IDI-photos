import { useEffect, useId, useRef, useState } from 'react';
import { ChevronLeft, ChevronRight, ExternalLink, FileText, ImageOff, RefreshCw, Trash2, X, ZoomIn, ZoomOut } from 'lucide-react';
import { dateTime, PHOTO_GROUPS } from './common';
import type { OfficialFile } from './platformTypes';
import type { Photo } from './types';
import './attachment-gallery.css';

export interface Attachment {
  id: string; fileId: string; name: string; original: string; thumbnail: string | null; image: boolean;
  official: boolean; detail: string;
}

export function officialAttachments(files: OfficialFile[], base: string): Attachment[] {
  return files.map((file, index) => {
    const name = file.oldFileName || file.fileName || file.name || `官网附件 ${index + 1}`;
    const image = /^(image\/|\.?(jpg|jpeg|png|gif|webp|bmp|tif|tiff)$)/i.test(file.fileType || '')
      || [name, file.fileUrl || ''].some(value => /\.(jpg|jpeg|png|gif|webp|bmp|tif|tiff)(?:[?#]|$)/i.test(value));
    const path = `${base}/${encodeURIComponent(file.id)}`;
    const version = `?source=${encodeURIComponent(file.fileUrl || '')}`;
    return { id: `official-${file.id}-${file.fileUrl}`, fileId: String(file.id), name, original: `${path}/original${version}`, thumbnail: image ? `${path}/thumbnail${version}` : null,
      image, official: true, detail: image ? '点击照片放大核对' : '点击查看或下载原件' };
  });
}

export function selectedAttachments(photos: Photo[]): Attachment[] {
  return photos.map(photo => ({
    id: `local-${photo.id}`, fileId: photo.id, name: photo.original_name, original: photo.original_url,
    thumbnail: photo.thumbnail_url || (photo.media_type === 'image' ? photo.original_url : null),
    image: photo.media_type === 'image', official: false,
    detail: [PHOTO_GROUPS[photo.photo_group], photo.watermark_time && dateTime(photo.watermark_time), photo.part || photo.process].filter(Boolean).join(' · '),
  }));
}

function AttachmentCard({ item, open, remove, disabled }: { item: Attachment; open: () => void; remove?: () => void; disabled?: boolean }) {
  const [state, setState] = useState<'loading' | 'loaded' | 'failed'>('loading');
  const [attempt, setAttempt] = useState(0);
  const thumbnail = item.thumbnail && `${item.thumbnail}${attempt ? `${item.thumbnail.includes('?') ? '&' : '?'}retry=${attempt}` : ''}`;
  return <article className="attachment-card" data-source={item.official ? 'official' : 'local'}>
    {item.image ? <div className="attachment-image">
      <button type="button" className="attachment-open" aria-label={`查看${item.name}大图`} onClick={open}>
        {thumbnail && state !== 'failed' && <img src={thumbnail} alt={item.name} loading="lazy"
          onLoad={() => setState('loaded')} onError={() => setState('failed')} />}
        {state === 'loading' && <span className="attachment-placeholder" role="status"><RefreshCw size={20} aria-hidden="true" />{item.official ? '正在同步官网照片…' : '正在加载照片…'}</span>}
        {state === 'failed' && <span className="attachment-placeholder"><ImageOff size={26} aria-hidden="true" />照片暂时无法显示</span>}
      </button>
      {state === 'failed' && <button type="button" className="attachment-retry" onClick={() => { setState('loading'); setAttempt(value => value + 1); }}><RefreshCw size={14} aria-hidden="true" />{item.official ? '重试同步' : '重新加载'}</button>}
    </div> : <a className="attachment-document" href={item.original} target="_blank" rel="noreferrer"><FileText size={30} aria-hidden="true" /><span>查看原件</span></a>}
    <div className="attachment-caption">
      <span className={`attachment-source ${item.official ? 'official' : 'selected'}`}>{item.official ? '官网已有' : '本次新增'}</span>
      {item.official && state === 'loaded' && <span className="attachment-synced">已在本地</span>}
      <strong title={item.name}>{item.name}</strong>
      {item.detail && <small title={item.detail}>{item.detail}</small>}
      {remove && <button type="button" className="attachment-remove" disabled={disabled} aria-label={`${item.official ? '删除并撤回' : '移除选材'}：${item.name}`} onClick={remove}><Trash2 size={15} aria-hidden="true" />{item.official ? '删除并撤回' : '移除选材'}</button>}
    </div>
  </article>;
}

function AttachmentViewer({ items, index, setIndex, close, label }: {
  items: Attachment[]; index: number; setIndex: (index: number) => void; close: () => void; label: string;
}) {
  const ref = useRef<HTMLDialogElement>(null), titleId = useId();
  const [zoomed, setZoomed] = useState(false), [failed, setFailed] = useState(false);
  const item = items[index];
  useEffect(() => {
    const dialog = ref.current;
    dialog?.showModal();
    return () => { if (dialog?.open) dialog.close(); };
  }, []);
  useEffect(() => { setZoomed(false); setFailed(false); }, [index]);
  function move(direction: number) { setIndex(Math.max(0, Math.min(items.length - 1, index + direction))); }
  return <dialog ref={ref} className="modal modal-wide attachment-viewer" aria-labelledby={titleId}
    onCancel={event => { event.preventDefault(); event.stopPropagation(); close(); }}
    onKeyDown={event => {
      if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
        event.preventDefault(); event.stopPropagation(); move(event.key === 'ArrowLeft' ? -1 : 1);
      }
    }}>
    <header className="modal-head"><h2 id={titleId}>{label} · 照片预览</h2><button type="button" className="icon-button" aria-label="关闭照片预览" onClick={close}><X size={20} aria-hidden="true" /></button></header>
    <div className="modal-body">
      <div className={`attachment-viewer-canvas ${zoomed ? 'is-zoomed' : ''}`}>
        {failed ? <p className="attachment-viewer-error" role="alert">原图暂时无法显示，请关闭后重试同步。</p> : <img src={item.original} alt={item.name} onError={() => setFailed(true)} />}
      </div>
      <p className="attachment-viewer-caption">{item.name}<small>{item.official ? '官网已有 · 本地原件' : '本次新增 · 本地原件'}</small></p>
    </div>
    <footer className="modal-foot">
      <span className="grow muted" role="status">第 {index + 1} / {items.length} 张</span>
      <button type="button" disabled={index === 0} onClick={() => move(-1)}><ChevronLeft size={16} aria-hidden="true" />上一张</button>
      <button type="button" disabled={index === items.length - 1} onClick={() => move(1)}>下一张<ChevronRight size={16} aria-hidden="true" /></button>
      <button type="button" disabled={failed} aria-pressed={zoomed} onClick={() => setZoomed(value => !value)}>{zoomed ? <ZoomOut size={16} aria-hidden="true" /> : <ZoomIn size={16} aria-hidden="true" />}{zoomed ? '适应窗口' : '放大细看'}</button>
      <a className="button" href={item.original} target="_blank" rel="noreferrer"><ExternalLink size={16} aria-hidden="true" />打开原件</a>
    </footer>
  </dialog>;
}

export default function AttachmentGallery({ items, label, onRemove, removeDisabled, canRemove }: {
  items: Attachment[]; label: string; onRemove?: (item: Attachment) => void;
  removeDisabled?: boolean | ((item: Attachment) => boolean); canRemove?: (item: Attachment) => boolean;
}) {
  const [current, setCurrent] = useState<number | null>(null);
  const opener = useRef<HTMLElement | null>(null);
  const images = items.filter(item => item.image);
  useEffect(() => { if (current === null) opener.current?.focus(); }, [current]);
  return <>
    <div className="attachment-gallery" aria-label={`${label}附件`}>
      {items.map(item => <AttachmentCard key={item.id} item={item} remove={onRemove && (!canRemove || canRemove(item)) ? () => onRemove(item) : undefined} disabled={typeof removeDisabled === 'function' ? removeDisabled(item) : removeDisabled} open={() => {
        opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
        setCurrent(images.findIndex(image => image.id === item.id));
      }} />)}
    </div>
    {current !== null && images[current] && <AttachmentViewer items={images} index={current} setIndex={setCurrent} close={() => setCurrent(null)} label={label} />}
  </>;
}
