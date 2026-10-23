import { test, expect, type Page, type APIRequestContext } from '@playwright/test';

async function prepare(page: Page, request: APIRequestContext) {
  const auth = await (await request.get('/api/auth/status')).json();
  const login = await request.post(auth.setup_required ? '/api/auth/setup' : '/api/auth/login', {
    headers: { 'X-IDI-Request': '1' },
    data: { username: 'camera-ui-admin', password: 'isolated-camera-ui-password', display_name: '相机测试管理员' },
  });
  expect(login.ok()).toBeTruthy();
  await page.context().addCookies((await request.storageState()).cookies);
}

test('capture history shows ten rows per page with stage and terminal, including empty and partial pages', async ({ page, request }) => {
  await prepare(page, request);
  let count = 100;
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/api/camera/admin/official', route => route.fulfill({ json: {
    config: { enabled: false, auto_verify: false, ready: false },
    captures: Array.from({ length: count }, (_, i) => ({
      case_number: `CASE-${String(i + 1).padStart(3, '0')}`, stage_label: '施工过程', device_name: '现场手机',
      device_id: 'test-device', capture_id: `capture-${i}`, media_type: 'image', verification_state: 'not_requested', report: {},
    })),
  } }));
  await page.goto('/camera-setup');
  const rows = page.locator('#official-captures .device');
  await expect(rows).toHaveCount(10);
  await expect(rows.first().locator('.capture-title')).toHaveText('CASE-001 · 施工过程 · 现场手机');
  await expect(page.getByRole('button', { name: '上一页' })).toBeDisabled();
  for (let n = 1; n < 10; n++) await page.getByRole('button', { name: '下一页' }).click();
  await expect(rows.first()).toContainText('CASE-091');
  await expect(rows.last()).toContainText('CASE-100');
  await expect(page.locator('#capture-page-info')).toHaveText('第 10 / 10 页 · 共 100 条');
  await expect(page.getByRole('button', { name: '下一页' })).toBeDisabled();
  await page.getByRole('button', { name: '上一页' }).click();
  await expect(rows.first()).toContainText('CASE-081');
  count = 11;
  await page.getByRole('button', { name: '刷新拍摄记录' }).click();
  await expect(rows.first()).toContainText('CASE-001');
  await page.getByRole('button', { name: '下一页' }).click();
  await expect(rows).toHaveCount(1);
  await expect(rows.first()).toContainText('CASE-011');
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
  count = 0;
  await page.getByRole('button', { name: '刷新拍摄记录' }).click();
  await expect(rows).toHaveCount(0);
  await expect(page.locator('#capture-pagination')).toBeHidden();
  await expect(page.locator('#official-captures')).toContainText('还没有官方 SDK 拍摄记录');
  expect(errors).toEqual([]);
});

test('revoked phone can be deleted after confirmation and stays removed on reload', async ({ page, request }) => {
  await prepare(page, request);
  const headers = { 'X-IDI-Request': '1' };
  const projectResponse = await request.post('/api/projects', { headers, data: { name: '手机删除测试项目' } });
  expect(projectResponse.ok()).toBeTruthy();
  const project = await projectResponse.json();
  const admin = await (await request.get('/api/camera/admin')).json();
  const user = admin.users.find((u: { username: string }) => u.username === 'camera-ui-admin');
  const pairResponse = await request.post('/api/camera/admin/pairings', {
    headers, data: { name: '删除测试手机', user_id: user.id, project_ids: [project.id] },
  });
  expect(pairResponse.ok()).toBeTruthy();
  const paired = await request.post('/api/camera/pair', { headers, data: { code: (await pairResponse.json()).code } });
  expect(paired.ok()).toBeTruthy();
  const device = await paired.json();
  await page.goto('/camera-setup');
  const row = page.locator('#devices .device').filter({ hasText: '删除测试手机' });
  await expect(row.getByRole('button', { name: '删除', exact: true })).toHaveCount(0);
  await row.getByRole('button', { name: '停用连接' }).click();
  await expect(row).toContainText('已停用');
  page.once('dialog', dialog => dialog.dismiss());
  await row.getByRole('button', { name: '删除', exact: true }).click();
  await expect(row).toHaveCount(1);
  page.once('dialog', dialog => dialog.accept());
  await row.getByRole('button', { name: '删除', exact: true }).click();
  await expect(row).toHaveCount(0);
  await page.reload();
  await expect(page.locator('#devices')).toContainText('还没有已连接的手机');
  const access = await request.get('/api/camera/cases', { headers: { Authorization: 'Bearer ' + device.token } });
  expect(access.status()).toBe(401);
});
