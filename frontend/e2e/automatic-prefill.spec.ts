import { test, expect } from '@playwright/test';

test('automatic fields explain their evidence and do not overwrite a dirty form', async ({ page, request }, testInfo) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  const headers = { 'X-IDI-Request': '1' }, errors: string[] = [];
  page.on('pageerror', e => errors.push(e.message));
  const project = await (await request.post('/api/projects', { headers, data: { name: '自动填字段验收项目', short_prefix: 'FILL' } })).json();
  const item = await (await request.post('/api/cases', { headers, data: { project_id: project.id, title: '主卧卫生间瓷砖破裂，卫生间反臭', building: 'A3', room: '3001' } })).json();
  await request.post(`/api/workflow/cases/${item.id}/result`, { headers, data: { action: 'accepted', revision: item.revision, event_at: '2026-10-17 09:00', note: '隔离测试' } });
  const plan = await (await request.get(`/api/platform/cases/${item.id}/stages/survey`)).json();
  plan.link.report_receiving_id = 'test-official-id';
  plan.draft.target_report_id = 'test-official-id';
  plan.draft.revision = 1;
  plan.draft.values = { defectDesc: '施工时保护不到位、操作不当造成', defectiveSite: '主卧卫生间瓷砖' };
  plan.values = { ...plan.draft.values };
  plan.field_sources = {
    defectDesc: { origin: 'automatic', value: plan.values.defectDesc, source: '官网受理时填写的原因说明；请核对', photo_ids: [], needs_review: true, current: true, stale: false },
    defectiveSite: { origin: 'automatic', value: plan.values.defectiveSite, source: '报修问题中的部位用语', photo_ids: [], needs_review: true, current: true, stale: false },
  };
  const status = { policy: { enabled: true }, connected: true, write_enabled: true, counts: { needs_fields: 1 }, items: [{
    case_id: item.id, enabled: true, state: 'saving', stage: 'survey', short_code: item.short_code, save_review_id: 'field-save',
    detail: { reason: '已自动填写可确定字段；等待补齐本环节资料', other_stage_count: 18,
      filled_fields: [
        { field: 'defectDesc', label: '缺陷原因分析', value: plan.values.defectDesc, source: plan.field_sources.defectDesc.source, needs_review: true },
        { field: 'defectiveSite', label: '缺陷部位', value: plan.values.defectiveSite, source: plan.field_sources.defectiveSite.source, needs_review: true },
      ], issues: ['缺少现场查勘日期'], field_notes: [{ label: '现场查勘日期', reason: '维修打卡时间不能直接当作查勘时间。' }] }
  }] };
  const saved: { revision: number; values: Record<string, string> }[] = [];
  await page.route('**/api/workflow/uploads', route => route.fulfill({ json: status }));
  await page.route(`**/api/platform/cases/${item.id}/stages/survey`, route => {
    if (route.request().method() === 'PUT') {
      saved.push(route.request().postDataJSON());
      return route.fulfill({ status: 409, json: { detail: '本地草稿已被修改，请刷新后重试' } });
    }
    return route.fulfill({ json: plan });
  });
  await page.route(`**/api/platform/cases/${item.id}/stages/survey/preview`, route => route.fulfill({ json: { ...plan, values: route.request().postDataJSON().values } }));
  await page.goto('/');
  await expect(page.getByRole('region', { name: '拟提交事项', exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: '系统设置', exact: true }).click();
  await page.getByRole('navigation', { name: '设置分类' }).getByRole('button', { name: '后台任务', exact: true }).click();
  const panel = page.getByRole('region', { name: '后台保存进度', exact: true });
  await expect(panel).toContainText('暂无自动保存内容');
  await expect(panel.getByRole('table')).toHaveCount(0);
  status.items[0].state = 'needs_fields';
  await expect(panel.getByRole('table', { name: '自动保存记录' })).toBeVisible();
  await expect(panel).toContainText('缺陷原因分析 · 缺陷部位');
  await expect(panel).not.toContainText('施工时保护不到位、操作不当造成');
  await expect(panel).not.toContainText('维修打卡时间不能直接当作查勘时间');
  await expect(panel).not.toContainText('已归档 18 张');
  for (const width of [1440, 390, 320]) {
    await page.setViewportSize({ width, height: 1000 });
    expect((await panel.boundingBox())!.x).toBeGreaterThanOrEqual(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`automatic-prefill-${width}.png`), fullPage: true });
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  await panel.getByRole('button', { name: item.short_code, exact: true }).click();
  await expect(panel).toHaveCount(0);
  await expect(page.getByRole('region', { name: '拟提交事项', exact: true })).toHaveCount(0);
  await expect(page.getByRole('textbox', { name: '缺陷原因分析', exact: true })).toHaveValue(plan.values.defectDesc);
  await expect(page.locator('.platform-prefill-source').filter({ hasText: '已自动填写：官网受理' })).toBeVisible();
  // A background fill becomes visible while the user is not editing.
  plan.draft.revision = 2;
  plan.draft.values.defectiveSite = '主卧卫生间墙面瓷砖';
  plan.values.defectiveSite = plan.draft.values.defectiveSite;
  await expect(page.getByRole('textbox', { name: '缺陷部位', exact: true })).toHaveValue('主卧卫生间墙面瓷砖');
  await page.getByRole('textbox', { name: '缺陷原因分析', exact: true }).fill('人工检查后的具体原因');
  plan.draft.revision = 3;
  plan.draft.values.defectDesc = '另一项后台记录';
  plan.values.defectDesc = plan.draft.values.defectDesc;
  // Force a preview of the dirty form; it must retain the editing revision.
  await expect.poll(async () => page.locator('.platform-workspace').getByText('修改尚未保存').count()).toBe(1);
  await page.waitForTimeout(3700);
  await expect(page.getByRole('textbox', { name: '缺陷原因分析', exact: true })).toHaveValue('人工检查后的具体原因');
  await page.getByRole('button', { name: '保存本地草稿', exact: true }).click();
  await expect(page.getByText('本地草稿已被修改，请刷新后重试', { exact: true })).toBeVisible();
  expect(saved[0].revision).toBe(2);
  expect(saved[0].values.defectDesc).toBe('人工检查后的具体原因');
  expect(errors).toEqual([]);
});
