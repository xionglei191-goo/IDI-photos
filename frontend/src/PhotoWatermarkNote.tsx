import type { Photo } from './types';

export default function PhotoWatermarkNote({ photo }: { photo: Photo }) {
  if (photo.media_type !== 'image') return null;
  return <>
    {photo.watermark_code && <p className="muted break-all">
      水印码：{photo.watermark_code}
      {photo.source_type === 'xhey_camera' && <><br />当前使用手机直传原片</>}
    </p>}
    {photo.watermark_dedup_reason && <p className="notice" role="status">{photo.watermark_dedup_reason}</p>}
  </>;
}
