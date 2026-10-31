import { test, expect, type APIRequestContext, type Page } from '@playwright/test';
import type { PlatformAccountStatus, PlatformConnectionIssue } from '../src/platformTypes';

async function prepare(page: Page, request: APIRequestContext) {
  const auth = await (await request.get('/api/auth/status')).json();
  const login = await request.post(auth.setup_required ? '/api/auth/setup' : '/api/auth/login', {
    headers: { 'X-IDI-Request': '1' },
    data: { username: 'upload-ui-admin', password: 'isolated-upload-ui-password', display_name: '隔离测试管理员' },
  });
  expect(login.ok()).toBeTruthy();
  await page.context().addCookies((await request.storageState()).cookies);
}

const paused = (): PlatformAccountStatus => ({
  configured: true, username: 'isolated-saved-account', password_stored: true, auto_connect: true,
  enabled: true, state: 'needs_attention', failures: 4, max_attempts: 4,
  error: '官网已响应，但暂未取得有效登录令牌。 连续 4 次未成功，已暂停自动重试；请检查官网后点击“立即连接”。',
  last_attempt_at: '2026-10-30T12:08:00Z', last_success_at: '2026-10-30T11:00:00Z',
  first_failure_at: '2026-10-30T12:00:00Z', blocked_at: '2026-10-30T12:08:00Z',
  blocked_reason: 'retry_limit', next_retry_at: null,
  session: { connected: false, account: '', mode: 'read_only', connected_at: null, write_enabled: false },
});

test('paused project connection is visible on the dashboard and links to the correct account', async ({ page, request }, testInfo) => {
  await prepare(page, request);
  const response = await request.post('/api/projects', {
    headers: { 'X-IDI-Request': '1' }, data: { name: '连接重试验收 A 地块' },
  });
  expect(response.ok()).toBeTruthy();
  const project = await response.json();
  let state = paused();
  let issues: PlatformConnectionIssue[] = [{ ...state, project_id: project.id, name: project.name,
    failures: 4, max_attempts: 4, first_failure_at: state.first_failure_at!, blocked_at: state.blocked_at!,
    blocked_reason: state.blocked_reason! }];
  const operations = await (await request.get('/api/operations')).json();
  const endpoint = '/api/access/project-accounts/' + project.id;
  let connects = 0;
  const errors: string[] = [], writes: string[] = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('request', r => { if (r.url().includes('/api/') && !['GET', 'HEAD'].includes(r.method())) writes.push(new URL(r.url()).pathname); });
  await page.route('**/api/operations', route => route.fulfill({ json: { ...operations, connection_issues: issues } }));
  await page.route('**' + endpoint, route => route.fulfill({ json: state }));
  await page.route('**' + endpoint + '/connect', route => {
    connects++; state = { ...state, state: 'connecting', error: '', failures: 0, blocked_at: null };
    return route.fulfill({ json: state });
  });
  await page.goto('/');
  const strip = page.getByRole('region', { name: '运行状态', exact: true });
  const alert = strip.getByRole('status', { name: '官网连接提醒', exact: true });
  await expect(alert).toContainText(project.name);
  await expect(alert).toContainText('连续 4 次未成功');
  await expect(strip.getByRole('button', { name: '查看运行状态，有待处理事项', exact: true })).toBeVisible();
  for (const width of [1440, 390, 320]) {
    await page.setViewportSize({ width, height: 1000 });
    await expect(alert.getByRole('button', { name: '处理连接 · ' + project.name })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`connection-alert-${width}.png`), fullPage: true });
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  await alert.getByRole('button', { name: '处理连接 · ' + project.name }).click();
  await expect(page.locator('.settings-section-heading h2')).toHaveText('人员与权限');
  const panel = page.getByRole('region', { name: '系统账号设置', exact: true });
  await expect(panel).toContainText('isolated-saved-account');
  await expect(panel).toContainText('本轮已尝试 4/4 次');
  await expect(panel).toContainText('暂停自动重试时间');
  await expect(panel.getByLabel('系统密码', { exact: true })).toHaveCount(0);
  await panel.getByRole('button', { name: '立即连接', exact: true }).click();
  await expect(panel.getByRole('status')).toHaveText('正在连接官网');
  expect(connects).toBe(1);
  state = { ...state, state: 'retry_wait', failures: 1, next_retry_at: '2026-10-30T12:20:00Z',
    error: '官网登录等待超时。 将按计划重试（本轮已尝试 1/4 次）。' };
  await expect(panel.getByRole('status')).toHaveText('等待自动重试');
  await expect(panel).toContainText('下次重试');
  state = { ...state, state: 'connected', failures: 0, error: '', next_retry_at: null,
    session: { ...state.session, connected: true, account: state.username } };
  issues = [];
  await expect(panel.getByRole('status')).toHaveText('官网已连接');
  await expect(alert).toHaveCount(0);
  expect(writes).toEqual([endpoint + '/connect']);
  expect(errors).toEqual([]);
});

test('company connection warning opens saved company settings without an automatic login request', async ({ page, request }) => {
  await prepare(page, request);
  const state = { ...paused(), blocked_reason: 'manual_required', failures: 1,
    error: '官网明确拒绝账号或密码，请核对保存的账号信息。 自动重试已暂停，请处理后点击“立即连接”。' };
  const operations = await (await request.get('/api/operations')).json();
  const writes: string[] = [];
  page.on('request', r => { if (r.url().includes('/api/') && !['GET', 'HEAD'].includes(r.method())) writes.push(r.url()); });
  await page.route('**/api/operations', route => route.fulfill({ json: { ...operations,
    connection_issues: [{ ...state, project_id: null, name: '公司官网读取账号' }] } }));
  await page.route('**/api/platform/account', route => route.fulfill({ json: state }));
  await page.goto('/');
  await page.getByRole('button', { name: '处理连接 · 公司官网读取账号' }).click();
  await expect(page.locator('.settings-section-heading h2')).toHaveText('连接与收图');
  const panel = page.getByRole('region', { name: '系统账号设置', exact: true });
  await expect(panel).toContainText('明确拒绝账号或密码');
  await expect(panel.getByRole('status')).toHaveText('需要处理');
  expect(writes).toEqual([]);
});
