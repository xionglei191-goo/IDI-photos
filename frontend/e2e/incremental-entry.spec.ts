import { test, expect, type Page, type APIRequestContext } from '@playwright/test';

async function setup(page: Page, request: APIRequestContext, connected = true) {
  const auth = await (await request.get('/api/auth/status')).json();
  const login = await request.post(auth.setup_required ? '/api/auth/setup' : '/api/auth/login', {
    headers: { 'X-IDI-Request': '1' },
    data: { username: 'readback-admin', password: 'isolated-readback-password', display_name: '回读测试' },
  });
  expect(login.ok()).toBeTruthy();
  await page.context().addCookies((await request.storageState()).cookies);
  const calls: string[] = [], errors: string[] = [];
  let jobs: object[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/api/platform/session', route => {
    if (route.request().method() === 'POST') connected = true;
    return route.fulfill({ json: { connected, account: connected ? 'isolated-official-reader' : '', mode: 'read_only', write_enabled: false } });
  });
  await page.route('**/api/jobs', route => route.fulfill({ json: jobs }));
  for (const path of ['/api/workflow/sync', '/api/workflow/sync/recent']) {
    await page.route('**' + path, route => {
      if (route.request().method() !== 'POST') return route.continue();
      calls.push(path);
      return route.fulfill({ json: { id: 'isolated-sync-' + calls.length, state: 'queued' } });
    });
  }
  await page.goto('/');
  return { calls, errors, setJobs: (next: object[]) => { jobs = next; } };
}

async function openMaterials(page: Page) {
  await page.getByRole('button', { name: '系统设置', exact: true }).click();
  await page.getByRole('navigation', { name: '设置分类', exact: true }).getByRole('button', { name: '项目与资料', exact: true }).click();
}

test('board uses incremental sync and materials settings owns the full-sync action', async ({ page, request }, testInfo) => {
  const state = await setup(page, request);
  const manual = page.getByRole('button', { name: '手动同步', exact: true });
  await expect(manual).toHaveAttribute('title', /昨天、今天.*全部在办/);
  await expect(page.getByRole('button', { name: '手动完整同步', exact: true })).toHaveCount(0);
  await manual.click();
  await expect.poll(() => state.calls).toEqual(['/api/workflow/sync/recent']);
  await openMaterials(page);
  const panel = page.getByRole('region', { name: '官网完整同步', exact: true });
  const full = panel.getByRole('button', { name: '手动完整同步', exact: true });
  await expect(full).toBeVisible();
  await expect(page.getByRole('button', { name: '手动同步', exact: true })).toHaveCount(0);
  expect(state.calls).toHaveLength(1);
  await full.click();
  await expect.poll(() => state.calls).toEqual(['/api/workflow/sync/recent', '/api/workflow/sync']);
  for (const [width, height] of [[1440, 1000], [375, 900], [812, 375]]) {
    await page.setViewportSize({ width, height });
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await full.scrollIntoViewIfNeeded();
    await expect(full).toBeVisible();
    expect((await full.boundingBox())!.height).toBeGreaterThanOrEqual(44);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`full-sync-${width}.png`), fullPage: true });
  }
  expect(state.errors).toEqual([]);
});

for (const mode of ['incremental', 'full'] as const) {
  test(`${mode} selection survives the website login dialog`, async ({ page, request }) => {
    const state = await setup(page, request, false);
    if (mode === 'full') await openMaterials(page);
    await page.getByRole('button', { name: mode === 'full' ? '手动完整同步' : '手动同步', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: '连接官网', exact: true });
    await expect(dialog).toBeVisible();
    expect(state.calls).toEqual([]);
    await dialog.getByLabel('官网账号', { exact: true }).fill('isolated-official-reader');
    await dialog.getByLabel('官网密码', { exact: true }).fill('isolated-no-real-account');
    await dialog.getByRole('button', { name: '连接官网', exact: true }).click();
    await expect.poll(() => state.calls).toEqual([mode === 'full' ? '/api/workflow/sync' : '/api/workflow/sync/recent']);
    await expect(dialog).toHaveCount(0);
    expect(state.errors).toEqual([]);
  });
}

test('active incremental work prevents duplicate clicks while full sync remains separately available', async ({ page, request }) => {
  const state = await setup(page, request);
  state.setJobs([{ id: 'running-incremental', kind: 'platform_sync', payload: { mode: 'incremental' }, state: 'running', progress: 1, total: 5 }]);
  await page.reload();
  await expect(page.getByRole('group', { name: '官网同步', exact: true }).getByRole('button')).toBeDisabled();
  await expect(page.getByRole('group', { name: '官网同步', exact: true })).toContainText('同步中');
  await openMaterials(page);
  await expect(page.getByRole('button', { name: '手动完整同步', exact: true })).toBeEnabled();
  expect(state.calls).toEqual([]);
  expect(state.errors).toEqual([]);
});
