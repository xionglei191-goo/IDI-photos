import { useEffect, useState } from 'react';
import { api, message } from './api';
import { ErrorBox, Loading } from './common';
import ReviewBatchDialog from './ReviewBatchDialog';
import type { ReviewBatch } from './workflowTypes';

export default function ReviewLink({ reviewId }: { reviewId: string }) {
  const [batch, setBatch] = useState<ReviewBatch | null>(null), [error, setError] = useState(''), [status, setStatus] = useState('');
  useEffect(() => {
    let alive = true;
    if (!/^[a-f0-9]{32}$/.test(reviewId)) { setError('核对链接无效，请从最新飞书卡片重新打开。'); return; }
    void api<ReviewBatch>('/api/workflow/reviews/' + reviewId)
      .then(next => { if (alive) setBatch(next); })
      .catch(e => { if (alive) setError(message(e)); });
    return () => { alive = false; };
  }, [reviewId]);
  return <main className="access-settings" style={{ maxWidth: 1100, margin: '32px auto', padding: 16 }}>
    <h1>拟提交资料核对</h1><p>核对当前案件、全部字段和附件后确认办理。</p>
    <ErrorBox error={error} />{status && <p role="status">{status}</p>}
    <a className="button" href="/">返回 IDI 工作台</a>
    {!batch && !error && <Loading />}
    {batch && <ReviewBatchDialog initial={batch} close={() => window.location.assign('/')} changed={() => {}}
      notify={(value, failed) => { if (failed) setError(value); else setStatus(value); }} />}
  </main>;
}
