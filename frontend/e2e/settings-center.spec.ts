import { test, expect } from '@playwright/test';

test('settings and task details have one organized home without changing configuration while browsing', async ({ page }, testInfo) => {
  const writes: string[] = [], errors: string[] = [];
  page.on('request', request => { if (request.url().includes('/api/') && !['GET', 'HEAD'].includes(request.method())) writes.push(request.method() + ' ' + new URL(request.url()).pathname); });
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/?view=followup');
  await expect(page.getByRole('heading', { name: '项目跟进', exact: true })).toBeVisible();
  await expect(page.getByRole('region', { name: '运行详情', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: /^后台保存/ })).toHaveCount(0);
  await expect(page.getByRole('checkbox', { name: '启用自动上传', exact: true })).toHaveCount(0);
  await page.locator('.utility-menu summary').click();
  await expect(page.locator('.utility-menu').getByRole('button')).toHaveText(['全部照片', '报送记录']);
  await page.locator('.utility-menu summary').click();
  await page.getByRole('button', { name: '系统设置', exact: true }).click();
  const nav = page.getByRole('navigation', { name: '设置分类', exact: true });
  await expect(nav.getByRole('button')).toHaveCount(6);
  await expect(page.getByRole('heading', { name: '系统设置', exact: true })).toBeVisible();
  await expect(page.getByRole('region', { name: '自动保存设置', exact: true })).toBeVisible();
  await expect(page.getByRole('checkbox', { name: '自动规则整理', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '调整项目检查', exact: true }).click();
  const checkConfig = page.getByRole('dialog', { name: '项目检查设置', exact: true });
  await expect(checkConfig).toBeVisible();
  await checkConfig.getByRole('button', { name: '取消', exact: true }).click();
  for (const width of [1440, 390, 320]) {
    await page.setViewportSize({ width, height: 1000 });
    for (const section of ['连接与收图', '自动处理', '识别与模型', '项目与资料', '后台任务', '备份恢复']) {
      await nav.getByRole('button', { name: section, exact: true }).click();
      await expect(page.locator('.settings-section-heading h2')).toHaveText(section);
      await expect(page.getByRole('heading', { level: 1 })).toHaveCount(1);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    }
    await nav.getByRole('button', { name: '自动处理', exact: true }).click();
    await expect(page.getByRole('checkbox', { name: '启用自动上传', exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: /自动同步/ })).toBeEnabled();
    await expect(page.getByRole('checkbox', { name: '自动规则整理', exact: true })).toBeEnabled();
    await expect(page.getByRole('button', { name: '调整项目检查', exact: true })).toBeEnabled();
    await page.screenshot({ path: testInfo.outputPath(`settings-automatic-${width}.png`), fullPage: true });
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  await nav.getByRole('button', { name: '后台任务', exact: true }).click();
  await page.getByRole('group', { name: '后台任务分类' }).getByRole('button', { name: '运行详情', exact: true }).click();
  const runtime = page.getByRole('region', { name: '运行详情', exact: true });
  await expect(runtime).toBeVisible();
  await expect(runtime.getByRole('checkbox')).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath('settings-runtime.png'), fullPage: true });
  await page.getByRole('button', { name: '返回项目跟进', exact: true }).click();
  await expect(page.getByRole('heading', { name: '项目跟进', exact: true })).toBeVisible();
  expect(writes).toEqual([]);
  expect(errors).toEqual([]);
});

test('unsaved model settings stay available when navigating categories or leaving settings', async ({ page }) => {
  const saved: unknown[] = [];
  await page.route('**/api/settings/recognition', route => { saved.push(route.request().postDataJSON()); return route.fulfill({ status: 409, json: { detail: '设置尚未保存' } }); });
  await page.goto('/');
  await page.getByRole('button', { name: '系统设置', exact: true }).click();
  const nav = page.getByRole('navigation', { name: '设置分类', exact: true });
  await nav.getByRole('button', { name: '识别与模型', exact: true }).click();
  const field = page.getByRole('textbox', { name: '模型名称', exact: true });
  const before = await field.inputValue();
  await field.fill('unsaved-example-model');
  await nav.getByRole('button', { name: '连接与收图', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('识别设置尚未保存');
  await expect(field).toHaveValue('unsaved-example-model');
  await page.getByRole('button', { name: '返回工作台', exact: true }).click();
  await expect(field).toHaveValue('unsaved-example-model');
  expect(saved).toEqual([]);
  await page.getByRole('button', { name: '撤销修改', exact: true }).click();
  await expect(field).toHaveValue(before);
  await nav.getByRole('button', { name: '连接与收图', exact: true }).click();
  await expect(page.locator('.settings-section-heading h2')).toHaveText('连接与收图');
});
