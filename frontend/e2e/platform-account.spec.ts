import { test, expect } from '@playwright/test';
import type { PlatformAccountStatus } from '../src/platformTypes';

const empty = (): PlatformAccountStatus => ({ configured: false, username: '', password_stored: false, auto_connect: false, state: 'unconfigured', last_attempt_at: null, last_success_at: null, next_retry_at: null, error: '', session: { connected: false, account: '', mode: 'read_only', connected_at: null, write_enabled: false } });

test('system account saves once, reconnects without exposing passwords, and keeps unsaved changes', async ({ page }, testInfo) => {
  let state = empty();
  const saves: { username: string; password: string; auto_connect: boolean }[] = [];
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/api/platform/account', async route => {
    if (route.request().method() === 'PUT') {
      const data = route.request().postDataJSON(); saves.push(data);
      state = { ...state, configured: true, username: data.username.trim(), password_stored: true, auto_connect: data.auto_connect, state: data.auto_connect ? 'connecting' : 'disabled' };
    } else if (route.request().method() === 'DELETE') state = empty();
    await route.fulfill({ json: state });
  });
  await page.route('**/api/platform/session', async route => {
    if (route.request().method() === 'DELETE') state = { ...state, auto_connect: false, state: 'disabled', session: { ...state.session, connected: false, account: '' } };
    await route.fulfill({ json: state.session });
  });
  await page.goto('/');
  await page.getByRole('button', { name: '系统设置', exact: true }).click();
  const nav = page.getByRole('navigation', { name: '设置分类' });
  await nav.getByRole('button', { name: '连接与收图', exact: true }).click();
  const panel = page.getByRole('region', { name: '系统账号设置', exact: true });
  await expect(panel).toContainText('尚未设置');
  const username = panel.getByLabel('系统账号', { exact: true }), password = panel.getByLabel('系统密码', { exact: true });
  await username.fill('isolated-test-user');
  await password.fill('isolated-test-password');
  await nav.getByRole('button', { name: '自动处理', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('系统账号设置尚未保存');
  await expect(password).toHaveValue('isolated-test-password');
  await panel.getByRole('button', { name: '显示输入的密码', exact: true }).click();
  await expect(password).toHaveAttribute('type', 'text');
  await panel.getByRole('button', { name: '隐藏输入的密码', exact: true }).click();
  await expect(password).toHaveAttribute('type', 'password');
  await panel.getByRole('button', { name: '保存并自动连接', exact: true }).click();
  await expect(panel.getByRole('status')).toHaveText('正在连接官网');
  await expect(password).toHaveValue('');
  expect(saves).toEqual([{ username: 'isolated-test-user', password: 'isolated-test-password', auto_connect: true }]);
  state = { ...state, state: 'connected', last_success_at: '2026-10-17T12:00:00Z', session: { ...state.session, connected: true, account: state.username } };
  await expect(panel.getByRole('status')).toHaveText('官网已连接');
  await expect(password).toHaveValue('');
  await username.fill('another-user');
  await panel.getByRole('button', { name: '保存并自动连接', exact: true }).click();
  await expect(panel).toContainText('首次设置或更换账号时，请填写对应密码。');
  expect(saves).toHaveLength(1);
  await panel.getByRole('button', { name: '撤销账号修改', exact: true }).click();
  for (const width of [1440, 390, 320]) {
    await page.setViewportSize({ width, height: 1000 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await expect(username).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath(`system-account-${width}.png`), fullPage: true });
  }
  await panel.getByRole('button', { name: '断开并暂停自动连接', exact: true }).click();
  await expect(panel.getByRole('status')).toHaveText('自动连接已暂停');
  await page.reload();
  await page.getByRole('button', { name: '系统设置', exact: true }).click();
  await nav.getByRole('button', { name: '连接与收图', exact: true }).click();
  await expect(username).toHaveValue('isolated-test-user');
  await expect(password).toHaveValue('');
  expect(await page.evaluate(() => JSON.stringify([localStorage, sessionStorage]))).not.toContain('isolated-test-password');
  await panel.getByRole('button', { name: '移除系统账号', exact: true }).click();
  await panel.getByRole('button', { name: '确认移除', exact: true }).click();
  await expect(panel.getByRole('status')).toHaveText('尚未设置');
  expect(errors).toEqual([]);
});

test('stored account shows rejection and manual retry status without asking for password again', async ({ page }) => {
  let state: PlatformAccountStatus = { ...empty(), configured: true, username: 'isolated-test-user', password_stored: true, auto_connect: true, state: 'needs_attention', error: '官网登录未完成，请核对账号密码、官网验证或本机浏览器后手动重试。' };
  let connects = 0;
  await page.route('**/api/platform/account', route => route.fulfill({ json: state }));
  await page.route('**/api/platform/account/connect', route => { connects++; state = { ...state, state: 'connecting', error: '' }; return route.fulfill({ json: state }); });
  await page.goto('/');
  await page.getByRole('button', { name: '系统设置', exact: true }).click();
  await page.getByRole('navigation', { name: '设置分类' }).getByRole('button', { name: '连接与收图', exact: true }).click();
  const panel = page.getByRole('region', { name: '系统账号设置', exact: true });
  await expect(panel.getByRole('alert')).toContainText('核对账号密码');
  await panel.getByRole('button', { name: '立即连接', exact: true }).click();
  await expect(panel.getByRole('status')).toHaveText('正在连接官网');
  expect(connects).toBe(1);
  state = { ...state, state: 'retry_wait', next_retry_at: '2026-10-18T03:00:00Z', error: '官网暂时无法连接，将按重试时间再次尝试。' };
  await expect(panel.getByRole('status')).toHaveText('等待自动重试');
  await expect(panel).toContainText('下次重试');
  await expect(panel.getByLabel('系统密码', { exact: true })).toHaveValue('');
});
test('temporary service outage clears after the account status recovers', async ({ page }) => {
  let available = false;
  const state: PlatformAccountStatus = { ...empty(), configured: true, username: 'isolated-test-user', password_stored: true, auto_connect: true, state: 'connected', session: { ...empty().session, connected: true, account: 'isolated-test-user' } };
  await page.route('**/api/platform/account', route => available ? route.fulfill({ json: state }) : route.abort('failed'));
  await page.goto('/');
  await page.getByRole('button', { name: '系统设置', exact: true }).click();
  await page.getByRole('navigation', { name: '设置分类' }).getByRole('button', { name: '连接与收图', exact: true }).click();
  const panel = page.getByRole('region', { name: '系统账号设置', exact: true });
  await expect(panel.getByRole('alert')).toContainText('暂时无法读取系统账号状态');
  available = true;
  await expect(panel.getByRole('status')).toHaveText('官网已连接');
  await expect(panel.getByRole('alert')).toHaveCount(0);
});
