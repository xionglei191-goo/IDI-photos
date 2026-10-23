import { test, expect, type Page, type APIRequestContext } from '@playwright/test';
import fs from 'node:fs/promises';

test.use({ timezoneId: 'Asia/Shanghai' });
const headers = { 'X-IDI-Request': '1' };
async function prepare(page: Page, request: APIRequestContext) {
  const auth = await (await request.get('/api/auth/status')).json();
  const login = await request.post(auth.setup_required ? '/api/auth/setup' : '/api/auth/login', {
    headers, data: { username: 'upload-ui-admin', password: 'isolated-upload-ui-password', display_name: '隔离测试管理员' },
  });
  expect(login.ok()).toBeTruthy();
  await page.context().addCookies((await request.storageState()).cookies);
}
async function seed(request: APIRequestContext, suffix: string) {
  const project = await (await request.post('/api/projects', { headers, data: { name: `资料包测试${suffix}` } })).json();
  const value = await (await request.post('/api/cases', { headers, data: { project_id: project.id, title: `客厅墙面渗漏${suffix}`, building: '1', room: '1201', platform_report_code: 'ZLBH' + Date.now() } })).json();
  expect(value.id).toBeTruthy();
  return value;
}
async function openInsurer(page: Page, id: string) {
  await page.goto('/');
  await page.locator(`[data-case-id="${id}"] .case-link`).click();
  await page.getByRole('button', { name: /^保司资料/ }).click();
  await expect(page.getByRole('region', { name: '本案资料包', exact: true })).toBeVisible();
}

test('package reuse and deletion work in both case and reporting lists', async ({ page, request }, testInfo) => {
  await prepare(page, request);
  const value = await seed(request, '实包');
  const source = await fs.readFile(new URL('./workbench.spec.ts', import.meta.url), 'utf8');
  const png = Buffer.from(source.match(/Buffer.from\('([^']+)', 'base64'\)/)![1], 'base64');
  const upload = await request.post('/api/import/upload', { headers, multipart: {
    case_id: value.id, auto_ocr: 'false', files: { name: '隔离测试照片.png', mimeType: 'image/png', buffer: png },
  } });
  expect(upload.ok()).toBeTruthy();
  await expect.poll(async () => (await (await request.get(`/api/photos?case_id=${value.id}`)).json()).total).toBe(1);
  const photo = (await (await request.get(`/api/photos?case_id=${value.id}`)).json()).items[0];
  expect((await request.patch(`/api/photos/${photo.id}`, { headers, data: { photo_group: 'survey_problem' } })).ok()).toBeTruthy();
  for (const [name, mimeType, buffer] of [
    ['施工视频.mp4', 'video/mp4', Buffer.from('isolated-video-fixture')],
    ['验收资料.pdf', 'application/pdf', Buffer.from('%PDF-1.4\nfixture\n%%EOF')],
  ] as const) {
    expect((await request.post('/api/import/upload', { headers, multipart: {
      case_id: value.id, auto_ocr: 'false', files: { name, mimeType, buffer },
    } })).ok()).toBeTruthy();
  }
  await expect.poll(async () => (await (await request.get(`/api/photos?case_id=${value.id}`)).json()).total).toBe(3);
  const video = (await (await request.get(`/api/photos?case_id=${value.id}`)).json()).items.find((item: { media_type: string }) => item.media_type === 'video');
  expect((await request.patch(`/api/photos/${video.id}`, { headers, data: { photo_group: 'construction' } })).ok()).toBeTruthy();
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await openInsurer(page, value.id);
  await page.getByRole('button', { name: '生成保司资料包', exact: true }).click();
  const panel = page.getByRole('region', { name: '本案资料包', exact: true });
  const link = panel.getByRole('link', { name: '下载资料包 v1', exact: true });
  await expect(link).toBeVisible();
  const href = await link.getAttribute('href');
  const packageRow = (await (await request.get(`/api/packages?case_id=${value.id}`)).json())[0];
  const packageData = (await (await request.get(`/api/packages/${packageRow.id}`)).json()).manifest;
  expect(packageData.photos.map((item: { media_type: string }) => item.media_type).sort()).toEqual(['image', 'video']);
  expect(packageData.photos.find((item: { media_type: string }) => item.media_type === 'video').package_path).toContain('/施工照片/施工视频.mp4');
  const downloadEvent = page.waitForEvent('download');
  await link.click();
  const download = await downloadEvent;
  const target = testInfo.outputPath('insurer-package.zip');
  await download.saveAs(target);
  expect((await fs.readFile(target)).subarray(0, 2).toString()).toBe('PK');
  expect(download.suggestedFilename()).toContain('-v1.zip');
  expect(download.suggestedFilename()).toContain(`-1-1201-${value.title}-${value.platform_report_code}-v1.zip`);
  await openInsurer(page, value.id);
  await expect(link).toHaveAttribute('href', href!);
  await page.setViewportSize({ width: 1366, height: 900 });
  await panel.scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath('case-download.png'), fullPage: true, animations: 'disabled' });
  for (const width of [1024, 390]) {
    await page.setViewportSize({ width, height: 900 });
    const box = await panel.boundingBox();
    expect(box!.x).toBeGreaterThanOrEqual(0);
    expect(box!.x + box!.width).toBeLessThanOrEqual(width + 1);
    await expect(link).toBeVisible();
    await link.scrollIntoViewIfNeeded();
    expect(await link.evaluate(el => { const r = el.getBoundingClientRect(); return el.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2)); })).toBeTruthy();
  }
  await page.setViewportSize({ width: 1366, height: 900 });
  await page.getByRole('button', { name: '生成保司资料包', exact: true }).click();
  await expect(panel.getByRole('status')).toContainText('资料未变化，已复用资料包 v1');
  expect((await (await request.get(`/api/packages?case_id=${value.id}`)).json()).length).toBe(1);
  await expect(link).toHaveAttribute('href', href!);
  await page.screenshot({ path: testInfo.outputPath('case-reused.png'), fullPage: true, animations: 'disabled' });
  await panel.getByRole('button', { name: '删除资料包 v1', exact: true }).click();
  let dialog = page.getByRole('dialog', { name: '删除资料包 · v1', exact: true });
  await expect(dialog.getByRole('region', { name: '即将删除的资料包' })).toBeVisible();
  await expect(dialog.getByText(value.title, { exact: true })).toBeVisible();
  await dialog.getByRole('button', { name: '取消', exact: true }).click();
  await expect(link).toBeVisible();
  await panel.getByRole('button', { name: '删除资料包 v1', exact: true }).click();
  await dialog.getByRole('button', { name: '确认删除资料包', exact: true }).click();
  await expect(link).toHaveCount(0);
  expect((await request.get(href!)).status()).toBe(404);
  expect((await (await request.get(`/api/photos?case_id=${value.id}`)).json()).total).toBe(3);
  await openInsurer(page, value.id);
  await expect(panel.getByRole('link')).toHaveCount(0);
  await page.getByRole('button', { name: '生成保司资料包', exact: true }).click();
  await expect(panel.getByRole('link', { name: '下载资料包 v2', exact: true })).toBeVisible();
  const second = (await (await request.get(`/api/packages?case_id=${value.id}`)).json())[0];
  expect((await request.post(`/api/packages/${second.id}/events`, { headers, data: { state: 'accepted', note: '隔离测试收件' } })).ok()).toBeTruthy();
  await page.locator('.utility-menu summary').click();
  await page.getByRole('button', { name: '报送记录', exact: true }).click();
  const row = page.locator('.reporting-table tbody tr').filter({ hasText: value.title });
  await expect(row).toContainText('v2');
  await row.getByRole('button', { name: '删除资料包 v2', exact: true }).click();
  dialog = page.getByRole('dialog', { name: '删除资料包 · v2', exact: true });
  await expect(dialog).toContainText('已有 1 条提交或回执记录');
  await page.setViewportSize({ width: 390, height: 900 });
  await expect(dialog.getByRole('button', { name: '确认删除资料包', exact: true })).toBeInViewport();
  await page.screenshot({ path: testInfo.outputPath('reporting-delete-mobile.png'), fullPage: true, animations: 'disabled' });
  // A newly arrived receipt invalidates the already displayed confirmation.
  expect((await request.post(`/api/packages/${second.id}/events`, { headers, data: { state: 'returned', note: '隔离测试补件' } })).ok()).toBeTruthy();
  await dialog.getByRole('button', { name: '确认删除资料包', exact: true }).click();
  await expect(dialog.getByRole('alert')).toContainText('状态或回执已变化');
  await expect(dialog.getByRole('button', { name: '确认删除资料包', exact: true })).toBeDisabled();
  await dialog.getByRole('button', { name: '重新核对资料包', exact: true }).click();
  await expect(dialog).toContainText('已有 2 条提交或回执记录');
  await dialog.getByRole('button', { name: '确认删除资料包', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect(row).toHaveCount(0);
  expect((await request.get(second.download_url)).status()).toBe(404);
  await page.reload();
  expect((await (await request.get(`/api/packages?case_id=${value.id}`)).json()).length).toBe(0);
  expect((await (await request.get(`/api/photos?case_id=${value.id}`)).json()).total).toBe(3);
  expect(errors).toEqual([]);
});

test('pending and failed generation are visible without exposing a download', async ({ page, request }) => {
  await prepare(page, request);
  const value = await seed(request, '进度');
  let state = 'queued';
  await page.route('**/api/jobs', route => route.fulfill({ json: [
    { id: 'case-export', kind: 'export', state, payload: { groups: [{ case: { id: value.id } }] }, error: state === 'failed' ? '测试原件不可读取' : null },
    { id: 'other-export', kind: 'export', state: 'queued', payload: { groups: [{ case: { id: 'other-case' } }] } },
  ] }));
  await page.route(`**/api/packages?case_id=${value.id}`, route => route.fulfill({ json: state === 'failed' ? [
    { id: 'failed-export', case_id: value.id, title: '保司过程资料', version: 1, state: 'failed', photo_count: 1, missing_count: 0, created_at: '2026-10-23T00:00:00Z', download_url: null },
  ] : [] }));
  await openInsurer(page, value.id);
  const panel = page.getByRole('region', { name: '本案资料包', exact: true });
  await expect(panel.getByRole('status')).toContainText('已排队');
  await expect(page.getByRole('button', { name: '资料包生成中…', exact: true })).toBeDisabled();
  state = 'running';
  await expect(panel.getByRole('status')).toContainText('正在生成');
  state = 'failed';
  await expect(panel.getByRole('alert')).toContainText('测试原件不可读取');
  await expect(panel.getByRole('link')).toHaveCount(0);
  await expect(page.getByRole('button', { name: '生成保司资料包', exact: true })).toBeEnabled();
});

test('reporting combines keyword project state and local date filters with pagination', async ({ page, request }, testInfo) => {
  await prepare(page, request);
  const records = Array.from({ length: 25 }, (_, i) => ({
    id: `p${i}`, case_id: i < 2 ? 'a' : 'b', project_id: i < 2 ? 'pa' : 'pb', project_name: i < 2 ? '甲苑' : '乙苑',
    case_title: i < 2 ? '主卧墙面渗漏' : '卫生间维修', title: '保司过程资料', short_code: i < 2 ? 'JY-1-1201-01' : 'YY-2-701-01',
    report_number: i < 2 ? '0000123456789' : '00009999', platform_report_code: i < 2 ? 'REPAIR-10001' : 'REPAIR-20002',
    building: i < 2 ? '1' : '2', room: i < 2 ? '1201' : '701', version: i + 1, parent_id: null,
    state: i === 1 ? 'accepted' : 'ready', photo_count: 16, missing_count: 0,
    created_at: i === 0 ? '2026-10-22T16:05:00Z' : i === 1 ? '2026-10-21T16:00:00Z' : '2026-10-20T08:00:00Z',
    download_url: `/api/packages/p${i}/download`, zip_hash: '',
  }));
  await page.route('**/api/packages', route => route.fulfill({ json: records }));
  await page.goto('/');
  await page.locator('.utility-menu summary').click();
  await page.getByRole('button', { name: '报送记录', exact: true }).click();
  const rows = page.locator('.reporting-table tbody tr');
  const filters = page.getByRole('region', { name: '报送记录筛选', exact: true });
  await expect(rows).toHaveCount(20);
  await page.getByRole('button', { name: '下一页', exact: true }).click();
  await expect(rows).toHaveCount(5);
  await filters.getByLabel('查找资料', { exact: true }).fill('甲苑 1201');
  await expect(rows).toHaveCount(2);
  await filters.getByLabel('查找资料', { exact: true }).fill('0000123456789');
  await expect(rows).toHaveCount(2);
  await filters.getByLabel('所属项目', { exact: true }).selectOption('pa');
  await filters.getByLabel('报送状态', { exact: true }).selectOption('ready');
  await filters.getByLabel('生成日期从', { exact: true }).fill('2026-10-23');
  await filters.getByLabel('生成日期至', { exact: true }).fill('2026-10-23');
  await expect(rows).toHaveCount(1);
  await expect(rows).toContainText('v1');
  await filters.getByLabel('生成日期至', { exact: true }).fill('2026-10-24');
  await filters.getByLabel('生成日期从', { exact: true }).fill('2026-10-24');
  await expect(page.getByText('没有符合当前条件的报送记录，请调整关键词或筛选条件。', { exact: true })).toBeVisible();
  await filters.getByRole('button', { name: '清空筛选', exact: true }).click();
  await expect(rows).toHaveCount(20);
  await filters.getByLabel('生成日期从', { exact: true }).fill('2026-10-22');
  await filters.getByLabel('生成日期至', { exact: true }).fill('2026-10-22');
  await expect(rows).toHaveCount(1);
  await expect(rows).toContainText('已接收');
  await filters.getByLabel('生成日期从', { exact: true }).fill('2026-10-24');
  await expect(filters.getByRole('alert')).toContainText('开始日期不能晚于结束日期');
  await filters.getByRole('button', { name: '清空筛选', exact: true }).click();
  await expect(rows).toHaveCount(20);
  for (const width of [1366, 1024, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await page.screenshot({ path: testInfo.outputPath(`reporting-${width}.png`), fullPage: true, animations: 'disabled' });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
    expect(await filters.evaluate(el => el.scrollWidth <= el.clientWidth + 1)).toBeTruthy();
  }
});
