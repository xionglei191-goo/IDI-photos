import { test, expect } from '@playwright/test';

test('login shows the official rejection and handles an HTML gateway error', async ({ page }, testInfo) => {
  const errors: string[] = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.goto('/');
  await page.getByRole('button', { name: '官网清单', exact: true }).click();
  await page.getByRole('button', { name: '手动同步', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '连接官网', exact: true });
  await expect(dialog).toBeVisible();
  await page.route('**/api/platform/session', route => route.request().method() === 'POST'
    ? route.fulfill({ status: 401, json: { detail: '官网登录未通过：账号或密码错误' } }) : route.continue());
  await dialog.getByLabel('官网账号', { exact: true }).fill('isolated-test-account');
  await dialog.getByLabel('官网密码', { exact: true }).fill('isolated-test-password');
  await dialog.getByRole('button', { name: '连接官网', exact: true }).click();
  await expect(dialog).toContainText('官网登录未通过：账号或密码错误');
  await expect(dialog.getByLabel('官网密码', { exact: true })).toHaveValue('');
  await expect(dialog.getByLabel('官网账号', { exact: true })).toHaveValue('isolated-test-account');
  await page.unroute('**/api/platform/session');
  await page.route('**/api/platform/session', route => route.request().method() === 'POST'
    ? route.fulfill({ status: 502, contentType: 'text/html', body: '<h1>Bad Gateway</h1>' }) : route.continue());
  await dialog.getByLabel('官网密码', { exact: true }).fill('isolated-test-password');
  await dialog.getByRole('button', { name: '连接官网', exact: true }).click();
  await expect(dialog).toContainText('连接官网时网关未返回有效结果（502）');
  await expect(dialog).not.toContainText('Bad Gateway');
  await expect(dialog.getByLabel('官网密码', { exact: true })).toHaveValue('');
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await expect(dialog).toBeInViewport();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`login-error-${width}.png`) });
  }
  expect(errors).toEqual([]);
});
