export async function api<T = unknown>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  headers.set('X-IDI-Request', '1');
  if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json');
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    let message = `请求失败 (${response.status})`;
    try { const error = await response.json(); message = typeof error.detail === 'string' ? error.detail : JSON.stringify(error.detail || error); } catch { /* retain status */ }
    throw new Error(message);
  }
  return response.json() as Promise<T>;
}
export const post = <T = unknown>(path: string, data?: unknown) => api<T>(path, { method: 'POST', body: data === undefined ? undefined : JSON.stringify(data) });
export const patch = <T = unknown>(path: string, data: unknown) => api<T>(path, { method: 'PATCH', body: JSON.stringify(data) });
export const put = <T = unknown>(path: string, data: unknown) => api<T>(path, { method: 'PUT', body: JSON.stringify(data) });
export function queryString(values: Record<string, unknown>) { const q = new URLSearchParams(); Object.entries(values).forEach(([key, value]) => { if (value !== '' && value !== undefined && value !== null) q.set(key, String(value)); }); return q.toString(); }
export function message(error: unknown) { return error instanceof Error ? error.message : String(error); }
