import { expect, test, type Page } from '@playwright/test';

const KEY = 'a'.repeat(48), LATEST = '1'.repeat(32), OLDER = '2'.repeat(32);
const IDENTITY = 'idi-workspace-chat-key', POINTER = 'idi-workspace-chat-thread', SELECTION = 'idi-workspace-chat-selection-v1';
const answer = '已保存的答复：本次核对记录仍可继续查看。';
const olderAnswer = '较早的答复：请按案件编号查询。';
test.beforeEach(async ({ request, context }) => {
  const status = await (await request.get('/api/auth/status')).json();
  const result = await request.post(status.setup_required ? '/api/auth/setup' : '/api/auth/login', {
    headers: { 'X-IDI-Request': '1' },
    data: { username: 'workbench-admin', display_name: '管理界面对照', password: 'isolated-project-workbench-password' },
  });
  expect(result.ok(), await result.text()).toBeTruthy();
  await context.addCookies((await request.storageState()).cookies);
});
function thread(id: string, question: string, text: string) {
  return { id, title: question, project_id: null, case_id: null, context_label: '全部项目', updated_at: '2026-10-17T03:00:00+00:00',
    turns: [{ id: id + 'turn', question, answer: text, state: 'completed', progress: '已完成', error: null, created_at: '2026-10-17T03:00:00+00:00', steps: [], sources: [] }] };
}
async function service(page: Page) {
  const state = { threads: [thread(LATEST, '查询维修进展', answer), thread(OLDER, '怎样查询案件编号', olderAnswer)], failIndex: false, failDetail: false, missingDetails: false, detailReads: 0, posts: 0, keys: [] as string[] };
  await page.route('**/api/workspace-chat**', async route => {
    const request = route.request(), key = request.headers()['x-idi-chat-key'];
    state.keys.push(key);
    const path = new URL(request.url()).pathname;
    if (path === '/api/workspace-chat') {
      if (state.failIndex) return route.fulfill({ status: 503, json: { detail: '历史列表暂时不可用' } });
      const threads = key === KEY ? state.threads : [];
      return route.fulfill({ json: { ready: true, model: 'history-test', threads: threads.map(({ turns, ...item }) => item), active: null } });
    }
    if (path === '/api/workspace-chat/turns' && request.method() === 'POST') {
      state.posts++;
      const data = request.postDataJSON(), id = '3'.repeat(32);
      state.threads.unshift(thread(id, data.question, '新问题的已保存答复'));
      return route.fulfill({ json: { thread_id: id, turn_id: id + 'turn' } });
    }
    state.detailReads++;
    if (state.failDetail) return route.fulfill({ status: 503, json: { detail: '对话读取暂时中断' } });
    if (state.missingDetails) return route.fulfill({ status: 404, json: { detail: '对话不存在或不属于当前浏览器' } });
    const value = key === KEY && state.threads.find(item => path === '/api/workspace-chat/threads/' + item.id);
    return value ? route.fulfill({ json: value }) : route.fulfill({ status: 404, json: { detail: '对话不存在或不属于当前浏览器' } });
  });
  return state;
}
async function start(page: Page, saved = LATEST) {
  await page.goto('/');
  await page.evaluate(({ key, saved }) => {
    localStorage.setItem('idi-workspace-chat-key', key);
    for (const name of ['idi-workspace-chat-selection-v1', 'idi-workspace-chat-thread']) {
      localStorage.removeItem(name); sessionStorage.removeItem(name);
      document.cookie = `${name}=; Path=/; Max-Age=0`;
    }
    if (saved) localStorage.setItem('idi-workspace-chat-thread', saved);
  }, { key: KEY, saved });
  await page.reload();
  await page.getByRole('button', { name: /打开 AI 助手/ }).click();
}
const dialog = (page: Page) => page.getByRole('dialog', { name: '工作台 AI 助手' });

test('legacy conversation and open dialog survive refresh without resending questions', async ({ page }) => {
  const state = await service(page);
  await start(page);
  await expect(dialog(page).getByText(answer, { exact: true })).toBeVisible();
  await page.reload();
  await expect(dialog(page)).toBeVisible();
  await expect(dialog(page).getByText(answer, { exact: true })).toBeVisible();
  expect(await page.evaluate(name => localStorage.getItem(name), POINTER)).toBe(LATEST);
  expect((await page.context().cookies()).find(item => item.name === IDENTITY)?.value).toBe(KEY);
  expect(state.posts).toBe(0);
  await dialog(page).getByRole('button', { name: '收起 AI 助手' }).click();
  await page.reload();
  await expect(dialog(page)).not.toBeVisible();
  await page.getByRole('button', { name: /打开 AI 助手/ }).click();
  await expect(dialog(page).getByText(answer, { exact: true })).toBeVisible();
});

test('missing pointer restores latest saved history and remembers a subsequently selected older thread', async ({ page }, testInfo) => {
  const state = await service(page);
  await start(page, '');
  await expect(dialog(page).getByText(answer, { exact: true })).toBeVisible();
  await dialog(page).getByRole('button', { name: '最近对话', exact: true }).click();
  await expect(dialog(page).locator('.workspace-chat-history>button')).toHaveCount(2);
  await dialog(page).getByRole('button', { name: /怎样查询案件编号/ }).click();
  await expect(dialog(page).getByText(olderAnswer, { exact: true })).toBeVisible();
  await page.reload();
  await expect(dialog(page).getByText(olderAnswer, { exact: true })).toBeVisible();
  for (const width of [1440, 390, 320]) {
    await page.setViewportSize({ width, height: 900 });
    const box = await dialog(page).boundingBox();
    expect(box!.x).toBeGreaterThanOrEqual(0);
    expect(box!.x + box!.width).toBeLessThanOrEqual(width + 1);
    expect(await dialog(page).evaluate(el => el.scrollWidth <= el.clientWidth + 1)).toBe(true);
    await expect(dialog(page).getByRole('button', { name: '最近对话' })).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath(`chat-restored-${width}.png`) });
  }
  expect(state.posts).toBe(0);
});

test('explicit new conversation stays new after reload and old history remains reachable', async ({ page }) => {
  const state = await service(page);
  await start(page);
  await expect(dialog(page).getByText(answer, { exact: true })).toBeVisible();
  await dialog(page).getByRole('button', { name: '新建对话' }).click();
  await page.reload();
  await expect(dialog(page).getByRole('heading', { name: '今天需要了解什么？' })).toBeVisible();
  await expect(dialog(page).locator('.workspace-chat-question')).toHaveCount(0);
  await dialog(page).getByRole('button', { name: '继续上次对话' }).click();
  await expect(dialog(page).getByText(answer, { exact: true })).toBeVisible();
  expect(state.posts).toBe(0);
});

test('newly sent question persists selection immediately and reload reads the saved answer', async ({ page }) => {
  const state = await service(page);
  await start(page);
  await expect(dialog(page).getByText(answer, { exact: true })).toBeVisible();
  await dialog(page).getByRole('button', { name: '新建对话' }).click();
  await dialog(page).getByLabel('发送给 AI', { exact: true }).fill('保存这次新的提问');
  await dialog(page).getByRole('button', { name: '发送问题' }).click();
  await expect(dialog(page).getByText('新问题的已保存答复', { exact: true })).toBeVisible();
  await page.reload();
  await expect(dialog(page).getByText('保存这次新的提问', { exact: false })).toBeVisible();
  await expect(dialog(page).getByText('新问题的已保存答复', { exact: true })).toBeVisible();
  expect(state.posts).toBe(1);
});

test('first-party backup restores the same identity after local storage is lost', async ({ page }) => {
  await service(page);
  await start(page);
  await expect(dialog(page).getByText(answer, { exact: true })).toBeVisible();
  await page.evaluate(names => { for (const name of names) { localStorage.removeItem(name); sessionStorage.removeItem(name); } }, [IDENTITY, POINTER, SELECTION]);
  await page.reload();
  await expect(dialog(page).getByText(answer, { exact: true })).toBeVisible();
  expect(await page.evaluate(name => localStorage.getItem(name), IDENTITY)).toBe(KEY);
});

test('blocked local storage uses the existing cookie and survives another refresh', async ({ page }) => {
  await service(page);
  await start(page);
  await expect(dialog(page).getByText(answer, { exact: true })).toBeVisible();
  await page.addInitScript(() => Object.defineProperty(window, 'localStorage', { get() { throw new DOMException('Blocked', 'SecurityError'); } }));
  for (let n = 0; n < 2; n++) {
    await page.reload();
    await expect(dialog(page).getByText(answer, { exact: true })).toBeVisible();
  }
});

test('session storage retains history when both persistent stores are unavailable and explains the limit', async ({ page }) => {
  await service(page);
  await start(page);
  await expect(dialog(page).getByText(answer, { exact: true })).toBeVisible();
  await page.addInitScript(() => {
    Object.defineProperty(window, 'localStorage', { get() { throw new DOMException('Blocked', 'SecurityError'); } });
    Object.defineProperty(document, 'cookie', { get() { return ''; }, set() { throw new DOMException('Blocked', 'SecurityError'); } });
  });
  await page.reload();
  await expect(dialog(page).getByText(answer, { exact: true })).toBeVisible();
  await expect(dialog(page).getByText(/浏览器仅允许当前标签页保存对话标识/)).toBeVisible();
});

test('a temporary detail failure keeps the selected conversation and offers retry instead of an empty chat', async ({ page }) => {
  const state = await service(page);
  state.failDetail = true;
  await start(page);
  await expect(dialog(page).getByRole('alert')).toContainText('对话读取暂时中断');
  await expect(dialog(page).getByRole('heading', { name: '今天需要了解什么？' })).toHaveCount(0);
  expect(await page.evaluate(name => localStorage.getItem(name), POINTER)).toBe(LATEST);
  state.failDetail = false;
  await dialog(page).getByRole('button', { name: '重新读取' }).click();
  await expect(dialog(page).getByText(answer, { exact: true })).toBeVisible();
  await expect(dialog(page).getByRole('alert')).toHaveCount(0);
  expect(state.posts).toBe(0);
});

test('a history-list outage is not presented as having no saved records', async ({ page }) => {
  const state = await service(page);
  state.failIndex = true;
  await start(page, '');
  await dialog(page).getByRole('button', { name: '最近对话' }).click();
  await expect(dialog(page).getByText('历史记录暂时无法读取，请重试。')).toBeVisible();
  await expect(dialog(page).getByText(/还没有对话记录/)).toHaveCount(0);
  state.failIndex = false;
  await dialog(page).getByRole('button', { name: '重新读取' }).click();
  await expect(dialog(page).locator('.workspace-chat-history>button')).toHaveCount(2);
});

test('404 selection recovers an accessible thread without mixing a different browser identity', async ({ page }) => {
  const state = await service(page);
  await start(page, '9'.repeat(32));
  await expect(dialog(page).getByText(answer, { exact: true })).toBeVisible();
  await page.evaluate(({ key, names }) => {
    for (const name of names) {
      localStorage.removeItem(name); sessionStorage.removeItem(name); document.cookie = `${name}=; Path=/; Max-Age=0`;
    }
    localStorage.setItem('idi-workspace-chat-key', key);
  }, { key: 'b'.repeat(48), names: [IDENTITY, POINTER, SELECTION] });
  await page.reload();
  await dialog(page).getByRole('button', { name: '最近对话' }).click();
  await expect(dialog(page).getByText(/当前浏览器在此访问地址还没有对话记录/)).toBeVisible();
  await expect(dialog(page).locator('.workspace-chat-history>button')).toHaveCount(0);
  expect(state.posts).toBe(0);
});

test('a stale index with missing threads stops retrying after checking each candidate', async ({ page }) => {
  const state = await service(page);
  state.missingDetails = true;
  await start(page);
  await expect(dialog(page).getByRole('alert')).toContainText('上次对话在当前浏览器中不可用');
  expect(state.detailReads).toBeLessThanOrEqual(3);
  expect(state.posts).toBe(0);
});

test('an explicitly selected project scope survives a closed-dialog refresh', async ({ page }) => {
  await service(page);
  const projectId = '4'.repeat(32);
  await page.route('**/api/projects', route => route.fulfill({ json: [{ id: projectId, name: '历史恢复验证项目' }] }));
  await start(page);
  await expect(dialog(page).getByText(answer, { exact: true })).toBeVisible();
  await dialog(page).getByLabel('查询范围').selectOption('project:' + projectId);
  await dialog(page).getByRole('button', { name: '收起 AI 助手' }).click();
  await page.reload();
  await page.getByRole('button', { name: /打开 AI 助手/ }).click();
  await expect(dialog(page).getByLabel('查询范围')).toHaveValue('project:' + projectId);
  await expect(dialog(page).locator('.workspace-chat-question')).toHaveCount(0);
});
