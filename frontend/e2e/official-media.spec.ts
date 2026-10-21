import { test, expect } from '@playwright/test';

const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAHgAAABkCAIAAADCEmNlAAAA80lEQVR4nO3SQQ3AMAADsazIC30oei+bQKRTvu2O906wgdAdj44IHRE6InRE6IjQEaEjQkeEjggdEToidEToiNARoSNCR4SOCB0ROiJ0ROiI0BGhI0JHhI4IHRE6InRE6IjQEaEjQkeEjggdEToidEToiNARoSNCR4SOCB0ROiJ0ROiI0BGhI0JHhI4IHRE6InRE6IjQEaEjQkeEjggdEToidEToiNARoSNCR4SOCB0ROiJ0ROiI0BGhI0JHhI4IHRE6InRE6IjQEaEjQkeEjggdEToidEToiNARoSNCR4SOCB0ROiJ0ROiI0BGhI0JHhF7jByp2AUhQ6fjGAAAAAElFTkSuQmCC', 'base64');

test('official photos appear in both form and review, with local preview and retry', async ({ page, request }, testInfo) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  const headers = { 'X-IDI-Request': '1' }, errors: string[] = [], confirmations: unknown[] = [];
  page.on('pageerror', error => errors.push(error.message));
  const project = await (await request.post('/api/projects', { headers, data: { name: '官网照片预览验收', short_prefix: 'MEDIA' } })).json();
  const item = await (await request.post('/api/cases', { headers, data: { project_id: project.id, title: '柜门与水龙头松动', building: 'C4', room: '1101' } })).json();
  await request.post(`/api/workflow/cases/${item.id}/result`, { headers, data: { action: 'accepted', revision: item.revision, event_at: '2026-10-20 09:00', note: '隔离测试' } });
  const plan = await (await request.get(`/api/platform/cases/${item.id}/stages/survey`)).json();
  plan.link.report_receiving_id = 'official-media-case';
  plan.actions = ['save', 'submit'];
  plan.business_fields = { siteSurveyDate: '2026-10-16 10:12:00', defectiveSite: '柜门、水龙头、门把手' };
  plan.file_fields.forEach((field: { key: string; selected: unknown[]; existing: unknown[] }, index: number) => {
    field.selected = [];
    field.existing = Array.from({ length: index === 2 ? 3 : 1 }, (_, number) => ({
      id: `${field.key}-${number}`, oldFileName: `${['楼栋打卡', '入户打卡', '缺陷照片'][index]}${number + 1}.png`,
      fileUrl: `/profile/${field.key}-${number}.png`, fileType: 'png',
    }));
  });
  plan.file_fields[2].selected = [{
    id: 'new-picture', original_name: '本次补充.png', original_url: '/api/photos/new-picture/original',
    thumbnail_url: '/api/photos/new-picture/thumbnail', media_type: 'image', photo_group: 'survey_problem', watermark_time: '2026-10-16 10:15:00',
  }];
  const status = { policy: { enabled: true }, connected: true, write_enabled: true, counts: { awaiting_confirmation: 1 }, items: [{
    case_id: item.id, enabled: true, state: 'awaiting_confirmation', stage: 'survey', short_code: item.short_code, review_id: 'media-review', save_review_id: 'media-save',
    detail: { saved_count: 5, files: [{ label: '楼栋打卡图片', count: 1 }, { label: '入户打卡图片', count: 1 }, { label: '缺陷照片/视频', count: 3 }] },
  }] };
  const review = { id: 'media-review', case_id: item.id, stage: 'survey', state: 'prepared', fingerprint: 'unchanged-photo-review',
    snapshot: plan, result: {}, write_enabled: true, connected: true, active: false, access: plan.access };
  await page.route('**/api/workflow/uploads', route => route.fulfill({ json: status }));
  await page.route('**/api/workflow/reviews/media-review', route => route.fulfill({ json: review }));
  await page.route('**/api/workflow/reviews/*/confirm', route => {
    confirmations.push(route.request().postDataJSON());
    return route.fulfill({ status: 409, json: { detail: 'This preview must not write to the official site.' } });
  });
  await page.route(`**/api/platform/cases/${item.id}/stages/survey`, route => route.fulfill({ json: plan }));
  await page.route(`**/api/platform/cases/${item.id}/stages/survey/preview`, route => route.fulfill({ json: plan }));
  let failedOnce = false;
  await page.route('**/files/**', route => {
    if (route.request().url().includes('fileInfoList-1/thumbnail') && !failedOnce) {
      failedOnce = true;
      return route.fulfill({ status: 502, json: { detail: '临时下载失败' } });
    }
    return route.fulfill({ contentType: 'image/png', body: png });
  });
  await page.route('**/api/photos/new-picture/*', route => route.fulfill({ contentType: 'image/png', body: png }));
  await page.goto('/');
  const proposal = page.getByRole('region', { name: '拟提交事项', exact: true });
  await proposal.getByRole('button', { name: '核对拟提交信息', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '现场查勘 · 预提交审核', exact: true });
  await expect(dialog.locator('.attachment-card[data-source=official]')).toHaveCount(5);
  await expect(dialog.locator('.attachment-card[data-source=local]')).toHaveCount(1);
  await dialog.getByRole('button', { name: '查看缺陷照片2.png大图', exact: true }).scrollIntoViewIfNeeded();
  const retry = dialog.getByRole('button', { name: '重试同步', exact: true });
  await expect(retry).toBeVisible();
  await retry.click();
  for (const img of await dialog.locator('.attachment-gallery img').all()) {
    await img.scrollIntoViewIfNeeded();
    await expect.poll(() => img.evaluate(node => (node as HTMLImageElement).naturalWidth)).toBeGreaterThan(0);
  }
  await expect(dialog.getByText('已在本地', { exact: true })).toHaveCount(5);
  const card = dialog.getByRole('button', { name: '查看缺陷照片1.png大图', exact: true });
  await card.click();
  const viewer = page.getByRole('dialog', { name: '缺陷照片/视频 · 照片预览', exact: true });
  await expect(viewer.getByRole('img', { name: '缺陷照片1.png' })).toBeVisible();
  await viewer.getByRole('button', { name: '下一张', exact: true }).click();
  await expect(viewer.getByRole('img', { name: '缺陷照片2.png' })).toBeVisible();
  await viewer.getByRole('button', { name: '放大细看', exact: true }).click();
  await expect(viewer.locator('.attachment-viewer-canvas')).toHaveClass(/is-zoomed/);
  await page.keyboard.press('Escape');
  await expect(viewer).toHaveCount(0);
  await expect(dialog).toBeVisible();
  await expect(card).toBeFocused();
  await expect(dialog.getByRole('checkbox')).not.toBeChecked();
  await expect(dialog.getByRole('button', { name: '确认并提交', exact: true })).toBeDisabled();
  expect(confirmations).toEqual([]);
  for (const width of [1440, 390, 320]) {
    await page.setViewportSize({ width, height: 1000 });
    await dialog.locator('.attachment-gallery').first().scrollIntoViewIfNeeded();
    expect(await dialog.evaluate(node => node.scrollWidth <= node.clientWidth + 1)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`review-photos-${width}.png`), fullPage: true });
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  await dialog.getByRole('button', { name: '关闭', exact: true }).last().click();
  await proposal.getByRole('button', { name: item.short_code, exact: true }).click();
  const fields = page.locator('.platform-file-field');
  await expect(fields.locator('.attachment-card[data-source=official]')).toHaveCount(5);
  for (const img of await fields.locator('.attachment-gallery img').all()) {
    await img.scrollIntoViewIfNeeded();
    await expect.poll(() => img.evaluate(node => (node as HTMLImageElement).naturalWidth)).toBeGreaterThan(0);
  }
  await fields.getByRole('button', { name: '查看楼栋打卡1.png大图', exact: true }).click();
  const fieldViewer = page.getByRole('dialog', { name: '楼栋打卡图片 · 照片预览', exact: true });
  await expect(fieldViewer.getByRole('img', { name: '楼栋打卡1.png' })).toBeVisible();
  await page.keyboard.press('Escape');
  await page.screenshot({ path: testInfo.outputPath('official-field-photos.png'), fullPage: true });
  expect(confirmations).toEqual([]);
  expect(errors).toEqual([]);
});
