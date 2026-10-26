import { test, expect, type Page, type APIRequestContext } from '@playwright/test';

const headers = { 'X-IDI-Request': '1' };
const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=', 'base64');

async function prepare(page: Page, request: APIRequestContext, options: { failFirst?: boolean; holdFirst?: boolean; advanceStage?: boolean } = {}) {
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
  plan.values.defectDesc = '上次核验的原因';
  plan.draft.values.defectDesc = '上次核验的原因';
  if (options.holdFirst) { plan.error_count = 0; plan.issues = []; }
  const repairPlan = await (await request.get(`/api/platform/cases/${item.id}/stages/repair`)).json();
  repairPlan.access = { ...repairPlan.access, editable: true, mode: 'editable', workflow_stage: 'repair', active_stage: 'repair', active_label: '修复成果' };
  const field = plan.file_fields.find((f: { key: string }) => f.key === 'fileInfoList');
  const keep = { id: 'keep', fileUrl: '/profile/keep.png', oldFileName: '仍在官网.png' };
  const removed = { id: 'deleted', fileUrl: '/profile/deleted.png', oldFileName: '官网删除的最后一张.png', removed_at: '2026-10-21T03:00:00Z' };
  field.existing = [keep, removed];
  field.selected = [];
  let calls = 0;
  let releaseReadback = () => {};
  const readbackGate = new Promise<void>(resolve => { releaseReadback = resolve; });
  const writes: string[] = [], errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', req => { if (req.url().includes('/api/') && !['GET', 'HEAD'].includes(req.method())) writes.push(new URL(req.url()).pathname); });
  await page.route(`**/api/workflow/cases/${item.id}`, route => route.fulfill({ json: detail }));
  await page.route(`**/api/workflow/cases/${item.id}/refresh`, async route => {
    calls++;
    if (options.holdFirst && calls === 1) await readbackGate;
    if (options.failFirst && calls === 1) return route.fulfill({ status: 502, json: { detail: '测试：官网暂时离线' } });
    field.existing = [keep];
    field.removed = [removed];
    detail.official_read_at = removed.removed_at;
    plan.link.verified_at = removed.removed_at;
    plan.link.revision += 1;
    plan.values.defectDesc = '官网新核验的原因';
    plan.draft.values.defectDesc = '官网新核验的原因';
    if (options.advanceStage) {
      detail.case.workflow_stage = 'repair';
      detail.form_stage = 'repair';
      detail.stage_label = '施工中';
      detail.stage_access.survey = { ...detail.stage_access.survey, editable: false, mode: 'completed', reason: '现场查勘已完成，本环节仅供查看。', workflow_stage: 'repair', active_stage: 'repair', active_label: '修复成果' };
      detail.stage_access.repair = repairPlan.access;
      plan.access = detail.stage_access.survey;
    }
    return route.fulfill({ json: detail });
  });
  await page.route(`**/api/platform/cases/${item.id}/stages/survey`, route => route.fulfill({ json: plan }));
  await page.route(`**/api/platform/cases/${item.id}/stages/survey/preview`, route => route.fulfill({ json: plan }));
  await page.route(`**/api/platform/cases/${item.id}/stages/repair`, route => route.fulfill({ json: repairPlan }));
  await page.route('**/files/**', route => route.fulfill({ contentType: 'image/png', body: png }));
  await page.goto('/');
  await page.getByRole('navigation', { name: '工作看板' }).getByRole('button', { name: '查勘维修', exact: true }).click();
  const open = async () => page.locator(`[data-case-id="${item.id}"] .case-link`).click();
  await open();
  return { calls: () => calls, writes, errors, item, open, releaseReadback, field: page.locator('[data-field="fileInfoList"]') };
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
  const state = await prepare(page, request, { failFirst: true });
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

test('cached case stays usable before a slow website read completes and keeps edits afterwards', async ({ page, request }, testInfo) => {
  const state = await prepare(page, request, { holdFirst: true });
  try {
    const reason = page.getByRole('textbox', { name: '缺陷原因分析', exact: true });
    // The remote response is deliberately withheld: visibility cannot depend on it.
    await expect(reason).toBeVisible({ timeout: 2500 });
    await expect(reason).toHaveValue('上次核验的原因');
    await expect(state.field.locator('.attachment-card[data-source=official]')).toHaveCount(2);
    await expect(page.getByText('已显示本地内容，正在后台核对官网最新进度和附件…', { exact: true })).toBeVisible();
    await reason.fill('用户正在填写的原因，回读不得覆盖');
    await expect(page.getByRole('button', { name: '预提交审核', exact: true })).toBeDisabled();
    await page.screenshot({ path: testInfo.outputPath('cached-before-readback.png'), fullPage: true });
    state.releaseReadback();
    await expect(page.getByText('已显示本地内容，正在后台核对官网最新进度和附件…', { exact: true })).toHaveCount(0);
    await expect(state.field.locator('.attachment-card[data-source=official]')).toHaveCount(1);
    await expect(reason).toHaveValue('用户正在填写的原因，回读不得覆盖');
    await expect(page.getByRole('button', { name: '预提交审核', exact: true })).toBeEnabled();
    expect(state.calls()).toBe(1);
    expect(state.writes.filter(path => !path.endsWith('/preview'))).toEqual([`/api/workflow/cases/${state.item.id}/refresh`]);
    expect(state.errors).toEqual([]);
  } finally { state.releaseReadback(); }
});

test('an untouched cached form follows the verified new stage after background readback', async ({ page, request }) => {
  const state = await prepare(page, request, { holdFirst: true, advanceStage: true });
  try {
    await expect(page.getByRole('heading', { name: '现场查勘 · 业务信息', exact: true })).toBeVisible({ timeout: 2500 });
    state.releaseReadback();
    await expect(page.locator('.case-detail-heading .workflow-status')).toHaveText('施工中');
    await expect(page.getByRole('heading', { name: '修复成果 · 业务信息', exact: true })).toBeVisible();
    expect(state.calls()).toBe(1);
    expect(state.errors).toEqual([]);
  } finally { state.releaseReadback(); }
});

test('a stage change in background preserves unsaved text and prevents writes to the old stage', async ({ page, request }) => {
  const state = await prepare(page, request, { holdFirst: true, advanceStage: true });
  try {
    const reason = page.getByRole('textbox', { name: '缺陷原因分析', exact: true });
    await expect(reason).toBeVisible({ timeout: 2500 });
    await reason.fill('必须保留的未保存查勘填写');
    state.releaseReadback();
    await expect(page.locator('.case-detail-heading .workflow-status')).toHaveText('施工中');
    await expect(page.getByLabel('缺陷原因分析', { exact: true })).toContainText('必须保留的未保存查勘填写');
    await expect(page.getByRole('button', { name: '放弃未保存修改', exact: true })).toBeVisible();
    await page.getByRole('button', { name: '返回看板', exact: true }).click();
    await expect(page.getByLabel('缺陷原因分析', { exact: true })).toContainText('必须保留的未保存查勘填写');
    await page.getByRole('button', { name: '放弃未保存修改', exact: true }).click();
    await expect(page.getByRole('heading', { name: '修复成果 · 业务信息', exact: true })).toBeVisible();
    expect(state.writes.filter(path => !path.endsWith('/preview'))).toEqual([`/api/workflow/cases/${state.item.id}/refresh`]);
    expect(state.errors).toEqual([]);
  } finally { state.releaseReadback(); }
});
