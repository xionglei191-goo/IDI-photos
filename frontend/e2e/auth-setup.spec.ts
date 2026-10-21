import { test, expect } from '@playwright/test';

test('remote first visit explains the management entry without accepting credentials', async ({ page }) => {
  let initialized = false;
  const setupPosts: string[] = [];
  page.on('request', request => {
    if (request.method() === 'POST' && new URL(request.url()).pathname === '/api/auth/setup') {
      setupPosts.push(request.url());
    }
  });
  await page.route('**/api/auth/status', route => route.fulfill({
    json: { setup_required: !initialized, setup_allowed: false },
  }));
  await page.goto('/');
  await expect(page.getByRole('heading', { name: '等待管理员初始化' })).toBeVisible();
  await expect(page.getByText('当前访问地址不能创建首位管理员。', { exact: false })).toBeVisible();
  await expect(page.getByLabel('人员账号')).toHaveCount(0);
  await expect(page.getByLabel('密码', { exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '创建管理员并进入' })).toHaveCount(0);
  initialized = true;
  await page.getByRole('button', { name: '已完成初始化，刷新登录' }).click();
  await expect(page.getByRole('heading', { name: '登录工作台' })).toBeVisible();
  await expect(page.getByLabel('人员账号')).toBeVisible();
  await expect(page.getByLabel('密码', { exact: true })).toBeVisible();
  expect(setupPosts).toEqual([]);
});

test('the permitted local entry offers the administrator form', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: '创建系统管理员' })).toBeVisible();
  await expect(page.getByText('当前为本机管理入口。', { exact: false })).toBeVisible();
  await expect(page.getByLabel('人员账号')).toBeVisible();
  await expect(page.getByLabel('姓名')).toBeVisible();
  await expect(page.getByLabel('密码', { exact: true })).toHaveAttribute('minlength', '10');
  await expect(page.getByRole('button', { name: '创建管理员并进入' })).toBeEnabled();
});
