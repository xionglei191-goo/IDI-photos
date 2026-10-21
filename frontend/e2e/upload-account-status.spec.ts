import { test, expect, type Page, type APIRequestContext } from '@playwright/test';

const headers = { 'X-IDI-Request': '1' };
const session = { connected: true, account: 'company-reader', mode: 'read_only', write_enabled: false };

async function prepare(page: Page, request: APIRequestContext) {
  const auth = await (await request.get('/api/auth/status')).json();
  const credentials = { username: 'upload-ui-admin', password: 'isolated-upload-ui-password' };
  const login = await request.post(auth.setup_required ? '/api/auth/setup' : '/api/auth/login', {
    headers, data: { ...credentials, display_name: '隔离测试管理员' },
  });
  expect(login.ok()).toBeTruthy();
  await page.context().addCookies((await request.storageState()).cookies);
  const projects = await (await request.get('/api/projects')).json();
  const create = async (name: string) => projects.find((p: { name: string }) => p.name === name)
    || (await (await request.post('/api/projects', { headers, data: { name } })).json());
  const first = await create('A号项目（已连接）'), second = await create('C号项目（待配置）');
  await page.route('**/api/platform/session', route => route.fulfill({ json: session }));
  const runtime = await (await request.get('/api/operations')).json();
  await page.route('**/api/operations', route => route.fulfill({ json: { ...runtime, platform: session } }));
  const status = {
    policy: { enabled: true }, account_scope: 'project', company_connected: true, connected: true,
    write_enabled: true, can_configure_project_accounts: true, counts: { awaiting_login: 1 },
    project_connections: [
      { project_id: first.id, project_name: first.name, state: 'connected', connected: true },
      { project_id: second.id, project_name: second.name, state: 'unconfigured', connected: false },
    ],
    items: [{ case_id: 'pending-upload', project_id: second.id, project_name: second.name, enabled: true,
      state: 'awaiting_login', stage: 'repair', short_code: 'DEMO-C1-1101-01', review_id: null, save_review_id: null,
      detail: { files: [{ label: '楼栋打卡照片', count: 1 }], saved_count: 1 } }],
  };
  await page.route('**/api/workflow/uploads', route => route.fulfill({ json: status }));
  return { first, second, status };
}

async function openProgress(page: Page) {
  await page.goto('/');
  await page.getByRole('button', { name: '系统设置', exact: true }).click();
  await page.getByRole('navigation', { name: '设置分类' }).getByRole('button', { name: '后台任务', exact: true }).click();
  return page.getByRole('region', { name: '后台保存进度', exact: true });
}

test('missing writer is identified even with another project connected and opens the correct project', async ({ page, request }, testInfo) => {
  const { first, second } = await prepare(page, request);
  const errors: string[] = [], writes: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => { if (request.url().includes('/api/') && !['GET', 'HEAD'].includes(request.method())) writes.push(request.url()); });
  const progress = await openProgress(page);
  await expect(page.getByRole('button', { name: '公司官网已连接', exact: true })).toBeVisible();
  await expect(progress).toContainText('项目官网账号尚未配置');
  await expect(progress).toContainText(second.name);
  await expect(progress).not.toContainText(first.name);
  await expect(progress.getByRole('button', { name: '连接官网', exact: true })).toHaveCount(0);
  const configure = progress.getByRole('button', { name: `配置项目官网账号：${second.name}`, exact: true });
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    await expect(configure).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`account-status-${width}.png`), fullPage: true });
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  await configure.click();
  const target = page.locator('[aria-label="本项目官网写入账号设置"]');
  await expect(target).toContainText(`当前项目：${second.name}`);
  await expect(page.getByLabel('选择项目', { exact: true })).toHaveValue(second.id);
  await expect(target.getByLabel('系统账号', { exact: true })).toBeEnabled();
  await expect(target).toBeFocused();
  await expect(page.getByRole('dialog', { name: '连接官网', exact: true })).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath('correct-project-account.png'), fullPage: true });
  expect(writes).toEqual([]);
  expect(errors).toEqual([]);
});

test('member sees a specific administrator instruction without an unavailable settings action', async ({ page, request }) => {
  const { status } = await prepare(page, request);
  status.can_configure_project_accounts = false;
  const progress = await openProgress(page);
  await expect(progress).toContainText('请联系系统管理员处理本项目官网账号');
  await expect(progress.getByRole('button', { name: /项目官网账号/ })).toHaveCount(0);
  await expect(progress.getByRole('button', { name: '连接官网', exact: true })).toHaveCount(0);
});

test('paused writer and automatic reconnect retain their separate explanations', async ({ page, request }) => {
  const { second, status } = await prepare(page, request);
  status.project_connections[1].state = 'write_paused';
  const progress = await openProgress(page);
  await expect(progress).toContainText('本项目的官网保存已暂停');
  await expect(progress.getByRole('button', { name: `查看项目官网账号：${second.name}`, exact: true })).toBeVisible();
  status.project_connections[1].state = 'retry_wait';
  await expect(progress).toContainText('项目官网账号连接中断，等待自动重试');
  status.project_connections[1].state = 'connected';
  status.project_connections[1].connected = true;
  await expect(progress.locator('.upload-project-connections')).toHaveCount(0);
});
