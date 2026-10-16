// Normal website login, including the website's SM2 encryption. Secrets travel only
// through parent/child pipes; no command arguments, profile, screenshot, or storageState.
import { chromium } from '../frontend/node_modules/playwright/index.mjs';

let input = '';
for await (const chunk of process.stdin) input += chunk;
let browser;
let phase = 'start';
const diagnostics = { requestSent: false, httpStatus: null, scriptError: false, failure: '' };
const deadline = setTimeout(() => { if (browser) void browser.close().catch(() => {}); }, 85000);
try {
  const credentials = JSON.parse(input);
  input = '';
  phase = 'browser';
  browser = await chromium.launch({ channel: 'chrome', headless: true });
  const context = await browser.newContext();
  const page = await context.newPage();
  page.on('pageerror', () => { diagnostics.scriptError = true; });
  page.on('request', request => { if (new URL(request.url()).pathname === '/iidi/login') diagnostics.requestSent = true; });
  phase = 'page';
  await page.goto('https://gzzb2021.com/idi/#/login', { waitUntil: 'domcontentloaded', timeout: 40000 });
  phase = 'account_field';
  await page.getByPlaceholder('账号/手机号', { exact: true }).fill(credentials.username, { timeout: 30000 });
  await page.getByPlaceholder('输入密码', { exact: true }).fill(credentials.password);
  credentials.password = '';
  phase = 'login_response';
  const responsePromise = page.waitForResponse(r => {
    const url = new URL(r.url());
    return url.origin === 'https://gzzb2021.com' && url.pathname === '/iidi/login' && r.request().method() === 'POST';
  }, { timeout: 30000 });
  const [, response] = await Promise.all([page.getByRole('button', { name: '登录', exact: true }).click(), responsePromise]);
  diagnostics.httpStatus = response.status();
  phase = 'token_response';
  // The website redirects immediately after its success handler stores the token.
  // Reading the response body across that navigation is unreliable in Chromium.
  await page.waitForFunction(() => Boolean(sessionStorage.getItem('token')), undefined, { timeout: 20000 });
  const token = await page.evaluate(() => sessionStorage.getItem('token'));
  if (typeof token !== 'string' || !token) throw new Error('login_rejected');
  process.stdout.write(JSON.stringify({ token }));
} catch (error) {
  diagnostics.failure = error?.name === 'TimeoutError' ? 'timeout' : String(error?.message).includes('strict mode violation') ? 'ambiguous_login_control' : 'login_failed';
  process.stdout.write(JSON.stringify({ error: '官网登录未成功，请核对账号密码、网络或官网登录要求。', phase, diagnostics }));
  process.exitCode = 1;
} finally {
  clearTimeout(deadline);
  if (browser) await browser.close().catch(() => {});
}
