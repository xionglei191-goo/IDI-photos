import { test, expect, type APIRequestContext } from '@playwright/test';

test.beforeEach(async ({ page, request }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  const auth = await (await request.get('/api/auth/status')).json();
  expect((await request.post(auth.setup_required ? '/api/auth/setup' : '/api/auth/login', {
    headers: { 'X-IDI-Request': '1' }, data: { username: 'upload-ui-admin', password: 'isolated-upload-ui-password', display_name: '隔离测试管理员' },
  })).ok()).toBeTruthy();
  await page.context().addCookies((await request.storageState()).cookies);
});

async function casesFor(request: APIRequestContext, name: string) {
  const headers = { 'X-IDI-Request': '1' };
  const project = await (await request.post('/api/projects', { headers, data: { name } })).json();
  const cases = [];
  for (let index = 0; index < 5; index++) cases.push(await (await request.post('/api/cases', {
    headers, data: { project_id: project.id, title: `门套与墙面修复 ${index + 1}`, building: '1', room: String(1001 + index) },
  })).json());
  return cases.map((item, index) => ({
    case_id: item.id, project_id: project.id, enabled: true, state: 'awaiting_confirmation', stage: 'repair',
    short_code: item.short_code, platform_report_code: `ZLBH20261030090000000${index + 1}`, project_name: name,
    title: item.title, review_id: `layout-review-${index}`, save_review_id: `layout-save-${index}`,
    detail: { pending_checkins: [{ field: 'houseFileInfoList', label: '入户打卡照片', message: '缺少入户打卡照片' }],
      files: [{ label: '楼栋打卡照片', count: 1 }, { label: '入户打卡照片', count: 0 }, { label: '修复后照片', count: 2 }] },
    notification: { state: 'sent', name: '项目负责人', chat_name: '界面验收群' },
  }));
}

test('proposal summary stays compact as the queue grows; drawer filters and scrolls on desktop and mobile', async ({ page, request }, testInfo) => {
  const items = await casesFor(request, '待办布局验收项目');
  const status = { policy: { enabled: true }, connected: true, write_enabled: true, counts: { awaiting_confirmation: 5 }, items };
  const writes: string[] = [], errors: string[] = [];
  page.on('request', req => { if (['POST', 'PUT', 'DELETE'].includes(req.method())) writes.push(req.url()); });
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/api/workflow/uploads', route => route.fulfill({ json: status }));
  await page.goto('/');
  const strip = page.getByRole('region', { name: '拟提交事项', exact: true });
  const launcher = strip.getByRole('button', { name: '查看待办', exact: true });
  await expect(launcher).toBeVisible();
  await expect(strip.getByText('待核对 0 · 待补打卡 5')).toBeVisible();
  expect((await strip.boundingBox())!.height).toBeLessThanOrEqual(80);
  const heading = page.getByRole('heading', { name: '理赔受理', exact: true });
  expect((await heading.boundingBox())!.y).toBeLessThan(300);
  await expect(page.getByRole('button', { name: '核对拟提交信息', exact: true })).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath('proposal-summary-desktop.png'), fullPage: true });
  await launcher.click();
  const drawer = page.getByRole('dialog', { name: '拟提交待办', exact: true });
  await expect(drawer.getByRole('article')).toHaveCount(5);
  const first = drawer.getByRole('article').first();
  await expect(first.getByRole('button', { name: items[0].platform_report_code, exact: true })).toBeVisible();
  await expect(first.locator('.proposal-item-project')).toHaveText('待办布局验收项目 · 1栋 1001');
  await expect(first.getByRole('button', { name: items[0].short_code, exact: true })).toHaveCount(0);
  expect((await drawer.boundingBox())!.width).toBeLessThan(700);
  await page.screenshot({ path: testInfo.outputPath('proposal-drawer-desktop.png'), fullPage: true });
  await page.keyboard.press('Escape');
  await expect(drawer).toHaveCount(0);
  await expect(launcher).toBeFocused();
  const originalHeight = (await strip.boundingBox())!.height;
  status.items = Array.from({ length: 25 }, (_, index) => ({ ...items[index % 5], case_id: index < 5 ? items[index].case_id : `${items[index % 5].case_id}-${index}`,
    short_code: `LAYOUT-${1000 + index}`, platform_report_code: `ZLBH2026103009${String(index).padStart(8, '0')}`, detail: { ...items[index % 5].detail, pending_checkins: index % 5 === 0 ? [] : items[index % 5].detail.pending_checkins } }));
  status.counts.awaiting_confirmation = 25;
  await expect(strip.getByText('待核对 5 · 待补打卡 20')).toBeVisible();
  expect((await strip.boundingBox())!.height).toBe(originalHeight);
  await launcher.click();
  await expect(drawer.getByRole('article')).toHaveCount(25);
  await drawer.getByRole('button', { name: '待核对 5', exact: true }).click();
  await expect(drawer.getByRole('article')).toHaveCount(5);
  await drawer.getByRole('button', { name: '全部 25', exact: true }).click();
  await drawer.getByRole('textbox', { name: '搜索拟提交事项' }).fill('LAYOUT-1024');
  await expect(drawer.getByRole('article')).toHaveCount(1);
  await expect(drawer.getByRole('article').getByRole('button', { name: status.items[24].platform_report_code, exact: true })).toBeVisible();
  await drawer.getByRole('textbox', { name: '搜索拟提交事项' }).fill('1栋 1003');
  await expect(drawer.getByRole('article')).toHaveCount(1);
  await expect(drawer.locator('.proposal-item-project')).toHaveText('待办布局验收项目 · 1栋 1003');
  await drawer.getByRole('textbox', { name: '搜索拟提交事项' }).fill('不存在的案件');
  await expect(drawer.getByText('没有符合当前筛选的事项。')).toBeVisible();
  await drawer.getByRole('textbox', { name: '搜索拟提交事项' }).clear();
  await drawer.locator('.proposal-queue-list').evaluate(element => { element.scrollTop = element.scrollHeight; });
  await expect(drawer.getByRole('article').last()).toBeInViewport();
  await expect(drawer.getByRole('button', { name: '关闭', exact: true })).toBeInViewport();
  await page.keyboard.press('Escape');
  for (const width of [390, 320]) {
    await page.setViewportSize({ width, height: 844 });
    expect((await strip.boundingBox())!.height).toBeLessThanOrEqual(80);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await launcher.click();
    await expect(drawer).toBeVisible();
    expect((await drawer.boundingBox())!.width).toBeLessThanOrEqual(width);
    expect(await drawer.evaluate(element => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`proposal-drawer-${width}.png`), fullPage: true });
    await drawer.getByRole('button', { name: '关闭', exact: true }).click();
  }
  expect(writes).toEqual([]);
  expect(errors).toEqual([]);
});

test('current case is first and emptied queue remains open until explicitly closed', async ({ page, request }) => {
  const items = await casesFor(request, '本案待办验收项目');
  const status = { policy: { enabled: true }, connected: true, write_enabled: true, counts: { awaiting_confirmation: 5 }, items };
  await page.route('**/api/workflow/uploads', route => route.fulfill({ json: status }));
  await page.goto('/');
  const strip = page.getByRole('region', { name: '拟提交事项', exact: true });
  await strip.getByRole('button', { name: '查看待办' }).click();
  const drawer = page.getByRole('dialog', { name: '拟提交待办', exact: true });
  await drawer.getByRole('button', { name: items[4].platform_report_code, exact: true }).click();
  await expect(drawer).toHaveCount(0);
  await expect(strip.getByRole('button', { name: '核对本案', exact: true })).toBeVisible();
  await strip.getByRole('button', { name: '查看待办' }).click();
  await expect(drawer.getByRole('article').first()).toContainText(items[4].platform_report_code);
  await expect(drawer.getByRole('article').first()).toContainText('当前案件');
  status.items = [];
  await expect(drawer.getByText('暂无待核对事项。')).toBeVisible();
  await drawer.getByRole('button', { name: '关闭', exact: true }).click();
  await expect(strip).toHaveCount(0);
});
