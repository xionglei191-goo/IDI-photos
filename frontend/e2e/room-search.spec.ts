import { test, expect } from '@playwright/test';
import { mkdir } from 'node:fs/promises';
import path from 'node:path';

test('raw official address is searchable in the board, navigation and assigned worker list', async ({ page, request, browser }) => {
  const headers = { 'X-IDI-Request': '1' }, password = 'isolated-room-search-password';
  const auth = await (await request.get('/api/auth/status')).json();
  expect((await request.post(auth.setup_required ? '/api/auth/setup' : '/api/auth/login', {
    headers, data: { username: 'room-search-admin', display_name: '房号搜索验收', password },
  })).ok()).toBeTruthy();
  async function create(url: string, data: object) {
    const result = await request.post(url, { headers, data });
    expect(result.ok(), await result.text()).toBeTruthy(); return result.json();
  }
  const project = await create('/api/projects', { name: '斜杠地址搜索验收项目' });
  const target = await create('/api/cases', { project_id: project.id, title: '墙面开裂两处',
    platform_report_code: 'ZLBH202610211619130264', location_detail: '验收项目A8栋/未划分/27F/2702' });
  const hidden = await create('/api/cases', { project_id: project.id, title: '未分配的同房号案件', location_detail: '验收项目B8栋/未划分/27F/2702' });
  const other = await create('/api/cases', { project_id: project.id, title: '另一户案件', location_detail: '验收项目A8栋/未划分/28F/2802' });
  const worker = await create('/api/access/users', { username: 'room-search-worker', display_name: '现场工人', password });
  expect((await request.put(`/api/access/projects/${project.id}/members`, { headers,
    data: { members: [{ user_id: worker.id, role: 'worker' }] } })).ok()).toBeTruthy();
  await create('/api/access/case-assignments', { case_ids: [target.id], user_ids: [worker.id], mode: 'replace', expected_revisions: { [target.id]: 0 } });
  await page.context().addCookies((await request.storageState()).cookies);
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/');
  await page.getByLabel('搜索案件', { exact: true }).fill('2702');
  await expect(page.locator(`.board-table tr[data-case-id="${target.id}"]`)).toBeVisible();
  await expect(page.locator(`.board-table tr[data-case-id="${hidden.id}"]`)).toBeVisible();
  await expect(page.locator(`.board-table tr[data-case-id="${other.id}"]`)).toHaveCount(0);
  await page.getByLabel('快速定位案件', { exact: true }).fill('2702');
  await expect(page.locator('.case-tree')).toContainText('ZLBH202610211619130264');
  await expect(page.locator('.case-tree')).not.toContainText('2802');
  const out = path.resolve('..', 'artifacts', 'room-search-2702-20261031');
  await mkdir(out, { recursive: true });
  await page.screenshot({ path: path.join(out, 'admin-room-search.png'), fullPage: true });
  const context = await browser.newContext({ baseURL: 'http://127.0.0.1:8774' });
  expect((await context.request.post('/api/auth/login', { headers, data: { username: worker.username, password } })).ok()).toBeTruthy();
  const workerPage = await context.newPage();
  workerPage.on('pageerror', error => errors.push(error.message));
  await workerPage.goto('/');
  await workerPage.getByLabel('查找案件', { exact: true }).fill('2702');
  await expect(workerPage.locator('tbody tr')).toHaveCount(1);
  await expect(workerPage.locator('tbody')).toContainText('墙面开裂两处');
  await expect(workerPage.locator('body')).not.toContainText(hidden.title);
  await workerPage.screenshot({ path: path.join(out, 'worker-room-search.png'), fullPage: true });
  await context.close();
  expect(errors).toEqual([]);
});
