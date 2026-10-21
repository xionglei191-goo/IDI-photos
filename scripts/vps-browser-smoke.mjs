// Uses a synthetic login page. No request or credential reaches the real website.
import assert from 'node:assert/strict';
import { chromium } from '../frontend/node_modules/playwright/index.mjs';
import { browserLaunchOptions, loginThroughWebsite } from './platform-login.mjs';

assert.equal(browserLaunchOptions('win32').channel, 'chrome');
assert.equal(browserLaunchOptions('linux').channel, undefined);
const browser = await chromium.launch(browserLaunchOptions());
try {
  const page = await browser.newPage();
  let loginRequests = 0;
  await page.route('**/*', async route => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.origin === 'https://gzzb2021.com' && url.pathname === '/idi/' && request.method() === 'GET') {
      await route.fulfill({ contentType: 'text/html', body: `<!doctype html><meta charset="utf-8">
        <input placeholder="账号/手机号"><input placeholder="输入密码" type="password">
        <button onclick="fetch('/iidi/login',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({username:document.querySelectorAll('input')[0].value,
        password:document.querySelectorAll('input')[1].value})})">登录</button>` });
    } else if (url.origin === 'https://gzzb2021.com' && url.pathname === '/iidi/login' && request.method() === 'POST') {
      assert.deepEqual(request.postDataJSON(), { username: 'smoke-user', password: 'smoke-password' });
      loginRequests++;
      await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ code: 0, data: { token: 'smoke-token' } }) });
    } else {
      await route.abort();
      throw new Error('Unexpected request in isolated login test');
    }
  });
  const diagnostics = {};
  const token = await loginThroughWebsite(page, { username: 'smoke-user', password: 'smoke-password' }, diagnostics, () => {});
  assert.equal(token, 'smoke-token');
  assert.equal(loginRequests, 1);
  assert.equal(diagnostics.requestSent, true);
  const result = { browser_login: 'passed', platform: process.platform };
  if (process.env.IDI_SMOKE_WORKBENCH === '1') {
    const workbench = await browser.newPage();
    const errors = [];
    workbench.on('pageerror', error => errors.push(error.message));
    const response = await workbench.goto('http://127.0.0.1:8766/', { waitUntil: 'networkidle' });
    assert.equal(response.status(), 200);
    const auth = await (await workbench.request.get('http://127.0.0.1:8766/api/auth/status')).json();
    await workbench.getByRole('heading', { name: auth.setup_required ? '创建系统管理员' : '登录工作台', exact: true }).waitFor();
    assert.equal((await workbench.request.get('http://127.0.0.1:8766/api/projects')).status(), 401);
    assert.deepEqual(errors, []);
    result.workbench_login_render = 'passed';
  }
  console.log(JSON.stringify(result));
} finally {
  await browser.close();
}
