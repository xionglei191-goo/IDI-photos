// Normal website login, including the website's SM2 encryption. Secrets travel only
// through parent/child pipes; no command arguments, profile, screenshot, or storageState.
import { pathToFileURL } from 'node:url';
import { chromium } from '../frontend/node_modules/playwright/index.mjs';

const LOGIN_URL = 'https://gzzb2021.com/idi/#/login';
const validToken = value => typeof value === 'string' && value.trim() && !['undefined', 'null'].includes(value.trim());

export class WebsiteLoginError extends Error {
  constructor(message, status, failure) { super(message); this.status = status; this.failure = failure; }
}

function rejected(message, password) {
  // Only the site's short user-facing message is returned, never its full response.
  let detail = typeof message === 'string' ? message : '';
  if (password) detail = detail.split(password).join('[已隐藏]');
  detail = detail.replace(/eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/g, '[已隐藏]')
    .replace(/\b[A-Za-z0-9_-]{48,}\b/g, '[已隐藏]').replace(/<[^>]*>/g, '').replace(/\s+/g, ' ').trim().slice(0, 240);
  return new WebsiteLoginError('官网登录未通过' + (detail ? '：' + detail : '，请核对账号密码或先在官网确认登录要求。'), 401, 'login_rejected');
}

async function readReceipt(response, timeout) {
  let timer;
  try {
    // Start reading as soon as headers arrive, before the page's success redirect.
    return await Promise.race([response.json().catch(() => null), new Promise(resolve => { timer = setTimeout(() => resolve(null), timeout); })]);
  } finally { clearTimeout(timer); }
}

export async function loginThroughWebsite(page, credentials, diagnostics, setPhase, { tokenTimeout = 20000, bodyTimeout = 2500 } = {}) {
  const isLogin = url => url.origin === 'https://gzzb2021.com' && url.pathname === '/iidi/login';
  page.on('pageerror', () => { diagnostics.scriptError = true; });
  page.on('request', request => { if (isLogin(new URL(request.url())) && request.method() === 'POST') diagnostics.requestSent = true; });
  setPhase('page');
  await page.goto(LOGIN_URL, { waitUntil: 'domcontentloaded', timeout: 40000 });
  setPhase('account_field');
  await page.getByPlaceholder('账号/手机号', { exact: true }).fill(credentials.username, { timeout: 30000 });
  await page.getByPlaceholder('输入密码', { exact: true }).fill(credentials.password);
  setPhase('login_response');
  const receiptPromise = page.waitForResponse(response => isLogin(new URL(response.url())) && response.request().method() === 'POST', { timeout: 30000 }).then(async response => {
    diagnostics.httpStatus = response.status();
    setPhase('token_response');
    return { status: response.status(), body: await readReceipt(response, bodyTimeout) };
  });
  const [, receipt] = await Promise.all([page.getByRole('button', { name: '登录', exact: true }).click(), receiptPromise]);
  if (receipt.status === 429) throw new WebsiteLoginError('官网暂时限制登录请求，请稍后再试。', 429, 'rate_limited');
  if (receipt.status === 401 || receipt.status === 403) throw new WebsiteLoginError('官网拒绝了登录请求，请先在官网核对账号状态或完成验证。', receipt.status, 'http_rejected');
  if (receipt.status >= 400) throw new WebsiteLoginError(`官网登录接口返回 HTTP ${receipt.status}，请稍后重试。`, 502, 'http_failure');
  const body = receipt.body;
  if (body && typeof body === 'object' && !Array.isArray(body)) {
    diagnostics.businessCode = Number.isSafeInteger(body.code) ? body.code : null;
    if (body.code !== undefined && body.code !== 0 && body.code !== '0') throw rejected(body.msg || body.message, credentials.password);
    if ((body.code === 0 || body.code === '0') && validToken(body.data?.token)) return body.data.token;
  }
  // Chromium may discard the response body during a navigation. In that case use
  // the website's normal session storage or the visible login rejection message.
  const state = await page.waitForFunction(() => {
    const token = sessionStorage.getItem('token');
    if (token && token.trim() && !['undefined', 'null'].includes(token.trim())) return { token };
    const error = document.querySelector('.el-message--error .el-message__content, .el-message--error');
    return error?.textContent?.trim() ? { error: error.textContent.trim() } : false;
  }, undefined, { timeout: tokenTimeout });
  const result = await state.jsonValue();
  await state.dispose();
  if (validToken(result.token)) return result.token;
  throw rejected(result.error, credentials.password);
}

async function main() {
  let input = '', browser, credentials;
  let phase = 'start', deadlineExpired = false;
  const diagnostics = { requestSent: false, httpStatus: null, businessCode: null, scriptError: false, failure: '' };
  const deadline = setTimeout(() => { deadlineExpired = true; if (browser) void browser.close().catch(() => {}); }, 85000);
  try {
    for await (const chunk of process.stdin) input += chunk;
    credentials = JSON.parse(input);
    input = '';
    phase = 'browser';
    browser = await chromium.launch({ channel: 'chrome', headless: true });
    const context = await browser.newContext();
    const page = await context.newPage();
    const token = await loginThroughWebsite(page, credentials, diagnostics, value => { phase = value; });
    process.stdout.write(JSON.stringify({ token }));
  } catch (error) {
    const timedOut = deadlineExpired || error?.name === 'TimeoutError';
    let message = '官网登录过程未完成，请稍后重试或先在官网确认登录要求。';
    let status = 502;
    if (error instanceof WebsiteLoginError) { message = error.message; status = error.status; diagnostics.failure = error.failure; }
    else if (phase === 'browser') { message = '官网登录浏览器未能启动，请检查本机 Chrome 是否可用。'; status = 503; diagnostics.failure = 'browser_unavailable'; }
    else if (timedOut) {
      status = 504;
      diagnostics.failure = phase === 'token_response' ? 'token_missing' : 'timeout';
      message = phase === 'token_response' ? '官网已响应，但未取得有效登录令牌或失败提示。请先在官网确认登录是否正常，再重试连接。' : '官网登录等待超时，请稍后重试或先打开官网检查网络。';
    } else diagnostics.failure = String(error?.message).includes('strict mode violation') ? 'ambiguous_login_control' : 'login_failed';
    process.stdout.write(JSON.stringify({ error: message, status, phase, diagnostics }));
    process.exitCode = 1;
  } finally {
    input = '';
    if (credentials) credentials.password = '';
    clearTimeout(deadline);
    if (browser) await browser.close().catch(() => {});
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) await main();
