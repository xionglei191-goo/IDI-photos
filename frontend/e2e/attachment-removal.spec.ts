import { test, expect, type APIRequestContext, type Page } from '@playwright/test';

const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAHgAAABkCAIAAADCEmNlAAAA80lEQVR4nO3SQQ3AMAADsazIC30oei+bQKRTvu2O906wgdAdj44IHRE6InRE6IjQEaEjQkeEjggdEToidEToiNARoSNCR4SOCB0ROiJ0ROiI0BGhI0JHhI4IHRE6InRE6IjQEaEjQkeEjggdEToidEToiNARoSNCR4SOCB0ROiJ0ROiI0BGhI0JHhI4IHRE6InRE6IjQEaEjQkeEjggdEToidEToiNARoSNCR4SOCB0ROiJ0ROiI0BGhI0JHhI4IHRE6InRE6IjQEaEjQkeEjggdEToidEToiNARoSNCR4SOCB0ROiJ0ROiI0BGhI0JHhF7jByp2AUhQ6fjGAAAAAElFTkSuQmCC', 'base64');

async function setup(page: Page, request: APIRequestContext, pending = false) {
  const headers = { 'X-IDI-Request': '1' };
  const suffix = Date.now().toString(36).slice(-5).toUpperCase();
  const project = await (await request.post('/api/projects', { headers, data: { name: '照片撤回隔离验收' + suffix, short_prefix: 'R' + suffix } })).json();
  const item = await (await request.post('/api/cases', { headers, data: { project_id: project.id, title: '照片删除验证', building: '4', room: '1101' } })).json();
  await request.post(`/api/workflow/cases/${item.id}/result`, { headers, data: { action: 'accepted', revision: item.revision, event_at: '2026-10-20 09:00', note: '隔离测试' } });
  const plan = await (await request.get(`/api/platform/cases/${item.id}/stages/survey`)).json();
  plan.link.report_receiving_id = 'removal-case';
  plan.draft.target_report_id = 'removal-case';
  plan.draft.revision = 7;
  plan.actions = ['save', 'submit'];
  const file = { id: 'wrong', oldFileName: '错误照片.png', fileUrl: '/profile/wrong.png', fileType: 'png' };
  plan.file_fields[2].existing = [file, { ...file, id: 'keep', oldFileName: '保留照片.png', fileUrl: '/profile/keep.png' }];
  plan.file_fields[2].selected = [{ id: 'local', original_name: '本地选材.png', original_url: '/api/photos/local/original',
    thumbnail_url: '/api/photos/local/thumbnail', media_type: 'image', photo_group: 'survey_problem' }];
  const review = { id: 'removal-review', case_id: item.id, stage: 'survey', state: 'prepared', fingerprint: 'review-fingerprint',
    snapshot: plan, result: {}, write_enabled: true, connected: true, active: false, access: plan.access };
  const record = { id: 'remove-1', case_id: item.id, stage: 'survey', field_key: 'fileInfoList', file_id: 'wrong',
    fingerprint: 'remove-fingerprint', state: 'prepared', error: '', file, label: '缺陷照片/视频',
    stage_label: '现场查勘', case_title: item.title, short_code: item.short_code };
  const status = { policy: { enabled: true }, connected: true, write_enabled: true, counts: { awaiting_confirmation: 1 },
    items: [{ case_id: item.id, enabled: true, state: 'awaiting_confirmation', stage: 'survey', short_code: item.short_code,
      review_id: review.id, detail: { saved_count: 2, files: [] } }] };
  const confirms: unknown[] = [], submissions: unknown[] = [], locals: unknown[] = [], reads: string[] = [], errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/api/workflow/uploads', route => route.fulfill({ json: status }));
  await page.route('**/api/platform/session', route => route.fulfill({ json: { connected: true, write_enabled: true, account: 'test' } }));
  await page.route('**/api/workflow/reviews/removal-review', route => route.fulfill({ json: review }));
  await page.route('**/api/workflow/reviews/*/confirm', route => {
    submissions.push(route.request().postDataJSON());
    return route.fulfill({ status: 409, json: { detail: 'Unexpected stage submission' } });
  });
  await page.route(`**/api/platform/cases/${item.id}/stages/survey`, route => route.fulfill({ json: plan }));
  await page.route(`**/api/platform/cases/${item.id}/stages/survey/preview`, route => route.fulfill({ json: plan }));
  await page.route('**/files/**', route => route.fulfill({ contentType: 'image/png', body: png }));
  await page.route('**/api/photos/local/*', route => route.fulfill({ contentType: 'image/png', body: png }));
  await page.route('**/files/fileInfoList/official/wrong/removal', route => route.fulfill({ json: record }));
  await page.route('**/files/fileInfoList/local/local/remove', route => {
    locals.push(route.request().postDataJSON());
    plan.file_fields[2].selected = [];
    return route.fulfill({ json: plan });
  });
  await page.route('**/api/platform/attachment-removals/remove-1/thumbnail', route => route.fulfill({ contentType: 'image/png', body: png }));
  await page.route('**/api/platform/attachment-removals/remove-1/original', route => route.fulfill({ contentType: 'image/png', body: png }));
  await page.route('**/api/platform/attachment-removals/remove-1/confirm', route => {
    confirms.push(route.request().postDataJSON());
    record.state = pending ? 'uncertain' : 'verified';
    record.error = pending ? '响应超时，请回读官网核实' : '';
    if (pending) plan.attachment_removals = [record];
    else plan.file_fields[2].existing = plan.file_fields[2].existing.filter((f: { id: string }) => f.id !== 'wrong');
    return route.fulfill({ json: record });
  });
  await page.route('**/api/platform/attachment-removals/remove-1/readback', route => {
    reads.push('readback'); record.state = 'verified'; record.error = ''; plan.attachment_removals = [];
    plan.file_fields[2].existing = plan.file_fields[2].existing.filter((f: { id: string }) => f.id !== 'wrong');
    return route.fulfill({ json: record });
  });
  await page.goto('/');
  await expect(page.getByRole('button', { name: '官网已连接', exact: true })).toBeVisible();
  return { item, plan, confirms, submissions, locals, reads, errors };
}

test('review withdrawal requires confirmation, cancels safely and invalidates the old proposal', async ({ page, request }, testInfo) => {
  const fixture = await setup(page, request);
  await page.getByRole('button', { name: '核对拟提交信息', exact: true }).click();
  const review = page.getByRole('dialog', { name: '现场查勘 · 预提交审核', exact: true });
  const remove = review.getByRole('button', { name: '删除并撤回：错误照片.png', exact: true });
  await remove.click();
  const dialog = page.getByRole('dialog', { name: '删除并从官网撤回', exact: true });
  const confirm = dialog.getByRole('button', { name: '确认删除并撤回', exact: true });
  await expect(confirm).toBeDisabled();
  await expect(dialog.getByRole('img', { name: '错误照片.png', exact: true })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
  await expect(review).toBeVisible();
  expect(fixture.confirms).toEqual([]);
  await remove.click();
  await expect(dialog.getByRole('checkbox')).toBeEnabled();
  for (const width of [1440, 390, 320]) {
    await page.setViewportSize({ width, height: 1000 });
    expect(await dialog.evaluate(node => node.scrollWidth <= node.clientWidth + 1)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`withdrawal-confirm-${width}.png`), fullPage: true });
  }
  await dialog.getByRole('checkbox').check();
  await confirm.click();
  await expect(review).toHaveCount(0);
  await expect(dialog).toHaveCount(0);
  expect(fixture.confirms).toEqual([{ fingerprint: 'remove-fingerprint', acknowledged: true }]);
  expect(fixture.submissions).toEqual([]);
  expect(fixture.errors).toEqual([]);
});

test('form keeps an uncertain withdrawal visible and only rereads it after reopening', async ({ page, request }, testInfo) => {
  const fixture = await setup(page, request, true);
  await page.getByRole('region', { name: '拟提交事项', exact: true }).getByRole('button', { name: fixture.item.short_code, exact: true }).click();
  await page.getByRole('button', { name: '删除并撤回：错误照片.png', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '删除并从官网撤回', exact: true });
  await expect(dialog.getByRole('checkbox')).toBeEnabled();
  await dialog.getByRole('checkbox').check();
  await dialog.getByRole('button', { name: '确认删除并撤回', exact: true }).click();
  await expect(dialog.getByText('响应超时，请回读官网核实', { exact: true })).toBeVisible();
  await expect(dialog.getByRole('button', { name: '确认删除并撤回', exact: true })).toHaveCount(0);
  await dialog.getByRole('button', { name: '稍后核对', exact: true }).click();
  await expect(page.getByText('缺陷照片/视频 · 照片撤回待核实', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '删除并撤回：错误照片.png', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: '核对撤回结果', exact: true }).click();
  await page.screenshot({ path: testInfo.outputPath('withdrawal-uncertain.png'), fullPage: true });
  await dialog.getByRole('button', { name: '回读撤回结果', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole('button', { name: '删除并撤回：错误照片.png', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '删除并撤回：保留照片.png', exact: true })).toBeVisible();
  expect(fixture.confirms).toHaveLength(1);
  expect(fixture.reads).toHaveLength(1);
  expect(fixture.submissions).toEqual([]);
  expect(fixture.errors).toEqual([]);
});

test('review can remove a pending local photo without an official write', async ({ page, request }) => {
  const fixture = await setup(page, request);
  await page.getByRole('button', { name: '核对拟提交信息', exact: true }).click();
  await page.getByRole('button', { name: '移除选材：本地选材.png', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '移除本环节选材', exact: true });
  await dialog.getByRole('checkbox').check();
  await dialog.getByRole('button', { name: '确认移除选材', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  expect(fixture.locals).toEqual([{ revision: 7 }]);
  expect(fixture.confirms).toEqual([]);
  expect(fixture.submissions).toEqual([]);
  expect(fixture.errors).toEqual([]);
});
