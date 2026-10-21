import { test, expect, type APIRequestContext } from '@playwright/test';

const headers = { 'X-IDI-Request': '1' };

async function makeProject(request: APIRequestContext) {
  const project = await (await request.post('/api/projects', {
    headers, data: { name: '跟进导航验证' + Date.now().toString(36).toUpperCase() },
  })).json();
  const item = await (await request.post('/api/cases', {
    headers, data: { project_id: project.id, title: '厨房渗漏', building: '1', room: '1201' },
  })).json();
  const result = await request.post(`/api/workflow/cases/${item.id}/result`, {
    headers, data: { action: 'accepted', revision: item.revision, event_at: '2026-10-20 10:00', note: '隔离导航验证' },
  });
  expect(result.ok()).toBe(true);
  return { project, item };
}

test('project followup keeps case and owner actions, with records available on demand', async ({ page, request }, testInfo) => {
  const { project, item } = await makeProject(request);
  const writes: string[] = [], errors: string[] = [], issueReads: string[] = [];
  page.on('request', r => {
    if (r.url().includes('/api/') && !['GET', 'HEAD'].includes(r.method())) writes.push(r.method() + ' ' + r.url());
    if (new URL(r.url()).pathname === '/api/followup/issues') issueReads.push(r.url());
  });
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/?view=followup');
  const nav = page.getByRole('navigation', { name: '项目跟进导航' });
  await expect(nav.getByRole('button')).toHaveText(['项目概览', '群助手']);
  await expect(page.getByRole('button', { name: '案件智能审阅', exact: true })).toHaveCount(0);
  const card = page.locator('.followup-project').filter({ hasText: project.name });
  await expect(card).toContainText('1 个跟进案件');
  await expect(card).not.toContainText('内部归档提示');
  await expect(card).not.toContainText('AI 已检查');
  expect(issueReads).toEqual([]);

  await card.getByRole('button', { name: `绑定负责人 · ${project.name}`, exact: true }).click();
  const contact = page.getByRole('dialog', { name: `项目负责人 · ${project.name}` });
  await expect(contact).toBeVisible();
  await contact.getByRole('button', { name: '取消', exact: true }).click();

  const tools = page.locator('.followup-tools');
  await tools.locator('summary').focus();
  await page.keyboard.press('Enter');
  await expect(tools.getByRole('button', { name: '照片检查记录', exact: true })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(tools).not.toHaveAttribute('open');
  await expect(tools.locator('summary')).toBeFocused();
  await tools.locator('summary').click();
  await tools.getByRole('button', { name: '照片检查记录', exact: true }).click();
  await expect(page.getByRole('heading', { name: '照片检查记录', exact: true })).toBeVisible();
  await expect.poll(() => issueReads.length).toBeGreaterThan(0);
  await expect(page.getByRole('button', { name: /立即检查|检查进行中/ })).toBeVisible();
  await page.getByRole('combobox', { name: '问题类型', exact: true }).selectOption('missing');
  await expect(page.getByRole('combobox', { name: '问题类型', exact: true })).toHaveValue('missing');
  await page.getByRole('button', { name: '返回项目概览', exact: true }).click();

  for (const width of [1440, 390, 320]) {
    await page.setViewportSize({ width, height: 1000 });
    await tools.locator('summary').click();
    await expect(tools.getByRole('button', { name: '群反馈记录', exact: true })).toBeVisible();
    const panel = await page.locator('.followup-tools-panel').boundingBox();
    expect(panel!.x).toBeGreaterThanOrEqual(0);
    expect(panel!.x + panel!.width).toBeLessThanOrEqual(width);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await tools.getByRole('button', { name: '群反馈记录', exact: true }).click();
    await expect(page.getByRole('heading', { name: '群反馈记录', exact: true })).toBeVisible();
    await page.getByRole('button', { name: '返回项目概览', exact: true }).click();
    await page.screenshot({ path: testInfo.outputPath(`followup-${width}.png`), fullPage: true });
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  await card.getByRole('button', { name: '查看案件', exact: true }).click();
  await page.locator(`[data-case-id="${item.id}"] .case-link`).click();
  await expect(page.getByRole('button', { name: '智能审阅', exact: true })).toBeVisible();
  expect(writes).toEqual([]);
  expect(errors).toEqual([]);
});

test('failed and uncertain deliveries stay actionable outside the secondary history page', async ({ page, request }) => {
  const { project } = await makeProject(request);
  const writes: string[] = [], errors: string[] = [];
  await page.route('**/api/followup/feedback', route => route.fulfill({
    json: ['failed', 'unknown', 'sent'].map((state, index) => ({
      id: `delivery-${index}`, project_name: project.name, name: '负责人', text: `具体照片问题 ${index}`,
      state, error: state === 'failed' ? '测试发送失败' : null, created_at: '2026-10-20T02:00:00Z',
      sent_at: state === 'sent' ? '2026-10-20T02:01:00Z' : null, remote_message_id: null,
    })),
  }));
  page.on('request', r => { if (r.url().includes('/api/') && !['GET', 'HEAD'].includes(r.method())) writes.push(r.url()); });
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/?view=followup');
  await expect(page.getByRole('status').filter({ hasText: '2 条群反馈' })).toBeVisible();
  await page.getByRole('button', { name: '处理发送问题', exact: true }).click();
  await expect(page.getByRole('heading', { name: '群反馈记录', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '重试反馈', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '核对群内送达情况', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '返回项目概览', exact: true }).click();
  await expect(page.getByRole('button', { name: '处理发送问题', exact: true })).toBeVisible();
  expect(writes).toEqual([]);
  expect(errors).toEqual([]);
});
