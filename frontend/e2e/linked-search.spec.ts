import { test, expect } from '@playwright/test';

test('navigation and case lists share search across pagination, details, boards and mobile', async ({ page, request }, testInfo) => {
  const headers = { 'X-IDI-Request': '1' };
  const project = await (await request.post('/api/projects', { headers, data: { name: '搜索联动验收项目', short_prefix: 'LINKEDSEARCH' } })).json();
  const target = await (await request.post('/api/cases', { headers, data: {
    project_id: project.id, title: '渗漏联动目标', building: 'A3', unit: '2', room: '3001',
    platform_report_code: 'ZLBH202610031310420014', policy_number: 'POLICY-LINKED',
  } })).json();
  for (let i = 0; i < 31; i++) {
    const response = await request.post('/api/cases', { headers, data: {
      project_id: project.id, title: `其他搜索样例 ${i}`, building: 'B1', room: String(101 + i),
    } });
    expect(response.ok()).toBe(true);
  }
  const work = await (await request.post('/api/cases', { headers, data: {
    project_id: project.id, title: '查勘搜索样例', building: 'A3', room: '3002', platform_report_code: 'ZLBH202610171710420014',
  } })).json();
  const accepted = await request.post(`/api/workflow/cases/${work.id}/result`, { headers, data: {
    action: 'accepted', revision: work.revision, event_at: '2026-10-17 09:00', note: '隔离浏览器搜索验证',
  } });
  expect(accepted.ok()).toBe(true);

  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/');
  await page.getByRole('button', { name: '本地案件', exact: true }).click();
  await page.locator('.project-button').filter({ hasText: project.name }).click();
  const quick = page.getByRole('textbox', { name: '快速定位案件', exact: true });
  const search = page.getByRole('textbox', { name: '搜索案件', exact: true });
  const rows = page.locator('.board-table [data-case-id]');
  const tree = page.locator('.case-tree button');
  const total = page.locator('.board-total');
  const nav = page.getByRole('navigation', { name: '工作看板' });
  await expect(total).toHaveText('当前显示 32 案');
  await page.getByRole('button', { name: '下一页案件', exact: true }).click();
  await expect(page.locator('.pagination .row')).toContainText('2 / 2');
  await expect(rows).toHaveCount(2);

  await quick.fill('20014');
  await expect(search).toHaveValue('20014');
  await expect(total).toHaveText('当前显示 1 案');
  await expect(rows).toHaveCount(1);
  await expect(rows).toHaveAttribute('data-case-id', target.id);
  await expect(tree).toHaveCount(1);
  await expect(tree).toContainText(target.short_code);
  await expect(page.locator('.pagination .row')).toContainText('1 / 1');
  await page.screenshot({ path: testInfo.outputPath('linked-search-desktop.png'), fullPage: true });

  await rows.locator('.case-link').click();
  await expect(page.getByRole('heading', { name: target.short_code, exact: true })).toBeVisible();
  await page.getByRole('button', { name: '返回看板', exact: true }).click();
  await expect(quick).toHaveValue('20014');
  await expect(search).toHaveValue('20014');
  await expect(rows).toHaveCount(1);
  await expect(rows).toHaveAttribute('data-case-id', target.id);

  // The unit in the short code means A3-3001 must match the location fields.
  await search.fill('');
  await expect(total).toHaveText('当前显示 32 案');
  await search.fill('  a3-3001  ');
  await expect(quick).toHaveValue('  a3-3001  ');
  await expect(rows).toHaveCount(1);
  await expect(rows).toHaveAttribute('data-case-id', target.id);
  await expect(tree).toHaveCount(1);
  await search.fill('3001 渗漏联动目标');
  await expect(quick).toHaveValue('3001 渗漏联动目标');
  await expect(rows).toHaveCount(0);
  await expect(tree).toHaveCount(0);
  await expect(total).toHaveText('当前显示 0 案');
  await expect(page.getByText('没有符合当前条件的案件', { exact: true })).toBeVisible();

  await quick.fill('');
  await expect(search).toHaveValue('');
  await expect(total).toHaveText('当前显示 32 案');
  await expect(rows).toHaveCount(30);
  await search.fill('20014');
  await expect(quick).toHaveValue('20014');
  await expect(total).toHaveText('当前显示 1 案');
  await nav.getByRole('button', { name: '查勘维修', exact: true }).click();
  await expect(search).toHaveValue('20014');
  await expect(rows).toHaveCount(1);
  await expect(rows).toHaveAttribute('data-case-id', work.id);
  await expect(tree).toHaveCount(1);
  await expect(tree).toContainText(work.short_code);
  await search.fill('');
  await expect(quick).toHaveValue('');
  await expect(rows).toHaveAttribute('data-case-id', work.id);
  await search.fill('20014');
  await page.getByRole('button', { name: '官网清单', exact: true }).click();
  await expect(quick).toHaveValue('');
  await expect(search).toHaveValue('');
  await expect(total).toHaveText('当前显示 0 案');
  await expect(tree).toHaveCount(0);

  await page.getByRole('button', { name: '本地案件', exact: true }).click();
  await nav.getByRole('button', { name: '理赔受理', exact: true }).click();
  await page.setViewportSize({ width: 375, height: 844 });
  await page.getByRole('button', { name: '展开快速导航', exact: true }).click();
  await quick.fill('20014');
  await expect(tree).toHaveCount(1);
  await page.getByRole('button', { name: '展开快速导航', exact: true }).click();
  await expect(search).toHaveValue('20014');
  await expect(rows).toHaveCount(1);
  await expect(rows).toHaveAttribute('data-case-id', target.id);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('linked-search-mobile.png'), fullPage: true });
  expect(errors).toEqual([]);
});
