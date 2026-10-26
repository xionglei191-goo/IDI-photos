import { useCallback, useEffect, useState } from 'react';
import { ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight } from 'lucide-react';
import { api, message, queryString } from './api';
import './record-pagination.css';

interface RecordPage<T> { items: T[]; total: number; page: number; page_size: number; pages: number }
const PAGE_SIZES = [10, 20, 50, 100];

export function useRecordPage<T>(path: string, storageKey: string, revision: number | string = 0) {
  const [pageSize, setSize] = useState(() => {
    try { const value = Number(localStorage.getItem(storageKey)); return PAGE_SIZES.includes(value) ? value : 20; }
    catch { return 20; }
  });
  const [position, setPosition] = useState({ path, page: 1 });
  const requestedPage = position.path === path ? position.page : 1;
  const [reload, setReload] = useState(0);
  const url = path + (path.includes('?') ? '&' : '?') + queryString({ page: requestedPage, page_size: pageSize });
  const key = JSON.stringify([url, revision, reload]);
  const [response, setResponse] = useState<{ path: string; url: string; data: RecordPage<T> } | null>(null);
  const [request, setRequest] = useState({ key: '', loading: true, error: '' });
  useEffect(() => {
    const abort = new AbortController();
    setRequest({ key, loading: true, error: '' });
    api<RecordPage<T>>(url, { signal: abort.signal }).then(data => {
      if (abort.signal.aborted) return;
      setResponse({ path, url, data });
      setRequest({ key, loading: false, error: '' });
    }).catch(error => {
      if (!abort.signal.aborted) setRequest({ key, loading: false, error: message(error) });
    });
    return () => abort.abort();
  }, [key, path, url]);
  const loading = request.key !== key || request.loading;
  const error = request.key === key ? request.error : '';
  // Keep table height during a page fetch, but never show another project's rows.
  const data = response?.path === path ? response.data : null;
  const page = response?.url === url ? response.data.page : requestedPage;
  const setPage = (value: number) => setPosition({ path, page: value });
  const setPageSize = (value: number) => {
    if (!PAGE_SIZES.includes(value)) return;
    setSize(value); setPosition({ path, page: 1 });
    try { localStorage.setItem(storageKey, String(value)); } catch { /* current-session preference still works */ }
  };
  const refresh = useCallback(() => setReload(value => value + 1), []);
  return { items: error ? [] : data?.items || [], total: data?.total || 0, page, pageSize,
    pages: data?.pages || 1, loading, error, setPage, setPageSize, refresh };
}

export default function RecordPagination({ label, page, pageSize, total, pages, loading, error = '', setPage, setPageSize }: {
  label: string; page: number; pageSize: number; total: number; pages: number; loading: boolean; error?: string;
  setPage: (page: number) => void; setPageSize: (size: number) => void;
}) {
  const first = total ? (page - 1) * pageSize + 1 : 0;
  return <nav className="pagination record-pagination" aria-label={`${label}分页`}>
    <label className="inline-field">每页<select aria-label={`${label}每页条数`} value={pageSize} disabled={loading} onChange={event => setPageSize(Number(event.target.value))}>
      {PAGE_SIZES.map(value => <option key={value} value={value}>{value}</option>)}
    </select>条</label>
    <span className="muted record-page-summary" role="status" aria-live="polite">{loading ? `正在读取第 ${page} 页…` : error ? '读取失败，请重试' : `${first}–${Math.min(page * pageSize, total)} / 共 ${total} 条`}</span>
    <div className="row record-page-buttons">
      <button type="button" aria-label="首页" title="首页" disabled={loading || page <= 1} onClick={() => setPage(1)}><ChevronsLeft size={16} aria-hidden="true" /></button>
      <button type="button" aria-label="上一页" title="上一页" disabled={loading || page <= 1} onClick={() => setPage(page - 1)}><ChevronLeft size={16} aria-hidden="true" /></button>
      <span className="record-page-number">第 {page} / {pages} 页</span>
      <button type="button" aria-label="下一页" title="下一页" disabled={loading || page >= pages} onClick={() => setPage(page + 1)}><ChevronRight size={16} aria-hidden="true" /></button>
      <button type="button" aria-label="末页" title="末页" disabled={loading || page >= pages} onClick={() => setPage(pages)}><ChevronsRight size={16} aria-hidden="true" /></button>
    </div>
  </nav>;
}
