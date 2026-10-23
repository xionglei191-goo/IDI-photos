import { test, expect, type Page, type APIRequestContext } from '@playwright/test';
import fs from 'node:fs/promises';

test.use({ timezoneId: 'Asia/Shanghai' });
const headers = { 'X-IDI-Request': '1' };
const images: string[] = ["iVBORw0KGgoAAAANSUhEUgAAAKAAAAB4CAIAAAD6wG44AAABK0lEQVR4nO3RAQkAIBDAwNf+nTWFCOMuwWDrDGX7dwBvGRxncJzBcQbHGRxncJzBcQbHGRxncJzBcQbHGRxncJzBcQbHGRxncJzBcQbHGRxncJzBcQbHGRxncJzBcQbHGRxncJzBcQbHGRxncJzBcQbHGRxncJzBcQbHGRxncJzBcQbHGRxncJzBcQbHGRxncJzBcQbHGRxncJzBcQbHGRxncJzBcQbHGRxncJzBcQbHGRxncJzBcQbHGRxncJzBcQbHGRxncJzBcQbHGRxncJzBcQbHGRxncJzBcQbHGRxncJzBcQbHGRxncJzBcQbHGRxncJzBcQbHGRxncJzBcQbHGRxncJzBcQbHGRxncJzBcQbHGRxncJzBcQbHGRxncJzBcQbHGRxn8LRdOqYB73oWR1UAAAAASUVORK5CYII=", "iVBORw0KGgoAAAANSUhEUgAAAKAAAAB4CAIAAAD6wG44AAABLElEQVR4nO3RAQkAIBDAwNf+nTWFCOMuwWBr5gxd+3cAbxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ/C0XTioAe+nEsaNAAAAAElFTkSuQmCC", "iVBORw0KGgoAAAANSUhEUgAAAKAAAAB4CAIAAAD6wG44AAABLElEQVR4nO3RAQkAIBDAwNfkRjeFCOMuwWBrzhC2fwfwlsFxBscZHGdwnMFxBscZHGdwnMFxBscZHGdwnMFxBscZHGdwnMFxBscZHGdwnMFxBscZHGdwnMFxBscZHGdwnMFxBscZHGdwnMFxBscZHGdwnMFxBscZHGdwnMFxBscZHGdwnMFxBscZHGdwnMFxBscZHGdwnMFxBscZHGdwnMFxBscZHGdwnMFxBscZHGdwnMFxBscZHGdwnMFxBscZHGdwnMFxBscZHGdwnMFxBscZHGdwnMFxBscZHGdwnMFxBscZHGdwnMFxBscZHGdwnMFxBscZHGdwnMFxBscZHGdwnMFxBscZHGdwnMFxBscZHGdwnMFxBscZHGdwnMFxBscZHGdwnMFxBk/bBVmNAXCxz6rFAAAAAElFTkSuQmCC"];

async function prepare(page: Page, request: APIRequestContext, suffix: string) {
  const auth = await (await request.get('/api/auth/status')).json();
  expect((await request.post(auth.setup_required ? '/api/auth/setup' : '/api/auth/login', { headers,
    data: { username: 'upload-ui-admin', password: 'isolated-upload-ui-password', display_name: '隔离测试管理员' } })).ok()).toBeTruthy();
  await page.context().addCookies((await request.storageState()).cookies);
  const project = await (await request.post('/api/projects', { headers, data: { name: `人保方案验收${suffix}`, insurer: '中国人保' } })).json();
  const value = await (await request.post('/api/cases', { headers, data: { project_id: project.id, title: `墙面裂缝${suffix}`, building: '3A', room: '301', platform_report_code: 'ZLBH' + Date.now() } })).json();
  expect((await request.post(`/api/cases/${value.id}/visits`, { headers, data: { stage: 'survey', visit_date: '2026-10-22' } })).ok()).toBeTruthy();
  for (const [index, name] of ['晚照片.png', '早照片.png', '无水印.png'].entries()) {
    expect((await request.post('/api/import/upload', { headers, multipart: { case_id: value.id, auto_ocr: 'false',
      files: { name, mimeType: 'image/png', buffer: Buffer.from(images[index], 'base64') } } })).ok()).toBeTruthy();
  }
  expect((await request.post('/api/import/upload', { headers, multipart: { case_id: value.id, auto_ocr: 'false',
    files: { name: '现场视频.mp4', mimeType: 'video/mp4', buffer: Buffer.from('isolated-video-fixture') } } })).ok()).toBeTruthy();
  await expect.poll(async () => (await (await request.get(`/api/photos?case_id=${value.id}`)).json()).total).toBe(4);
  const photos = (await (await request.get(`/api/photos?case_id=${value.id}`)).json()).items;
  for (const p of photos) {
    const metadata: Record<string, string> = { photo_group: 'survey_problem' };
    if (p.original_name === '晚照片.png') metadata.watermark_time = '2026-10-23T10:00:00';
    if (p.original_name === '早照片.png') metadata.watermark_time = '2026-10-23T09:00:00';
    expect((await request.patch(`/api/photos/${p.id}`, { headers, data: metadata })).ok()).toBeTruthy();
  }
  return { value, photos };
}

async function open(page: Page, id: string) {
  await page.goto('/');
  await page.locator(`[data-case-id="${id}"] .case-link`).click();
  await page.getByRole('button', { name: /^保司资料/ }).click();
  await page.getByLabel('保司资料要求', { exact: true }).selectOption('picc-record-v1');
  await expect(page.getByRole('region', { name: '人保记录表填写' })).toBeVisible();
}

test('PICC form saves, orders every photo and downloads Word only with immutable versions', async ({ page, request }, testInfo) => {
  const { value, photos } = await prepare(page, request, '完整流程');
  const errors: string[] = [];
  page.on('pageerror', e => errors.push(e.message));
  await open(page, value.id);
  const form = page.getByRole('region', { name: '人保记录表填写' });
  await expect(form).toContainText('全部 3 张');
  await expect(form).toContainText('优先按水印时间排序，没有水印时间时按上传时间排序');
  await expect(form).toContainText('1 个视频请切换到原照片打包方案下载');
  await expect(form.getByLabel('查勘日期', { exact: true })).toHaveValue('2026-10-22');
  await expect(form.getByLabel('查勘日期', { exact: true })).not.toBeEditable();
  await expect(form.getByLabel(/排序时间|照片备注|查勘说明|备注序号/)).toHaveCount(0);
  const saved = await (await request.get(`/api/cases/${value.id}/picc-record`)).json();
  const expected = photos.filter((p: { media_type: string }) => p.media_type === 'image').map((p: { id: string; original_name: string; created_at: string }) => ({ ...p, stamp: p.original_name === '晚照片.png' ? '2026-10-23T10:00:00+08:00' : p.original_name === '早照片.png' ? '2026-10-23T09:00:00+08:00' : p.created_at })).sort((a: { stamp: string }, b: { stamp: string }) => Date.parse(a.stamp) - Date.parse(b.stamp));
  expect(saved.photos.map((p: { id: string }) => p.id)).toEqual(expected.map((p: { id: string }) => p.id));
  for (const [index, p] of saved.photos.entries()) await expect(form.locator('.picc-photo-row').nth(index)).toContainText(p.original_name);
  const uploaded = photos.find((p: { original_name: string }) => p.original_name === '无水印.png');
  expect((await (await request.get(`/api/photos/${uploaded.id}`)).json()).watermark_time).toBeNull();
  expect(saved.photos.find((p: { id: string }) => p.id === uploaded.id).time_source).toBe('upload');
  expect(saved.photos.every((p: { remarks: string }) => p.remarks === '')).toBeTruthy();
  expect(saved.blockers).toEqual([]);
  await form.getByRole('button', { name: '生成记录表', exact: true }).click();
  const list = page.getByRole('region', { name: '本案资料包', exact: true });
  const link = list.getByRole('link', { name: '下载记录表 v1', exact: true });
  await expect(link).toBeVisible();
  await expect(list.getByRole('link', { name: /完整包|下载资料包/ })).toHaveCount(0);
  const event = page.waitForEvent('download');
  await link.click();
  const download = await event;
  expect(download.suggestedFilename()).toContain(value.platform_report_code + '-v1.docx');
  await download.saveAs(testInfo.outputPath('picc-record.docx'));
  expect((await fs.readFile(testInfo.outputPath('picc-record.docx'))).subarray(0, 2).toString()).toBe('PK');
  const first = (await (await request.get(`/api/packages?case_id=${value.id}`)).json())[0];
  const original = await (await request.get(first.download_url)).body();
  expect((await request.get(first.download_url)).headers()['content-type']).toContain('wordprocessingml.document');
  const detail = await (await request.get('/api/packages/' + first.id)).json();
  expect(detail.manifest.photos).toHaveLength(3);
  expect(detail.manifest.photos.every((p: { media_type: string }) => p.media_type === 'image')).toBeTruthy();
  await form.getByRole('button', { name: '生成记录表', exact: true }).click();
  await expect(list.getByRole('status')).toContainText('资料未变化，已复用资料包 v1');
  await form.getByLabel('查勘内容', { exact: true }).fill('墙面及天花裂缝核对');
  await form.getByRole('button', { name: '生成记录表', exact: true }).click();
  await expect(list.getByRole('link', { name: '下载记录表 v2', exact: true })).toBeVisible();
  expect(await (await request.get(first.download_url)).body()).toEqual(original);
  await form.getByLabel('楼栋房号', { exact: true }).fill('3A-302');
  await page.getByRole('button', { name: '办理记录', exact: true }).click();
  await expect(page.getByRole('heading', { name: '办理进度与凭据', exact: true })).toBeVisible();
  await page.getByRole('button', { name: /^保司资料/ }).click();
  await expect(form.getByLabel('楼栋房号', { exact: true })).toHaveValue('3A-302');
  for (const width of [1366, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await form.scrollIntoViewIfNeeded();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
    await page.screenshot({ path: testInfo.outputPath(`picc-form-${width}.png`), fullPage: true, animations: 'disabled' });
  }
  expect(errors).toEqual([]);
});

test('PICC selection dialog expands a partial selection to the whole case and saves on close', async ({ page, request }) => {
  const { value } = await prepare(page, request, '选材入口');
  await page.goto('/');
  await page.locator(`[data-case-id="${value.id}"] .case-link`).click();
  await page.getByRole('button', { name: '本案照片', exact: true }).click();
  await expect(page.locator('.photo-table tbody tr')).toHaveCount(4);
  await page.locator('.photo-table tbody tr').first().getByRole('checkbox').check();
  await page.getByRole('button', { name: '提取所选', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('资料要求', { exact: true }).selectOption('picc-record-v1');
  await expect(dialog).toContainText('本案去重且未移除的 3 张照片');
  await expect(dialog.getByLabel('查勘日期', { exact: true })).toHaveValue('2026-10-22');
  await dialog.getByLabel('查勘内容', { exact: true }).fill('已核对查勘内容');
  await dialog.getByRole('button', { name: '关闭', exact: true }).last().click();
  await expect(dialog).toHaveCount(0);
  const saved = await (await request.get(`/api/cases/${value.id}/picc-record`)).json();
  expect(saved.blockers).toEqual([]);
  expect(saved.fields.survey_date).toBe('2026-10-22');
  expect(saved.fields.survey_content).toBe('已核对查勘内容');
  await page.getByRole('button', { name: '提取所选', exact: true }).click();
  await dialog.getByLabel('资料要求', { exact: true }).selectOption('picc-record-v1');
  await expect(dialog.getByLabel('查勘日期', { exact: true })).toHaveValue('2026-10-22');
  await dialog.getByRole('button', { name: '生成记录表', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.locator('.reporting-table tbody tr').filter({ hasText: value.title }).getByRole('link', { name: 'Word', exact: true })).toBeVisible();
});

test('PICC stale drafts explain the conflict and can reload the saved version', async ({ page, request }) => {
  const { value } = await prepare(page, request, '并发填写');
  await open(page, value.id);
  const form = page.getByRole('region', { name: '人保记录表填写' });
  await form.getByLabel('查勘内容', { exact: true }).fill('本页尚未保存内容');
  const url = `/api/cases/${value.id}/picc-record`;
  const another = await (await request.get(url)).json();
  another.fields.survey_content = '其他页面已保存内容';
  expect((await request.put(url, { headers, data: another })).ok()).toBeTruthy();
  await form.getByRole('button', { name: '保存填写', exact: true }).click();
  await expect(form.getByRole('alert')).toContainText('其他页面更新');
  await expect(form.getByLabel('查勘内容', { exact: true })).toHaveValue('本页尚未保存内容');
  await form.getByRole('button', { name: '重新载入（放弃本页未保存填写）', exact: true }).click();
  await expect(form.getByLabel('查勘内容', { exact: true })).toHaveValue('其他页面已保存内容');
  await expect(form.getByRole('button', { name: '保存填写', exact: true })).toBeDisabled();
});

test('PICC duplicates collapse and removing or restoring a record photo preserves archive originals', async ({ page, request }, testInfo) => {
  const { value, photos } = await prepare(page, request, '去重移除');
  const target = photos.find((p: { original_name: string }) => p.original_name === '早照片.png');
  const changed = await (await request.patch(`/api/photos/${target.id}`, { headers, data: { photo_group: 'survey_checkin', role: 'building_checkin' } })).json();
  expect((await request.post(`/api/photos/${target.id}/reuse-checkin`, { headers, data: { case_id: value.id, revision: changed.revision } })).ok()).toBeTruthy();
  await open(page, value.id);
  const form = page.getByRole('region', { name: '人保记录表填写' });
  await expect(form).toContainText('全部 3 张');
  await expect(form).toContainText('已合并 1 张内容相同的重复照片');
  await form.getByRole('button', { name: '从记录表移除 早照片.png', exact: true }).click();
  await expect(form).toContainText('全部 2 张');
  await expect(form).toContainText('已从本表移除 1 张');
  await page.reload();
  await page.locator(`[data-case-id="${value.id}"] .case-link`).click();
  await page.getByRole('button', { name: /^保司资料/ }).click();
  await page.getByLabel('保司资料要求', { exact: true }).selectOption('picc-record-v1');
  await expect(form).toContainText('全部 2 张');
  await form.getByRole('button', { name: '生成记录表', exact: true }).click();
  await expect(page.getByRole('link', { name: '下载记录表 v1', exact: true })).toBeVisible();
  const pack = (await (await request.get(`/api/packages?case_id=${value.id}`)).json())[0];
  const manifest = (await (await request.get(`/api/packages/${pack.id}`)).json()).manifest;
  expect(manifest.photos).toHaveLength(2);
  expect(manifest.photos.map((p: { blob_hash: string }) => p.blob_hash)).not.toContain(target.blob_hash);
  expect((await (await request.get(`/api/photos?case_id=${value.id}`)).json()).total).toBe(5);
  expect((await request.get(`/api/photos/${target.id}/original`)).ok()).toBeTruthy();
  await form.locator('summary').filter({ hasText: '已从本表移除' }).click();
  for (const width of [1366, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await form.scrollIntoViewIfNeeded();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
    await page.screenshot({ path: testInfo.outputPath(`picc-removal-${width}.png`), fullPage: true, animations: 'disabled' });
  }
  await form.getByRole('button', { name: '恢复到记录表 早照片.png', exact: true }).click();
  await expect(form).toContainText('全部 3 张');
  await expect(form.getByRole('button', { name: '从记录表移除 早照片.png', exact: true })).toHaveCount(1);
});
