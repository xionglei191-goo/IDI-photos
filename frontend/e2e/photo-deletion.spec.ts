import { test, expect, type APIRequestContext } from '@playwright/test';

const headers = { 'X-IDI-Request': '1' };
const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAHgAAABkCAIAAADCEmNlAAAA80lEQVR4nO3SQQ3AMAADsazIC30oei+bQKRTvu2O906wgdAdj44IHRE6InRE6IjQEaEjQkeEjggdEToidEToiNARoSNCR4SOCB0ROiJ0ROiI0BGhI0JHhI4IHRE6InRE6IjQEaEjQkeEjggdEToidEToiNARoSNCR4SOCB0ROiJ0ROiI0BGhI0JHhI4IHRE6InRE6IjQEaEjQkeEjggdEToidEToiNARoSNCR4SOCB0ROiJ0ROiI0BGhI0JHhI4IHRE6InRE6IjQEaEjQkeEjggdEToidEToiNARoSNCR4SOCB0ROiJ0ROiI0BGhI0JHhF7jByp2AUhQ6fjGAAAAAElFTkSuQmCC', 'base64');

async function upload(request: APIRequestContext, prefix: string, count: number) {
  const form = new FormData();
  form.append('auto_ocr', 'false');
  for (let i = 1; i <= count; i++) form.append('files', new Blob([png, Buffer.from(prefix + i)], { type: 'image/png' }), `${prefix}-${i}.png`);
  const response = await request.post('/api/import/upload', { headers, multipart: form });
  expect(response.ok()).toBe(true);
  const job = (await response.json()).job.id;
  await expect.poll(async () => (await (await request.get(`/api/jobs/${job}`)).json()).state).toBe('completed');
  return (await (await request.get('/api/photos?query=' + encodeURIComponent(prefix))).json()).items as { id: string; original_name: string }[];
}

test('single and bulk deletion require review and preserve unselected photos', async ({ page, request }, testInfo) => {
  const errors: string[] = [];
  page.on('pageerror', e => errors.push(e.message));
  const photos = await upload(request, '删除交互验证', 4);
  await page.goto('/');
  await page.getByRole('button', { name: '去确认', exact: true }).click();
  await page.getByRole('textbox', { name: '搜索照片' }).fill('删除交互验证');
  await expect(page.locator('[data-photo-id]')).toHaveCount(4);
  await expect(page.getByRole('button', { name: '删除所选', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: '删除删除交互验证-1.png', exact: true }).click();
  let dialog = page.getByRole('dialog');
  await expect(dialog.getByRole('list', { name: '即将删除的照片' })).toContainText('删除交互验证-1.png');
  await dialog.getByRole('button', { name: '取消', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  expect((await request.get(`/api/photos/${photos.find(p => p.original_name.endsWith('-1.png'))!.id}`)).ok()).toBe(true);
  await page.getByRole('button', { name: '删除删除交互验证-1.png', exact: true }).click();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await page.getByRole('button', { name: '删除删除交互验证-1.png', exact: true }).click();
  dialog = page.getByRole('dialog');
  await expect(dialog.getByRole('button', { name: '确认删除 1 张' })).toBeEnabled();
  await page.screenshot({ path: testInfo.outputPath('delete-confirmation-desktop.png'), fullPage: true });
  await dialog.getByRole('button', { name: '确认删除 1 张' }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.locator('[data-photo-id]')).toHaveCount(3);
  for (const i of [2, 3]) await page.getByRole('checkbox', { name: `选择删除交互验证-${i}.png`, exact: true }).check();
  await page.getByRole('button', { name: '删除所选', exact: true }).click();
  dialog = page.getByRole('dialog');
  await expect(dialog.getByRole('listitem')).toHaveCount(2);
  await expect(dialog).not.toContainText('删除交互验证-4.png');
  for (const width of [390, 768]) {
    await page.setViewportSize({ width, height: 900 });
    const box = await dialog.boundingBox();
    expect(box!.x).toBeGreaterThanOrEqual(0);
    expect(box!.x + box!.width).toBeLessThanOrEqual(width + 1);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`delete-confirmation-${width}.png`), fullPage: true });
  }
  await dialog.getByRole('button', { name: '确认删除 2 张' }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.locator('[data-photo-id]')).toHaveCount(1);
  await expect(page.getByRole('button', { name: '删除所选', exact: true })).toBeDisabled();
  await expect(page.getByRole('button', { name: '查看删除交互验证-4.png', exact: true })).toBeVisible();
  expect(errors).toEqual([]);
});

test('changed photo blocks deletion until the confirmation is refreshed', async ({ page, request }) => {
  const [photo] = await upload(request, '删除版本验证', 1);
  await page.goto('/');
  await page.getByRole('button', { name: '去确认', exact: true }).click();
  await page.getByRole('textbox', { name: '搜索照片' }).fill('删除版本验证');
  await page.getByRole('button', { name: '删除删除版本验证-1.png', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByRole('button', { name: '确认删除 1 张' })).toBeEnabled();
  expect((await request.patch(`/api/photos/${photo.id}`, { headers, data: { notes: '并发更新' } })).ok()).toBe(true);
  await dialog.getByRole('button', { name: '确认删除 1 张' }).click();
  await expect(dialog.getByRole('alert')).toContainText('照片信息已变化');
  expect((await request.get(`/api/photos/${photo.id}`)).ok()).toBe(true);
  await expect(dialog.getByRole('button', { name: '确认删除 1 张' })).toBeDisabled();
  await dialog.getByRole('button', { name: '重新核对所选照片' }).click();
  await expect(dialog.getByRole('button', { name: '确认删除 1 张' })).toBeEnabled();
  await dialog.getByRole('button', { name: '确认删除 1 张' }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.locator('[data-photo-id]')).toHaveCount(0);
  await expect(page.getByText('没有符合当前条件的资料', { exact: true })).toBeVisible();
});

test('deleting the last row on the last page returns to the preceding page', async ({ page, request }) => {
  await upload(request, '删除翻页验证', 16);
  await page.goto('/');
  await page.getByRole('button', { name: '去确认', exact: true }).click();
  await page.getByRole('textbox', { name: '搜索照片' }).fill('删除翻页验证');
  await page.locator('.pagination select').selectOption('15');
  // Wait for the debounced filter, not the equally sized previous first page.
  await expect(page.locator('.pagination')).toContainText('1–15 / 16');
  await expect(page.locator('[data-photo-id]')).toHaveCount(15);
  await page.getByRole('button', { name: '下一页', exact: true }).click();
  await expect(page.locator('[data-photo-id]')).toHaveCount(1);
  await page.locator('.photo-actions-column button').click();
  await page.getByRole('dialog').getByRole('button', { name: '确认删除 1 张' }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.locator('[data-photo-id]')).toHaveCount(15);
  await expect(page.locator('.pagination')).toContainText('1 / 1');
});
