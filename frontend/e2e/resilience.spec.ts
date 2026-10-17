import { test, expect } from '@playwright/test';

const headers = { 'X-IDI-Request': '1' };

test('independent runtime states and recovery controls remain usable on desktop and mobile', async ({ page, request }, testInfo) => {
  const errors: string[] = [];
  page.on('pageerror', e => errors.push(e.message));
  const status = await (await request.get('/api/operations')).json();
  status.model = { ...status.model, state: 'paused', enabled: true, configured: true, retry_at: '2026-10-17T09:01:00Z', error: '模型请求受限，等待服务恢复', calls: 3 };
  status.ai_queue = { waiting: 2, completed: 4, failed: 1 };
  status.ai_tasks = [{ id: 'mock-analysis', photo_id: 'mock-photo', state: 'waiting', kind: 'classification', attempts: 1, error: '等待服务恢复', short_code: 'TEST-1-101-01' }];
  await page.route('**/api/operations', route => route.fulfill({ json: status }));
  let retried = 0;
  await page.route('**/api/operations/model/retry', route => {
    retried += 1;
    status.model.state = 'unverified'; status.model.error = ''; status.model.retry_at = null;
    return route.fulfill({ json: status.model });
  });
  await page.goto('/');
  await page.getByRole('button', { name: /^查看运行状态/ }).click();
  const panel = page.getByRole('region', { name: '运行详情', exact: true });
  await expect(panel).toContainText('AI：暂缓');
  await expect(panel).toContainText('官网：待登录');
  await expect(panel.getByText('模型请求受限，等待服务恢复', { exact: true })).toBeVisible();
  for (const width of [1440, 1024, 768, 390, 320]) {
    await page.setViewportSize({ width, height: 1000 });
    const box = await panel.boundingBox();
    expect(box!.x).toBeGreaterThanOrEqual(0);
    expect(box!.x + box!.width).toBeLessThanOrEqual(width + 1);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`runtime-${width}.png`), fullPage: true });
  }
  await panel.getByRole('button', { name: '允许重新尝试', exact: true }).click();
  await expect(panel.getByRole('status')).toContainText('未重跑旧对话或发送消息');
  await expect(panel).toContainText('AI：待验证');
  expect(retried).toBe(1);
  expect(errors).toEqual([]);
});

test('with AI disabled an archived, confirmed photo prepares a local draft and exposes separate progress', async ({ page, request }, testInfo) => {
  const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAHgAAABkCAIAAADCEmNlAAAA80lEQVR4nO3SQQ3AMAADsazIC30oei+bQKRTvu2O906wgdAdj44IHRE6InRE6IjQEaEjQkeEjggdEToidEToiNARoSNCR4SOCB0ROiJ0ROiI0BGhI0JHhI4IHRE6InRE6IjQEaEjQkeEjggdEToidEToiNARoSNCR4SOCB0ROiJ0ROiI0BGhI0JHhI4IHRE6InRE6IjQEaEjQkeEjggdEToidEToiNARoSNCR4SOCB0ROiJ0ROiI0BGhI0JHhI4IHRE6InRE6IjQEaEjQkeEjggdEToidEToiNARoSNCR4SOCB0ROiJ0ROiI0BGhI0JHhF7jByp2AUhQ6fjGAAAAAElFTkSuQmCC', 'base64');
  await request.put('/api/settings/recognition', { headers, data: { local_ocr: false, vision_enabled: false } });
  const project = await (await request.post('/api/projects', { headers, data: { name: '故障恢复浏览器验证' } })).json();
  const c = await (await request.post('/api/cases', { headers, data: { project_id: project.id, title: '墙面维修', building: '1', room: '101' } })).json();
  await request.post(`/api/workflow/cases/${c.id}/result`, { headers, data: { action: 'accepted', revision: c.revision, event_at: '2026-10-17 08:00', note: '隔离浏览器验证' } });
  await request.post('/api/import/upload', { headers, multipart: { files: { name: '规则整理验证.png', mimeType: 'image/png', buffer: png }, auto_ocr: 'false', case_id: c.id } });
  let id = '';
  await expect.poll(async () => { const p = await (await request.get(`/api/photos?case_id=${c.id}`)).json(); id = p.items[0]?.id; return p.total; }).toBe(1);
  await request.patch(`/api/photos/${id}`, { headers, data: { photo_group: 'survey_problem', confirm: true } });
  await expect.poll(async () => (await (await request.get(`/api/photos/${id}`)).json()).processing.pipeline.state).toBe('local_draft');
  await page.goto('/');
  await page.locator('.utility-menu summary').click();
  await page.locator('.utility-menu').getByRole('button', { name: '全部照片', exact: true }).click();
  await page.getByRole('button', { name: '查看规则整理验证.png', exact: true }).click();
  const progress = page.getByRole('region', { name: '照片处理进度' });
  await expect(progress).toContainText('原件已归档 · 已归案');
  await expect(progress).toContainText('本地草稿已准备');
  await expect(progress).toContainText('AI：尚无后台分析记录');
  await page.screenshot({ path: testInfo.outputPath('photo-independent-progress.png'), fullPage: true });
  const state = await (await request.get('/api/operations')).json();
  expect(state.model.calls).toBe(0);
  expect(state.draft_states).toEqual({});
});
