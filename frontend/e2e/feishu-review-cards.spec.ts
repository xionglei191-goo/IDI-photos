import { test, expect } from '@playwright/test';

test('Feishu identity binding handles members, save, cancellation and removal', async ({ page, request }, testInfo) => {
  const users = await (await request.get('/api/access/users')).json();
  const user = users.find((value: { username: string }) => value.username === 'upload-ui-admin');
  expect(user).toBeTruthy();
  let bindings: unknown[] = [];
  const saves: unknown[] = [], deletions: string[] = [];
  await page.route('**/api/access/feishu-bindings', route => {
    if (route.request().method() === 'PUT') {
      saves.push(route.request().postDataJSON());
      bindings = [{ id: 'qa-binding', app_id: 'cli_qa', open_id: 'ou_qa', user_id: user.id,
        name: '模拟群成员', display_name: user.display_name, username: user.username, enabled: true }];
      return route.fulfill({ json: bindings[0] });
    }
    return route.fulfill({ json: { bindings, groups: [{ id: 'qa-group', name: '隔离内测群', app_id: 'cli_qa' }] } });
  });
  await page.route('**/api/access/feishu-bindings/groups/qa-group/members', route => route.fulfill({
    json: [{ open_id: 'ou_qa', name: '模拟群成员' }],
  }));
  await page.route('**/api/access/feishu-bindings/qa-binding', route => {
    deletions.push(route.request().method()); bindings = [];
    return route.fulfill({ json: { ok: true } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: '系统设置', exact: true }).click();
  await page.getByRole('navigation', { name: '设置分类' }).getByRole('button', { name: '人员与权限', exact: true }).click();
  const section = page.getByRole('region', { name: '飞书人员绑定', exact: true });
  await expect(section.getByRole('button', { name: '保存人员绑定' })).toBeDisabled();
  await section.getByRole('combobox', { name: /^飞书群/ }).selectOption('qa-group');
  await section.getByRole('combobox', { name: /^飞书人员/ }).selectOption('ou_qa');
  await section.getByRole('combobox', { name: /^对应 IDI 人员/ }).selectOption(user.id);
  await section.getByRole('button', { name: '保存人员绑定' }).click();
  await expect(section.getByText('按当前项目权限执行', { exact: true })).toBeVisible();
  expect(saves).toEqual([{ connector_id: 'qa-group', open_id: 'ou_qa', user_id: user.id }]);
  await section.getByRole('button', { name: '解除绑定', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '解除飞书人员绑定', exact: true });
  await dialog.getByRole('button', { name: '取消', exact: true }).click();
  expect(deletions).toEqual([]);
  for (const width of [1440, 375]) {
    await page.setViewportSize({ width, height: 950 });
    await section.scrollIntoViewIfNeeded();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`binding-${width}.png`), fullPage: true });
  }
  await section.getByRole('button', { name: '解除绑定', exact: true }).click();
  await dialog.getByRole('button', { name: '确认解除绑定' }).click();
  await expect(section.getByText('尚未绑定飞书人员。', { exact: false })).toBeVisible();
  expect(deletions).toEqual(['DELETE']);
});

test('full review link rejects malformed IDs and displays permission failures without writes', async ({ page }) => {
  const writes: string[] = [];
  page.on('request', request => { if (request.method() !== 'GET') writes.push(request.url()); });
  await page.goto('/?review=invalid');
  await expect(page.getByText('核对链接无效，请从最新飞书卡片重新打开。')).toBeVisible();
  const review = 'a'.repeat(32);
  await page.route('**/api/workflow/reviews/' + review, route => route.fulfill({
    status: 403, json: { detail: '没有该项目的资料查看权限' },
  }));
  await page.goto('/?review=' + review);
  await expect(page.getByRole('heading', { name: '拟提交资料核对' })).toBeVisible();
  await expect(page.getByText('没有该项目的资料查看权限', { exact: true })).toBeVisible();
  await expect(page.getByRole('link', { name: '返回 IDI 工作台' })).toBeVisible();
  expect(writes).toEqual([]);
});
