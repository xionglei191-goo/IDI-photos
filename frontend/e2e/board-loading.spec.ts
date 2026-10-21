import { test, expect } from '@playwright/test';
import type { BoardData, SyncStatus } from '../src/workflowTypes';

test('board opens once without waiting for system details and ignores progress-only refreshes', async ({ page, request }) => {
  const headers = { 'X-IDI-Request': '1' };
  const project = await (await request.post('/api/projects', { headers, data: { name: '清单加载验证' + Date.now() } })).json();
  await request.post('/api/cases', { headers, data: { project_id: project.id, title: '加载速度验证', room: '1201', building: '1' } });
  const data: BoardData = await (await request.get('/api/workflow/board?source=local')).json();
  const original = data.items.find(row => row.project_id === project.id)!;
  const sync: SyncStatus = { ...data.sync, lists: { run_id: 'test', read_at: '2026-10-20', counts: { intake: 65, repair: 65 } } };
  const items = Array.from({ length: 65 }, (_, i) => ({ ...original, id: `loading-${i}`, title: `清单分页验证${i}`, platform_report_code: `TEST${i}` }));
  const reads: URL[] = [], errors: string[] = [];
  let releaseSystem!: () => void, systemReleased = false, jobReads = 0, jobState = 'running';
  const holdSystem = new Promise<void>(resolve => { releaseSystem = resolve; });
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/api/system', async route => {
    await holdSystem;
    await route.continue();
  });
  await page.route('**/api/workflow/sync', route => route.fulfill({ json: sync }));
  const summary = await (await request.get('/api/summary')).json();
  await page.route('**/api/summary', route => route.fulfill({ json: { ...summary, active_jobs: 1 } }));
  await page.route('**/api/jobs', route => route.fulfill({ json: [{
    id: 'progress-test', kind: 'export', state: jobState, progress: ++jobReads, total: 100,
    attempts: 1, payload: {}, result: null, error: null, created_at: '2026-10-20', finished_at: null,
  }] }));
  await page.route('**/api/workflow/board?*', route => {
    const url = new URL(route.request().url());
    reads.push(url);
    const pageNumber = Number(url.searchParams.get('page') || 1), query = url.searchParams.get('query');
    const filtered = query ? items.filter(item => item.title.includes(query)) : items;
    return route.fulfill({ json: { ...data, sync, source: 'platform', source_total: 65, total: filtered.length, page: pageNumber,
      items: filtered.slice((pageNumber - 1) * 30, pageNumber * 30), counts: { pending_intake: 65 } } });
  });
  try {
    await page.goto('/', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('.board-table tbody tr')).toHaveCount(30);
    expect(systemReleased).toBe(false);
    expect(reads.map(url => url.searchParams.get('source'))).toEqual(['platform']);
    releaseSystem(); systemReleased = true;
    await expect.poll(() => jobReads, { timeout: 10000 }).toBeGreaterThanOrEqual(3);
    expect(reads).toHaveLength(1);
    jobState = 'completed';
    await expect.poll(() => reads.length, { timeout: 10000 }).toBe(2);
    await page.getByRole('button', { name: '下一页案件' }).click();
    await expect(page.locator('.board-table tbody tr').first()).toContainText('TEST30');
    await page.getByRole('textbox', { name: '搜索案件', exact: true }).fill('清单分页验证64');
    await expect(page.locator('.board-table tbody tr')).toHaveCount(1);
    await expect(page.locator('.board-table tbody tr')).toContainText('TEST64');
    expect(errors).toEqual([]);
  } finally {
    if (!systemReleased) releaseSystem();
  }
});
