import { test, expect, type APIRequestContext } from '@playwright/test';
import { mkdir } from 'node:fs/promises';
import path from 'node:path';

const headers = { 'X-IDI-Request': '1' }, password = 'isolated-worker-access-password';
const out = path.resolve('..', 'artifacts', 'worker-case-access-20261031');

test('administrators assign workers in bulk and workers only see associated cases', async ({ page, request, browser }) => {
  const auth = await (await request.get('/api/auth/status')).json();
  expect((await request.post(auth.setup_required ? '/api/auth/setup' : '/api/auth/login', {
    headers, data: { username: 'worker-access-admin', display_name: '权限验收管理员', password },
  })).ok()).toBeTruthy();
  async function create(url: string, data: object) {
    const result = await request.post(url, { headers, data }); expect(result.ok(), await result.text()).toBeTruthy(); return result.json();
  }
  const project = await create('/api/projects', { name: '现场工人权限验收项目' });
  const cases = [];
  for (let n = 1; n <= 3; n++) cases.push(await create('/api/cases', { project_id: project.id, title: `工人分配验收案件${n}`, building: '1', room: '30' + n }));
  const workers = [];
  for (const [username, display_name] of [['worker-zhang', '张师傅'], ['worker-li', '李师傅'], ['worker-manager', '项目管理员']])
    workers.push(await create('/api/access/users', { username, display_name, password, system_role: 'user' }));
  expect((await request.put(`/api/access/projects/${project.id}/members`, { headers,
    data: { members: workers.map((w, i) => ({ user_id: w.id, role: i === 2 ? 'manager' : 'worker' })) } })).ok()).toBeTruthy();
  await page.context().addCookies((await request.storageState()).cookies);
  const errors: string[] = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.goto('/');
  const row = (id: string) => page.locator(`.board-table tr[data-case-id="${id}"]`);
  await expect(row(cases[0].id)).toContainText('未分配工人');
  await row(cases[0].id).getByRole('checkbox').check();
  await row(cases[1].id).getByRole('checkbox').check();
  await page.getByRole('button', { name: '批量分配工人', exact: true }).click();
  let dialog = page.getByRole('dialog');
  await dialog.getByRole('checkbox', { name: /张师傅/ }).check();
  await dialog.getByRole('checkbox', { name: /李师傅/ }).check();
  await dialog.getByRole('button', { name: '保存分配', exact: true }).click();
  await expect(dialog).toBeHidden();
  await expect(row(cases[0].id)).toContainText('张师傅');
  await expect(row(cases[1].id)).toContainText('李师傅');
  await page.getByLabel('筛选工人分配').selectOption('unassigned');
  await expect(page.locator('.board-table tbody tr')).toHaveCount(1);
  await expect(row(cases[2].id)).toBeVisible();
  await page.getByLabel('筛选工人分配').selectOption('');
  await row(cases[0].id).getByRole('button', { name: '分配工人', exact: true }).click();
  dialog = page.getByRole('dialog');
  await dialog.getByRole('checkbox', { name: /李师傅/ }).uncheck();
  await dialog.getByRole('button', { name: '保存分配', exact: true }).click();
  await expect(row(cases[0].id)).not.toContainText('李师傅');
  await mkdir(out, { recursive: true });
  await page.screenshot({ path: path.join(out, 'admin-case-list.png'), fullPage: true });
  await row(cases[0].id).locator('.case-link').click();
  await expect(page.getByRole('region', { name: '现场工人' })).toContainText('张师傅');
  await page.getByRole('region', { name: '现场工人' }).getByRole('button', { name: '分配工人' }).click();
  await page.getByRole('dialog').getByText('分配记录（最近 30 条）').click();
  await expect(page.getByRole('dialog')).toContainText('权限验收管理员');
  await page.screenshot({ path: path.join(out, 'assignment-history.png'), fullPage: true });
  await page.getByRole('dialog').getByRole('button', { name: '取消', exact: true }).click();

  for (const c of cases) await create(`/api/workflow/cases/${c.id}/result`, { action: 'accepted', event_at: '2026-10-31T09:00', note: '隔离验收：现场查勘', revision: c.revision });
  await page.getByRole('navigation', { name: '工作看板' }).getByRole('button', { name: '查勘维修', exact: true }).click();
  const workRow = page.locator(`.work-board-table tbody[data-case-id="${cases[1].id}"]`);
  await expect(workRow).toContainText('张师傅');
  await expect(workRow.getByRole('button', { name: '分配工人' })).toBeVisible();
  await page.screenshot({ path: path.join(out, 'work-case-list.png'), fullPage: true });

  async function login(contextRequest: APIRequestContext, username: string) {
    expect((await contextRequest.post('/api/auth/login', { headers, data: { username, password } })).ok()).toBeTruthy();
  }
  const managerContext = await browser.newContext({ baseURL: 'http://127.0.0.1:8772' });
  await login(managerContext.request, 'worker-manager');
  const managerPage = await managerContext.newPage(); managerPage.on('pageerror', e => errors.push(e.message));
  await managerPage.goto('/');
  await managerPage.getByRole('navigation', { name: '工作看板' }).getByRole('button', { name: '查勘维修', exact: true }).click();
  await expect(managerPage.locator(`tbody[data-case-id="${cases[1].id}"]`).getByRole('button', { name: '分配工人' })).toBeVisible();
  await managerPage.locator(`tbody[data-case-id="${cases[1].id}"]`).getByRole('button', { name: '分配工人' }).click();
  await expect(managerPage.getByRole('dialog').getByRole('checkbox', { name: /张师傅/ })).toBeChecked();
  await managerContext.close();

  const workerContext = await browser.newContext({ baseURL: 'http://127.0.0.1:8772' });
  await login(workerContext.request, 'worker-zhang');
  const workerPage = await workerContext.newPage(); workerPage.on('pageerror', e => errors.push(e.message));
  await workerPage.goto('/');
  await expect(workerPage.locator('tbody tr')).toHaveCount(2);
  await expect(workerPage.locator('body')).not.toContainText('工人分配验收案件3');
  await workerPage.getByRole('button', { name: '打开案件' }).first().click();
  await expect(workerPage.locator('body')).toContainText('请使用现有相机同步案件');
  await expect(workerPage.getByRole('button', { name: '上传本案照片' })).toHaveCount(0);
  await expect(workerPage.getByRole('button', { name: '分配工人' })).toHaveCount(0);
  await workerPage.screenshot({ path: path.join(out, 'worker-case.png'), fullPage: true });
  await workerPage.getByRole('button', { name: '返回案件列表' }).click();
  const detail = await (await request.get(`/api/cases/${cases[0].id}/workers`)).json();
  await create('/api/access/case-assignments', { case_ids: [cases[0].id], user_ids: [], mode: 'replace', expected_revisions: { [cases[0].id]: detail.revision } });
  await workerPage.getByRole('button', { name: '刷新', exact: true }).click();
  await expect(workerPage.locator('tbody tr')).toHaveCount(1);
  await expect(workerPage.locator('tbody')).toContainText('工人分配验收案件2');
  await workerContext.close();
  expect(errors).toEqual([]);
});
