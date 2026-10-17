import { test, expect } from '@playwright/test';

test.beforeEach(async ({ page }) => { await page.emulateMedia({ reducedMotion: 'reduce' }); });

test('automatic upload control persists and proposal requires human confirmation', async ({ page, request }, testInfo) => {
  const headers = { 'X-IDI-Request': '1' }, errors: string[] = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.goto('/');
  await expect(page.getByRole('region', { name: '拟提交事项', exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: '系统设置', exact: true }).click();
  const policy = page.getByRole('region', { name: '自动保存设置', exact: true });
  const enabled = policy.getByRole('checkbox', { name: '启用自动上传', exact: true });
  await expect(enabled).not.toBeChecked();
  await enabled.click();
  await expect(enabled).toBeChecked();
  await page.reload();
  await page.getByRole('button', { name: '系统设置', exact: true }).click();
  await expect(enabled).toBeChecked();
  const panel = page.getByRole('region', { name: '拟提交事项', exact: true });
  expect((await (await request.get('/api/workflow/uploads')).json()).policy.enabled).toBe(true);
  const p = await (await request.post('/api/projects', { headers, data: { name: '自动上传验收项目' } })).json();
  const c = await (await request.post('/api/cases', { headers, data: { project_id: p.id, title: '查勘照片自动上传', building: 'A3', room: '3001', platform_report_code: 'ZLBH202610031310420014' } })).json();
  await request.post(`/api/workflow/cases/${c.id}/result`, { headers, data: { action: 'accepted', revision: c.revision, event_at: '2026-10-17 09:00', note: '隔离测试' } });
  const plan = await (await request.get(`/api/platform/cases/${c.id}/stages/survey`)).json();
  plan.case.short_code = 'JMDHSJA-A3-3001-01'; plan.actions = ['save', 'submit'];
  plan.business_fields = { siteSurveyDate: '2026-10-17 09:00:00', defectDesc: '现场查勘核实的原因' };
  plan.link.platform_report_code = c.platform_report_code;
  plan.file_fields.forEach((f: { selected: unknown[]; existing: unknown[]; label: string }, i: number) => { f.selected = []; f.existing = [{ id: `f${i}`, fileName: `${f.label}.png`, fileUrl: '/profile/example.png' }]; });
  const item = { case_id: c.id, enabled: true, state: 'awaiting_confirmation', stage: 'survey', short_code: plan.case.short_code, platform_report_code: c.platform_report_code,
    review_id: 'mock-review', save_review_id: 'mock-save', detail: { reason: '资料已齐，请核对拟提交信息；确认后正式提交', saved_count: 3,
      files: [{ label: '楼栋打卡', count: 1 }, { label: '入户门打卡', count: 1 }, { label: '缺陷问题照片', count: 1 }] } };
  const status = { policy: { enabled: true }, connected: true, write_enabled: true, counts: { awaiting_confirmation: 1 }, items: [item] };
  const review = { id: 'mock-review', case_id: c.id, stage: 'survey', state: 'prepared', fingerprint: 'checked-version', snapshot: plan, result: {}, write_enabled: true, connected: true, active: false, access: plan.access };
  const confirmations: unknown[] = [];
  await page.route('**/api/workflow/uploads', route => route.fulfill({ json: status }));
  await page.route('**/api/workflow/reviews/mock-review', route => route.fulfill({ json: review }));
  await page.route('**/api/workflow/reviews/mock-review/confirm', route => {
    confirmations.push(route.request().postDataJSON());
    return route.fulfill({ json: { ...review, state: 'verified', result: { action: 'submit', phase: 'done' } } });
  });
  await page.reload();
  await expect(panel.getByText('1 案待确认')).toBeVisible();
  for (const width of [1440, 390, 320]) {
    await page.setViewportSize({ width, height: 1000 });
    await expect(panel.getByRole('button', { name: '核对拟提交信息' })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`automatic-upload-${width}.png`), fullPage: true });
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  await panel.getByRole('button', { name: '核对拟提交信息' }).click();
  const dialog = page.getByRole('dialog', { name: '现场查勘 · 预提交审核' });
  await expect(dialog).toBeVisible();
  const submit = dialog.getByRole('button', { name: '确认并提交', exact: true });
  await expect(submit).toBeDisabled();
  expect(confirmations).toEqual([]);
  await dialog.getByRole('checkbox').check();
  await expect(submit).toBeEnabled();
  await page.screenshot({ path: testInfo.outputPath('automatic-upload-review.png'), fullPage: true });
  await submit.click();
  await expect(dialog.getByText('已正式提交 · 官网回读一致')).toBeVisible();
  expect(confirmations).toEqual([{ acknowledged: true, fingerprint: 'checked-version', local_only: false, action: 'submit' }]);
  expect(errors).toEqual([]);
});

test('background saves stay quiet until a complete proposal is ready', async ({ page }, testInfo) => {
  const errors: string[] = [], confirmations: unknown[] = [];
  page.on('pageerror', e => errors.push(e.message));
  const status = {
    policy: { enabled: true }, connected: true, write_enabled: true,
    counts: { needs_fields: 1 } as Record<string, number>,
    items: [{ case_id: 'quiet-case', enabled: true, state: 'needs_fields', stage: 'survey', short_code: 'QUIET-A3-3001-01',
      platform_report_code: 'ZLBH-QUIET', review_id: null as string | null, save_review_id: 'quiet-save',
      detail: { reason: '已保存可用照片；补齐资料后生成拟提交信息', issues: ['缺少缺陷照片/视频'], saved_count: 2,
        files: [{ label: '楼栋打卡', count: 1 }, { label: '入户打卡', count: 1 }, { label: '缺陷照片', count: 0 }] } }],
  };
  let lastServedState = '';
  await page.route('**/api/workflow/uploads', route => {
    lastServedState = status.items[0].state;
    return route.fulfill({ json: status });
  });
  await page.route('**/api/workflow/reviews/*/confirm', route => {
    confirmations.push(route.request().postDataJSON());
    return route.fulfill({ status: 409, json: { detail: 'No automatic confirmation is allowed in this test.' } });
  });
  await page.goto('/');
  const panel = page.getByRole('region', { name: '拟提交事项', exact: true });
  await expect.poll(() => lastServedState).toBe('needs_fields');
  await expect(panel).toHaveCount(0);
  await expect(page.getByRole('checkbox', { name: '启用自动上传' })).toHaveCount(0);
  for (const width of [1440, 390, 320]) {
    await page.setViewportSize({ width, height: 1000 });
    await expect(page.getByRole('button', { name: /^后台保存/ })).toHaveCount(0);
    await expect(panel).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`quiet-background-${width}.png`), fullPage: true });
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.getByRole('button', { name: '系统设置', exact: true }).click();
  await expect(page.getByRole('checkbox', { name: '启用自动上传' })).toBeChecked();
  await page.getByRole('navigation', { name: '设置分类' }).getByRole('button', { name: '后台任务', exact: true }).click();
  const progress = page.getByRole('region', { name: '后台保存进度', exact: true });
  const saved = progress.getByRole('table', { name: '自动保存记录' });
  await expect(saved.getByRole('row')).toHaveCount(2);
  await expect(saved).toContainText('QUIET-A3-3001-01');
  await expect(saved).toContainText('楼栋打卡 × 1 · 入户打卡 × 1');
  await expect(progress).not.toContainText('缺陷照片');
  await expect(progress).not.toContainText('等待补齐资料');
  await expect(progress.getByRole('button', { name: '重新检查' })).toHaveCount(0);
  await expect(progress.getByRole('button', { name: '暂停本案' })).toHaveCount(0);
  await expect(progress.getByRole('checkbox')).toHaveCount(0);
  await expect(progress.getByText('拟提交', { exact: true })).toHaveCount(0);
  for (const width of [1440, 390, 320]) {
    await page.setViewportSize({ width, height: 1000 });
    await expect(saved).toBeVisible();
    expect((await progress.boundingBox())!.x).toBeGreaterThanOrEqual(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    if (width === 1440) expect((await saved.getByRole('row').nth(1).boundingBox())!.height).toBeLessThanOrEqual(56);
    await page.screenshot({ path: testInfo.outputPath(`compact-save-progress-${width}.png`), fullPage: true });
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.getByRole('button', { name: '返回工作台', exact: true }).click();
  await expect(panel).toHaveCount(0);

  for (const state of ['saving', 'readback_pending']) {
    status.items[0].state = state;
    status.counts = { [state]: 1 };
    status.items[0].detail.saved_count = 3;
    status.items[0].detail.files[2].count = 1;
    await expect.poll(() => lastServedState).toBe(state);
    await expect(panel).toHaveCount(0);
    await expect(panel.getByRole('button', { name: '核对拟提交信息' })).toHaveCount(0);
  }

  status.items[0].state = 'awaiting_confirmation';
  status.counts = { awaiting_confirmation: 1 };
  status.items[0].review_id = 'quiet-proposal';
  status.items[0].detail.issues = [];
  await expect(panel.getByRole('button', { name: '核对拟提交信息' })).toBeVisible();
  await expect(panel.getByText('拟提交', { exact: true })).toBeVisible();
  await expect(panel.getByText('1 案待确认', { exact: true })).toBeVisible();
  await expect(panel.getByText('待管理人员确认', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: /^后台保存/ })).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath('quiet-background-proposal.png'), fullPage: true });
  expect(confirmations).toEqual([]);

  // Explicit review actions still report failures, while routine saves remain quiet.
  await page.route('**/api/workflow/reviews/quiet-proposal', route => route.fulfill({ status: 409, json: { detail: '拟提交资料已更新，请重新核对' } }));
  await panel.getByRole('button', { name: '核对拟提交信息' }).click();
  await expect(page.getByText('拟提交资料已更新，请重新核对', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: /^后台保存/ })).toHaveCount(0);

  // Newly arrived material invalidates the old proposal, without reopening progress.
  status.items[0].state = 'needs_fields';
  status.counts = { needs_fields: 1 };
  status.items[0].review_id = null;
  status.items[0].detail.issues = ['新收到的照片仍待核对'];
  await expect(panel.getByRole('button', { name: '核对拟提交信息' })).toHaveCount(0);
  await expect(panel).toHaveCount(0);
  expect(confirmations).toEqual([]);
  expect(errors).toEqual([]);
});
