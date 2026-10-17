import { after, before, test } from 'node:test';
import assert from 'node:assert/strict';
import { chromium } from '../../frontend/node_modules/playwright/index.mjs';
import { loginThroughWebsite, WebsiteLoginError } from '../platform-login.mjs';

let browser;
before(async () => { browser = await chromium.launch({ channel: 'chrome', headless: true }); });
after(async () => { await browser?.close(); });

// Every browser request is intercepted. These tests never contact the real site.
async function fixture(t, { body, status = 200, behavior = 'none', unreadable = false }) {
  const context = await browser.newContext();
  t.after(() => context.close());
  const requests = [];
  await context.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.origin !== 'https://gzzb2021.com') return route.abort();
    if (url.pathname === '/iidi/login' && route.request().method() === 'POST') {
      requests.push(route.request().postDataJSON());
      return route.fulfill({ status, json: body });
    }
    if (url.pathname === '/idi/home') return route.fulfill({ contentType: 'text/html', body: '<h1>已登录</h1>' });
    if (url.pathname !== '/idi/') return route.abort();
    return route.fulfill({ contentType: 'text/html', body: `<!doctype html><meta charset="utf-8">
      <input placeholder="账号/手机号"><input type="password" placeholder="输入密码"><button>登录</button>
      <script>document.querySelector('button').onclick = async () => {
        const response = await fetch('/iidi/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({loginName:'mock-encrypted-user',password:'mock-encrypted-password'}) });
        const data = await response.json();
        if (${JSON.stringify(behavior)} === 'redirect') { sessionStorage.setItem('token', data.data.token); location.href='/idi/home'; }
        if (${JSON.stringify(behavior)} === 'error') { const el=document.createElement('div'); el.className='el-message--error'; el.textContent='账户已停用，请联系管理员'; document.body.append(el); }
      };</script>` });
  });
  const page = await context.newPage();
  if (unreadable) {
    const wait = page.waitForResponse.bind(page);
    page.waitForResponse = async (...args) => {
      const response = await wait(...args);
      return { status: () => response.status(), json: async () => { throw new Error('body lost during navigation'); } };
    };
  }
  const diagnostics = { requestSent: false, httpStatus: null, businessCode: null, scriptError: false };
  const phases = [];
  const run = () => loginThroughWebsite(page, { username: 'test-account', password: 'private-test-password' }, diagnostics, p => phases.push(p), { tokenTimeout: 600, bodyTimeout: 400 });
  return { run, diagnostics, phases, requests, page };
}

test('HTTP 200 business rejection returns the actual reason instead of a token timeout', async t => {
  const f = await fixture(t, { body: { code: 1, msg: '账号或密码错误' } });
  await assert.rejects(f.run(), error => error instanceof WebsiteLoginError && error.status === 401 && error.message === '官网登录未通过：账号或密码错误');
  assert.equal(f.diagnostics.httpStatus, 200);
  assert.equal(f.diagnostics.businessCode, 1);
  assert.equal(f.requests.length, 1);
  assert.ok(!JSON.stringify(f.requests).includes('private-test-password'));
});

test('successful receipt does not depend on a later DOM token read', async t => {
  const f = await fixture(t, { body: { code: 0, data: { token: 'test-response-token', user: { private: 'not-returned' } } } });
  assert.equal(await f.run(), 'test-response-token');
  assert.equal(await f.page.evaluate(() => sessionStorage.getItem('token')), null);
});

test('normal session storage survives redirect when Chromium loses the response body', async t => {
  const f = await fixture(t, { body: { code: 0, data: { token: 'test-session-token' } }, behavior: 'redirect', unreadable: true });
  assert.equal(await f.run(), 'test-session-token');
});

test('visible official rejection is used if the response body cannot be read', async t => {
  const f = await fixture(t, { body: { code: 9 }, behavior: 'error', unreadable: true });
  await assert.rejects(f.run(), error => error.status === 401 && error.message.includes('账户已停用'));
});

for (const status of [401, 403, 429, 503]) {
  test(`HTTP ${status} is classified without waiting for a token`, async t => {
    const f = await fixture(t, { status, body: { message: 'upstream error' } });
    await assert.rejects(f.run(), error => error instanceof WebsiteLoginError && error.status === (status === 503 ? 502 : status));
  });
}

test('missing or literal undefined tokens never establish a connection', async t => {
  const f = await fixture(t, { body: { code: 0, data: { token: 'undefined' } }, behavior: 'redirect' });
  await assert.rejects(f.run(), error => error.name === 'TimeoutError');
});

test('only a short sanitized rejection message escapes the browser helper', async t => {
  const f = await fixture(t, { body: { code: 1, msg: '<b>private-test-password</b> ' + 'x'.repeat(100), data: { token: 'must-not-be-returned' } } });
  await assert.rejects(f.run(), error => error.status === 401 && !error.message.includes('private-test-password') && !error.message.includes('must-not-be-returned') && !error.message.includes('x'.repeat(48)) && !error.message.includes('<b>'));
});
