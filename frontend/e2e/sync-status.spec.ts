import { test, expect } from '@playwright/test';

test('automatic sync settings persist separately from manual workbench actions', async ({ page, request }, testInfo) => {
  const errors: string[] = [];
  page.on('pageerror', e => errors.push(e.message));
  await request.put('/api/workflow/sync/policy', { headers: { 'X-IDI-Request': '1' }, data: { enabled: true } });
  await page.goto('/');
  await expect(page.getByRole('button', { name: /自动同步/ })).toHaveCount(0);
  const manual = page.getByRole('button', { name: '手动同步', exact: true });
  await expect(manual).toBeVisible();
  await page.getByRole('button', { name: '系统设置', exact: true }).click();
  const controls = page.getByRole('group', { name: '官网同步', exact: true });
  const auto = controls.getByRole('button', { name: /自动同步/ });
  await expect(auto).toHaveAttribute('aria-pressed', 'true');
  await expect(auto).toHaveAttribute('title', /每天 08:00.*官网尚未连接/);
  await expect(manual).toHaveCount(0);
  await auto.click();
  await expect(auto).toContainText('已暂停');
  const policy = await (await request.get('/api/workflow/sync')).json();
  expect(policy.schedule.enabled).toBe(false);
  await page.reload();
  await page.getByRole('button', { name: '系统设置', exact: true }).click();
  await expect(auto).toBeEnabled();
  await expect(auto).toHaveAttribute('aria-pressed', 'false');
  await auto.focus();
  await page.keyboard.press('Enter');
  await expect(auto).toHaveAttribute('aria-pressed', 'true');
  await page.route('**/api/workflow/sync/policy', route => route.fulfill({ status: 500, json: { detail: '同步设置暂未保存，请重试' } }));
  await auto.click();
  await expect(page.getByRole('alert')).toContainText('同步设置暂未保存，请重试');
  await expect(auto).toHaveAttribute('aria-pressed', 'true');
  await page.unroute('**/api/workflow/sync/policy');
  await page.getByRole('button', { name: '关闭提示', exact: true }).click();
  for (const width of [1440, 1024, 768, 390, 375, 320]) {
    await page.setViewportSize({ width, height: 1000 });
    await expect(controls).toBeVisible();
    const box = await controls.boundingBox();
    expect(box!.x).toBeGreaterThanOrEqual(0);
    expect(box!.x + box!.width).toBeLessThanOrEqual(width + 1);
    expect((await auto.boundingBox())!.height).toBeGreaterThanOrEqual(44);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`automatic-settings-${width}.png`), fullPage: true });
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.getByRole('button', { name: '返回工作台', exact: true }).click();
  await page.getByRole('navigation', { name: '工作看板' }).getByRole('button', { name: '查勘维修', exact: true }).click();
  await expect(page.getByRole('button', { name: /自动同步/ })).toHaveCount(0);
  await manual.click();
  const login = page.getByRole('dialog', { name: '连接官网', exact: true });
  await expect(login).toBeVisible();
  await login.getByRole('button', { name: '关闭', exact: true }).first().click();
  await expect(login).toHaveCount(0);
  expect((await (await request.get('/api/workflow/sync')).json()).schedule.enabled).toBe(true);
  for (const width of [1440, 390, 320]) {
    await page.setViewportSize({ width, height: 1000 });
    await expect(manual).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  }
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.setViewportSize({ width: 812, height: 375 });
  await expect(manual).toBeInViewport();
  expect(errors).toEqual([]);
});

test('incremental ledger says cumulative coverage and exposes actual photo verification time', async ({ page, request }, testInfo) => {
  const headers = { 'X-IDI-Request': '1' };
  const project = await (await request.post('/api/projects', { headers, data: { name: '增量界面验证项目' } })).json();
  const c = await (await request.post('/api/cases', { headers, data: { project_id: project.id, title: '照片核验示例', building: '1', room: '101' } })).json();
  await request.post(`/api/workflow/cases/${c.id}/result`, { headers, data: { action: 'accepted', revision: c.revision, event_at: '2026-10-16 09:00', note: '隔离浏览器验收样例' } });
  const board = await (await request.get(`/api/workflow/board?board=work&source=local&project_id=${project.id}`)).json();
  expect(board.items.length).toBeGreaterThan(0);
  board.source = 'platform'; board.source_total = board.total;
  board.sync.lists = { coverage: 'incremental', counts: { intake: 1, repair: 1 } };
  board.items[0].online_photos.read_at = '2026-10-16T01:23:00+00:00';
  await page.route('**/api/workflow/board?*', async route => {
    if (new URL(route.request().url()).searchParams.get('board') === 'work') await route.fulfill({ json: board });
    else await route.continue();
  });
  await page.goto('/');
  await page.getByRole('navigation', { name: '工作看板' }).getByRole('button', { name: '查勘维修', exact: true }).click();
  await page.getByRole('button', { name: '官网清单', exact: true }).click();
  await expect(page.getByText('本地累计：1 案', { exact: true })).toBeVisible();
  const cell = page.locator('.photo-source-cell.official-source').first();
  await expect(cell).toContainText('10/16 09:23');
  await expect(cell).toHaveAttribute('title', /检查列表不会更新此时间/);
  await page.screenshot({ path: testInfo.outputPath('photo-verification-time.png'), fullPage: true });
});
