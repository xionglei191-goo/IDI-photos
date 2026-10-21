import { test, expect, type Page, type APIRequestContext } from '@playwright/test';

const headers = { 'X-IDI-Request': '1' };
const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=', 'base64');

async function prepare(page: Page, request: APIRequestContext, failFirst = false) {
  const auth = await (await request.get('/api/auth/status')).json();
  const login = await request.post(auth.setup_required ? '/api/auth/setup' : '/api/auth/login', {
    headers, data: { username: 'readback-admin', password: 'isolated-readback-password', display_name: '回读测试' },
  });
  expect(login.ok()).toBeTruthy();
  await page.context().addCookies((await request.storageState()).cookies);
  const project = await (await request.post('/api/projects', { headers, data: { name: `单案回读-${Date.now()}` } })).json();
  const item = await (await request.post('/api/cases', { headers, data: { project_id: project.id, title: '官网照片删除验证', building: 'C4', room: '1101' } })).json();
  await request.post(`/api/workflow/cases/${item.id}/result`, { headers, data: { action: 'accepted', revision: item.revision, event_at: '2026-10-21 10:00', note: '隔离测试' } });
  const detail = await (await request.get(`/api/workflow/cases/${item.id}`)).json();
  detail.case.workflow_source = 'platform';
  detail.official_read_at = '2026-10-21T02:00:00Z';
  const plan = await (await request.get(`/api/platform/cases/${item.id}/stages/survey`)).json();
  plan.link.report_receiving_id = 'test-only-official-case';
  plan.link.verified_at = detail.official_read_at;
  const field = plan.file_fields.find((f: { key: string }) => f.key === 'fileInfoList');
  const keep = { id: 'keep', fileUrl: '/profile/keep.png', oldFileName: '仍在官网.png' };
  const removed = { id: 'deleted', fileUrl: '/profile/deleted.png', oldFileName: '官网删除的最后一张.png', removed_at: '2026-10-21T03:00:00Z' };
  field.existing = [keep, removed];
  field.selected = [];
  let calls = 0;
  const writes: string[] = [], errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', req => { if (req.url().includes('/api/') && !['GET', 'HEAD'].includes(req.method())) writes.push(new URL(req.url()).pathname); });
  await page.route(`**/api/workflow/cases/${item.id}`, route => route.fulfill({ json: detail }));
  await page.route(`**/api/workflow/cases/${item.id}/refresh`, async route => {
    calls++;
    if (failFirst && calls === 1) return route.fulfill({ status: 502, json: { detail: '测试：官网暂时离线' } });
    field.existing = [keep];
    field.removed = [removed];
    detail.official_read_at = removed.removed_at;
    plan.link.verified_at = removed.removed_at;
    return route.fulfill({ json: detail });
  });
  await page.route(`**/api/platform/cases/${item.id}/stages/survey`, route => route.fulfill({ json: plan }));
  await page.route(`**/api/platform/cases/${item.id}/stages/survey/preview`, route => route.fulfill({ json: plan }));
  await page.route('**/files/**', route => route.fulfill({ contentType: 'image/png', body: png }));
  await page.goto('/');
  await page.getByRole('navigation', { name: '工作看板' }).getByRole('button', { name: '查勘维修', exact: true }).click();
  const open = async () => page.locator(`[data-case-id="${item.id}"] .case-link`).click();
  await open();
  return { calls: () => calls, writes, errors, item, open, field: page.locator('[data-field="fileInfoList"]') };
}

test('opening and reopening a case reads the website once and removes stale photos without tab polling', async ({ page, request }, testInfo) => {
  const state = await prepare(page, request);
  await expect(state.field.locator('.attachment-card[data-source=official]')).toHaveCount(1);
  await expect(state.field.getByText('官网已删除 1 项', { exact: true })).toBeVisible();
  await expect(state.field.locator('.platform-removed-files')).toContainText('官网删除的最后一张.png');
  expect(state.calls()).toBe(1);
  await page.getByRole('navigation', { name: '案件资料' }).getByRole('button', { name: /^保司资料/ }).click();
  await page.getByRole('button', { name: '广州平台', exact: true }).click();
  await expect(state.field.getByText('官网已删除 1 项', { exact: true })).toBeVisible();
  expect(state.calls()).toBe(1);
  await page.getByRole('button', { name: '返回看板', exact: true }).click();
  await state.open();
  await expect(state.field.getByText('官网已删除 1 项', { exact: true })).toBeVisible();
  expect(state.calls()).toBe(2);
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    await state.field.locator('.platform-removed-files').scrollIntoViewIfNeeded();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`removed-${width}.png`), fullPage: true });
  }
  expect(state.calls()).toBe(2);
  expect(state.writes).toEqual(Array(2).fill(`/api/workflow/cases/${state.item.id}/refresh`));
  expect(state.errors).toEqual([]);
});

test('failed website read keeps cache with an explicit warning and supports retry', async ({ page, request }) => {
  const state = await prepare(page, request, true);
  await expect(page.getByRole('alert')).toContainText('官网刷新未完成，当前显示上次缓存');
  await expect(state.field.locator('.attachment-card[data-source=official]')).toHaveCount(2);
  await expect(state.field.locator('.platform-removed-files')).toHaveCount(0);
  await page.getByRole('button', { name: '重试官网刷新', exact: true }).click();
  await expect(page.getByText('官网刷新未完成，当前显示上次缓存', { exact: false })).toHaveCount(0);
  await expect(state.field.locator('.attachment-card[data-source=official]')).toHaveCount(1);
  await expect(state.field.getByText('官网已删除 1 项', { exact: true })).toBeVisible();
  expect(state.calls()).toBe(2);
  expect(state.writes).toEqual(Array(2).fill(`/api/workflow/cases/${state.item.id}/refresh`));
  expect(state.errors).toEqual([]);
});
