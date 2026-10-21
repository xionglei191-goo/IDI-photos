import { test, expect, type APIRequestContext } from '@playwright/test';

const headers = { 'X-IDI-Request': '1' };
const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAHgAAABkCAIAAADCEmNlAAAA80lEQVR4nO3SQQ3AMAADsazIC30oei+bQKRTvu2O906wgdAdj44IHRE6InRE6IjQEaEjQkeEjggdEToidEToiNARoSNCR4SOCB0ROiJ0ROiI0BGhI0JHhI4IHRE6InRE6IjQEaEjQkeEjggdEToidEToiNARoSNCR4SOCB0ROiJ0ROiI0BGhI0JHhI4IHRE6InRE6IjQEaEjQkeEjggdEToidEToiNARoSNCR4SOCB0ROiJ0ROiI0BGhI0JHhI4IHRE6InRE6IjQEaEjQkeEjggdEToidEToiNARoSNCR4SOCB0ROiJ0ROiI0BGhI0JHhF7jByp2AUhQ6fjGAAAAAElFTkSuQmCC', 'base64');

async function setup(request: APIRequestContext) {
  const suffix = Date.now().toString(36).toUpperCase();
  const project = await (await request.post('/api/projects', { headers, data: { name: '复用副本验证' + suffix } })).json();
  let item = await (await request.post('/api/cases', { headers, data: { project_id: project.id, title: '当天查勘维修', building: '1', room: '1101' } })).json();
  for (const action of ['accepted', 'survey_completed']) {
    const response = await request.post(`/api/workflow/cases/${item.id}/result`, { headers,
      data: { action, revision: item.revision, event_at: '2026-10-16 10:00', note: '隔离测试' } });
    expect(response.ok()).toBe(true);
    item = await (await request.get(`/api/cases/${item.id}`)).json();
  }
  const form = new FormData();
  form.append('auto_ocr', 'false'); form.append('case_id', item.id);
  form.append('files', new Blob([png], { type: 'image/png' }), '当天楼栋打卡.png');
  const upload = await (await request.post('/api/import/upload', { headers, multipart: form })).json();
  await expect.poll(async () => (await (await request.get(`/api/jobs/${upload.job.id}`)).json()).state).toBe('completed');
  const photos = await (await request.get(`/api/photos?case_id=${item.id}`)).json();
  const original = await (await request.patch(`/api/photos/${photos.items[0].id}`, { headers,
    data: { photo_group: 'survey_checkin', role: 'building_checkin', watermark_time: '2026-10-16 10:08', confirm: true } })).json();
  return { item, original };
}

test('manual repair selection saves a separate work copy and visit reuse selects that same copy', async ({ page, request }, testInfo) => {
  const { item, original } = await setup(request);
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/');
  await page.getByRole('button', { name: '查勘维修', exact: true }).click();
  await page.locator(`[data-case-id="${item.id}"] .case-link`).click();
  const field = page.locator('[data-field="buildingFileInfoList"]');
  await field.getByRole('button', { name: '选择文件', exact: true }).click();
  let dialog = page.getByRole('dialog', { name: '选择楼栋打卡照片', exact: true });
  await expect(dialog.getByText('保存后复制为维修打卡', { exact: true })).toBeVisible();
  await dialog.getByRole('checkbox', { name: '选择当天楼栋打卡.png', exact: true }).check();
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    expect(await dialog.evaluate(node => node.scrollWidth <= node.clientWidth + 1)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`copy-picker-${width}.png`), fullPage: true });
  }
  await dialog.getByRole('button', { name: '使用所选资料', exact: true }).click();
  await expect(field).toContainText('保存后复制为维修打卡');
  await page.getByRole('button', { name: '保存本地草稿', exact: true }).click();
  await expect(field).toContainText('维修打卡');
  await expect(field.getByRole('link', { name: '查看查勘原照', exact: true })).toHaveAttribute('href', `/api/photos/${original.id}/original`);
  const all = await (await request.get(`/api/photos?case_id=${item.id}`)).json();
  expect(all.total).toBe(2);
  const copy = all.items.find((p: { reuse_source_id: string }) => p.reuse_source_id === original.id);
  expect(copy.id).not.toBe(original.id);
  expect(copy.photo_group).toBe('work_checkin');
  expect(all.items.find((p: { id: string }) => p.id === original.id).revision).toBe(original.revision);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.getByRole('button', { name: /^保司资料/ }).click();
  await page.locator('.case-detail-heading').getByRole('button', { name: '进场与维修记录', exact: true }).click();
  dialog = page.getByRole('dialog', { name: '进场与维修记录', exact: true });
  await dialog.getByLabel('进场日期', { exact: true }).fill('2026-10-16');
  await dialog.getByRole('checkbox', { name: '查勘后当天维修，与查勘是同一次进场', exact: true }).check();
  await dialog.getByRole('checkbox', { name: '当天楼栋打卡.png（复制为维修打卡）', exact: true }).check();
  await dialog.getByRole('button', { name: '建立进场记录', exact: true }).click();
  await expect(dialog.locator('.visit-counts')).toContainText('楼栋打卡 1');
  await expect(dialog.locator('.visit-counts')).toContainText('复用副本 1');
  await expect(dialog.getByRole('checkbox', { name: '当天楼栋打卡.png（本次资料）', exact: true })).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath('copy-visit.png'), fullPage: true });
  expect((await (await request.get(`/api/photos?case_id=${item.id}`)).json()).total).toBe(2);
  const visits = await (await request.get(`/api/cases/${item.id}/visits`)).json();
  expect(visits[0].copied_checkin_ids).toEqual([copy.id]);
  expect(errors).toEqual([]);
});
