import { test, expect } from '@playwright/test';
import path from 'node:path';
import fs from 'node:fs/promises';

const headers = { 'X-IDI-Request': '1' };
const password = 'isolated-project-workbench-password';

test('project managers share the full workbench and only see assigned project data', async ({ page, request, browser }) => {
  const auth = await (await request.get('/api/auth/status')).json();
  expect((await request.post(auth.setup_required ? '/api/auth/setup' : '/api/auth/login', {
    headers, data: { username: 'workbench-admin', display_name: '管理界面对照', password },
  })).ok()).toBeTruthy();
  const create = async (url: string, data: object) => {
    const r = await request.post(url, { headers, data });
    expect(r.ok(), await r.text()).toBeTruthy();
    return r.json();
  };
  const own = await create('/api/projects', { name: '甲项目工作台验收' });
  const secret = await create('/api/projects', { name: '乙项目不可见标记' });
  const mine = await create('/api/cases', { project_id: own.id, title: '本项目墙面维修', building: '1', room: '301' });
  await create('/api/cases', { project_id: secret.id, title: '另一项目保密案件', building: '9', room: '909' });
  const manager = await create('/api/access/users', { username: 'workbench-manager', display_name: '项目管理员验收', password, system_role: 'user' });
  expect((await request.put(`/api/access/projects/${own.id}/members`, { headers, data: { members: [{ user_id: manager.id, role: 'manager' }] } })).ok()).toBeTruthy();
  const errors: string[] = [], failed: string[] = [], leaked: string[] = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('response', async r => {
    if (!r.url().includes('/api/') || r.url().includes('/auth/me')) return;
    if (r.status() >= 400) failed.push(`${r.status()} ${r.url()}`);
    if ((r.headers()['content-type'] || '').includes('application/json')) {
      const text = await r.text().catch(() => '');
      if (text.includes('乙项目不可见标记') || text.includes('另一项目保密案件')) leaked.push(r.url());
    }
  });
  await page.goto('/');
  await page.getByLabel('人员账号', { exact: true }).fill('workbench-manager');
  await page.getByLabel('密码', { exact: true }).fill(password);
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect(page.locator('.app-shell.v2-shell')).toBeVisible();
  await expect(page.getByRole('navigation', { name: '工作看板' })).toContainText('理赔受理');
  await expect(page.getByRole('navigation', { name: '工作看板' })).toContainText('查勘维修');
  await expect(page.getByRole('navigation', { name: '工作看板' })).toContainText('项目跟进');
  await expect(page.locator('.project-tree')).toContainText(own.name);
  await expect(page.locator('body')).not.toContainText(secret.name);
  await expect(page.getByRole('button', { name: '新建项目', exact: true })).toBeDisabled();
  await expect(page.getByRole('button', { name: '刷新清单', exact: true })).toBeEnabled();
  await expect(page.getByRole('button', { name: '打开 AI 助手', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '打开 AI 助手', exact: true }).click();
  await expect(page.getByRole('heading', { name: '工作台 AI 助手' })).toBeVisible();
  await page.getByRole('button', { name: 'AI 记忆', exact: true }).click();
  await expect(page.locator('#workspace-chat')).not.toContainText(secret.name);
  await page.getByRole('button', { name: '收起 AI 助手', exact: true }).click();
  const artifacts = path.resolve('..', 'artifacts', 'shared-workbench-2026-10-23');
  await fs.mkdir(artifacts, { recursive: true });
  await page.screenshot({ path: path.join(artifacts, 'manager-workbench.png'), fullPage: true });
  await page.getByRole('navigation', { name: '工作看板' }).getByRole('button', { name: '项目跟进', exact: true }).click();
  await expect(page.getByRole('heading', { name: '项目跟进', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '群助手', exact: true }).click();
  await expect(page.getByRole('heading', { name: '飞书群助手' })).toBeVisible();
  await page.getByRole('button', { name: '系统设置', exact: true }).click();
  for (const name of ['人员与权限', '连接与收图', '自动处理', '识别与模型', '项目与资料', '后台任务', '备份恢复']) {
    await page.getByRole('navigation', { name: '设置分类' }).getByRole('button', { name, exact: true }).click();
    await expect(page.getByRole('heading', { name, exact: true }).first()).toBeVisible();
    if (name === '自动处理') await expect(page.getByRole('checkbox', { name: '启用自动上传', exact: true })).toBeDisabled();
    if (name === '后台任务') {
      await page.getByRole('button', { name: '运行详情', exact: true }).click();
      await expect(page.getByRole('button', { name: '允许重新尝试', exact: true })).toBeDisabled();
      await page.getByRole('button', { name: '任务记录', exact: true }).click();
    }
  }
  await page.locator('.utility-menu summary').click();
  await page.locator('.utility-menu').getByRole('button', { name: '报送记录', exact: true }).click();
  await expect(page.getByRole('heading', { name: '报送管理', exact: true })).toBeVisible();
  await page.locator('.utility-menu summary').click();
  await page.locator('.utility-menu').getByRole('button', { name: '全部照片', exact: true }).click();
  await expect(page.getByRole('heading', { name: '照片资料', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '导入资料', exact: true }).first().click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByRole('button', { name: '开始导入', exact: true })).toBeDisabled();
  await expect(dialog.getByRole('button', { name: '本机文件夹', exact: true })).toHaveCount(0);
  await dialog.getByLabel('归入案件').selectOption(mine.id);
  await dialog.locator('input[type=file]').first().setInputFiles({ name: '项目管理员上传验收.png', mimeType: 'image/png',
    buffer: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAABQAAAAUCAIAAAAC64paAAAALElEQVR4nGNkYPjPQC5gIlsnw6hmkgET6VoQYFQziYCJVA3IYFQziYCiAAMAIJ4BJwS5UI4AAAAASUVORK5CYII=', 'base64') });
  await dialog.getByRole('checkbox', { name: '导入后进行本地OCR识别', exact: true }).uncheck();
  const uploadResponse = page.waitForResponse(r => r.url().endsWith('/api/import/upload') && r.request().method() === 'POST');
  await dialog.getByRole('button', { name: '开始导入', exact: true }).click();
  const upload = await (await uploadResponse).json();
  await expect.poll(async () => (await (await page.request.get(`/api/jobs/${upload.job.id}`)).json()).state).toBe('completed');
  await page.getByRole('button', { name: '刷新', exact: true }).click();
  await expect(page.getByRole('button', { name: '查看项目管理员上传验收.png', exact: true })).toBeVisible();
  await page.getByRole('navigation', { name: '工作看板' }).getByRole('button', { name: '理赔受理', exact: true }).click();
  await page.getByRole('row').filter({ hasText: '本项目墙面维修' }).getByRole('button').last().click();
  await expect(page.getByRole('button', { name: '编辑案件', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: '保司资料', exact: false }).click();
  await expect(page.getByRole('heading', { name: '本案资料包', exact: true })).toBeVisible();
  const adminContext = await browser.newContext({ storageState: await request.storageState(), viewport: { width: 1440, height: 1000 } });
  const adminPage = await adminContext.newPage();
  await adminPage.goto('/');
  await expect(adminPage.locator('.app-shell.v2-shell')).toBeVisible();
  await expect(adminPage.locator('.project-tree')).toContainText(secret.name);
  expect(await page.getByRole('navigation', { name: '工作看板' }).innerText()).toEqual(await adminPage.getByRole('navigation', { name: '工作看板' }).innerText());
  await adminPage.screenshot({ path: path.join(artifacts, 'admin-workbench.png'), fullPage: true });
  await adminContext.close();
  expect(errors).toEqual([]);
  expect(failed).toEqual([]);
  expect(leaked).toEqual([]);
});

test('mixed project roles retain edit boundaries and switching accounts resets the workbench', async ({ page, request }) => {
  const auth = await (await request.get('/api/auth/status')).json();
  expect((await request.post(auth.setup_required ? '/api/auth/setup' : '/api/auth/login', {
    headers, data: { username: 'workbench-admin', display_name: '管理界面对照', password },
  })).ok()).toBeTruthy();
  const create = async (url: string, data: object) => {
    const r = await request.post(url, { headers, data });
    expect(r.ok(), await r.text()).toBeTruthy();
    return r.json();
  };
  const own = await create('/api/projects', { name: '混合权限管理项目' });
  const member = await create('/api/projects', { name: '混合权限只读项目' });
  const mine = await create('/api/cases', { project_id: own.id, title: '允许编辑的案件' });
  const view = await create('/api/cases', { project_id: member.id, title: '只允许查看的案件' });
  const manager = await create('/api/access/users', { username: 'mixed-manager', display_name: '混合角色人员', password, system_role: 'user' });
  const second = await create('/api/access/users', { username: 'next-manager', display_name: '下一位管理员', password, system_role: 'user' });
  expect((await request.put(`/api/access/projects/${own.id}/members`, { headers, data: { members: [{ user_id: manager.id, role: 'manager' }] } })).ok()).toBeTruthy();
  expect((await request.put(`/api/access/projects/${member.id}/members`, { headers, data: { members: [{ user_id: manager.id, role: 'member' }, { user_id: second.id, role: 'manager' }] } })).ok()).toBeTruthy();
  const errors: string[] = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.goto('/');
  const login = async (name: string) => {
    await expect(page.getByRole('heading', { name: '登录工作台', exact: true })).toBeVisible();
    await page.getByLabel('人员账号', { exact: true }).fill(name);
    await page.getByLabel('密码', { exact: true }).fill(password);
    await page.getByRole('button', { name: '登录', exact: true }).click();
    await expect(page.locator('.app-shell.v2-shell')).toBeVisible();
  };
  await page.evaluate(() => {
    localStorage.setItem('idi-workspace-chat-selection-v1', JSON.stringify({ threadId: 'a'.repeat(32), scope: 'all' }));
    sessionStorage.setItem('idi-workspace-chat-open', '1');
  });
  const chatReads: string[] = [];
  page.on('request', r => { if (r.url().includes('/api/workspace-chat/threads/')) chatReads.push(r.url()); });
  await login('mixed-manager');
  await expect(page.getByRole('dialog', { name: '工作台 AI 助手', exact: true })).not.toBeVisible();
  await page.getByRole('row').filter({ hasText: view.title }).getByRole('button').last().click();
  await expect(page.getByRole('button', { name: '编辑案件', exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: '保司资料', exact: false }).click();
  await expect(page.getByRole('button', { name: '生成保司资料包', exact: true })).toBeDisabled();
  await page.getByRole('navigation', { name: '工作看板' }).getByRole('button', { name: '理赔受理', exact: true }).click();
  await page.getByRole('row').filter({ hasText: mine.title }).getByRole('button').last().click();
  await expect(page.getByRole('button', { name: '编辑案件', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: '打开 AI 助手', exact: true }).click();
  await page.getByRole('button', { name: '收起 AI 助手', exact: true }).click();
  await page.locator('.person-menu summary').click();
  await page.getByRole('button', { name: '退出登录', exact: true }).click();
  await page.evaluate(id => {
    localStorage.setItem('idi-workspace-chat-selection-v1-' + id, JSON.stringify({ threadId: 'b'.repeat(32), scope: 'all' }));
    sessionStorage.setItem('idi-workspace-chat-open-' + id, '1');
  }, manager.id);
  await login('next-manager');
  await expect(page.locator('.project-tree')).not.toContainText(own.name);
  await expect(page.locator('.project-tree')).toContainText(member.name);
  await expect(page.getByRole('dialog', { name: '工作台 AI 助手', exact: true })).not.toBeVisible();
  await page.getByRole('button', { name: '打开 AI 助手', exact: true }).click();
  await expect(page.getByRole('dialog', { name: '工作台 AI 助手', exact: true })).not.toContainText(own.name);
  await page.getByRole('button', { name: '收起 AI 助手', exact: true }).click();
  expect(chatReads).toEqual([]);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(async () => page.locator('.sidebar').evaluate(el => el.getBoundingClientRect().right)).toBeLessThanOrEqual(1);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  await page.screenshot({ path: path.resolve('..', 'artifacts', 'shared-workbench-2026-10-23', 'manager-mobile.png'), fullPage: true, animations: 'disabled' });
  expect(errors).toEqual([]);
});
