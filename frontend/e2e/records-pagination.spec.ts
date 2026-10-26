import { test, expect, type Page, type APIRequestContext } from '@playwright/test';

async function setup(page: Page, request: APIRequestContext) {
  const auth = await (await request.get('/api/auth/status')).json();
  const login = await request.post(auth.setup_required ? '/api/auth/setup' : '/api/auth/login', {
    headers: { 'X-IDI-Request': '1' },
    data: { username: 'readback-admin', password: 'isolated-readback-password', display_name: '分页测试' },
  });
  expect(login.ok()).toBeTruthy();
  await page.context().addCookies((await request.storageState()).cookies);
  const projects = [];
  for (const name of ['分页甲', '分页乙']) {
    const response = await request.post('/api/projects', { headers: { 'X-IDI-Request': '1' }, data: { name: name + '-' + Date.now() } });
    expect(response.ok()).toBeTruthy(); projects.push(await response.json());
  }
  const errors: string[] = [], calls: { type: string; page: number; size: number; project: string }[] = [];
  page.on('pageerror', error => errors.push(error.message));
  const jobs = Array.from({ length: 135 }, (_, index) => ({
    id: `job-${String(index + 1).padStart(4, '0')}`, kind: `隔离任务 ${String(index + 1).padStart(3, '0')}`,
    state: index === 100 ? 'failed' : 'completed', progress: 10, total: 10, attempts: 1,
    payload: { project_id: projects[0].id }, result: { record: index + 1 },
    error: index === 100 ? '隔离测试：等待重试' : '', created_at: '2026-10-26T08:00:00+00:00',
  }));
  const state = { failOperations: false, failJobs: false, jobsTotal: 135, operationsTotal: 235,
    holdProject: '', hold: null as Promise<void> | null, retries: [] as string[] };
  function slice<T>(items: T[], requested: number, size: number) {
    const pages = Math.max(1, Math.ceil(items.length / size)), current = Math.min(requested, pages);
    return { items: items.slice((current - 1) * size, current * size), total: items.length, page: current, page_size: size, pages };
  }
  await page.route('**/api/access/website-operations*', async route => {
    const url = new URL(route.request().url()), project = url.searchParams.get('project_id') || '';
    const number = Number(url.searchParams.get('page') || 1), size = Number(url.searchParams.get('page_size') || 20);
    calls.push({ type: 'operations', page: number, size, project });
    if (project === state.holdProject && state.hold) await state.hold;
    if (state.failOperations) { state.failOperations = false; return route.fulfill({ status: 503, json: { detail: '隔离测试：官网操作记录读取失败' } }); }
    const items = Array.from({ length: project === projects[1].id ? 3 : state.operationsTotal }, (_, index) => ({
      id: `operation-${index}`, project_name: project === projects[1].id ? '分页乙' : '分页甲',
      case_title: `${project === projects[1].id ? '乙' : '甲'}案件记录 ${String(index + 1).padStart(3, '0')}`,
      actor_name: '隔离确认人员', website_account: 'isolated-account', source_users: [{ id: 'u', display_name: '隔离上传人员' }],
      operation: 'readback', state: 'verified', error: '', created_at: '2026-10-26T08:00:00+00:00',
    }));
    return route.fulfill({ json: slice(items, number, size) });
  });
  await page.route(/\/api\/jobs(?:\?|$)/, route => {
    const url = new URL(route.request().url());
    if (!url.searchParams.has('page')) return route.fulfill({ json: [] });
    const number = Number(url.searchParams.get('page')), size = Number(url.searchParams.get('page_size'));
    calls.push({ type: 'jobs', page: number, size, project: '' });
    if (state.failJobs) { state.failJobs = false; return route.fulfill({ status: 503, json: { detail: '隔离测试：任务记录读取失败' } }); }
    return route.fulfill({ json: slice(jobs.slice(0, state.jobsTotal), number, size) });
  });
  await page.route('**/api/jobs/*/retry', route => {
    const id = new URL(route.request().url()).pathname.split('/')[3];
    state.retries.push(id);
    const job = jobs.find(value => value.id === id)!;
    job.state = 'queued'; job.progress = 0; job.error = '';
    return route.fulfill({ json: job });
  });
  await page.goto('/');
  await page.getByRole('button', { name: '系统设置', exact: true }).click();
  return { projects, errors, calls, jobs, state };
}

async function openOperations(page: Page, projectId: string) {
  await page.getByRole('navigation', { name: '设置分类', exact: true }).getByRole('button', { name: '人员与权限', exact: true }).click();
  await page.getByLabel('选择项目', { exact: true }).selectOption(projectId);
  await expect(page.getByLabel('选择项目', { exact: true })).toHaveValue(projectId);
  return page.getByRole('region', { name: '官网操作记录', exact: true });
}

async function openJobs(page: Page) {
  await page.getByRole('navigation', { name: '设置分类', exact: true }).getByRole('button', { name: '后台任务', exact: true }).click();
  await page.getByRole('group', { name: '后台任务分类' }).getByRole('button', { name: '任务记录', exact: true }).click();
  return page.getByRole('region', { name: '任务记录', exact: true });
}

test('website operations page through full history and remember page size', async ({ page, request }) => {
  const fixture = await setup(page, request);
  const panel = await openOperations(page, fixture.projects[0].id);
  const pager = panel.getByRole('navigation', { name: '官网操作记录分页' });
  await expect(panel.locator('tbody tr')).toHaveCount(20);
  await expect(pager).toContainText('1–20 / 共 235 条');
  await expect(pager.getByRole('button', { name: '上一页', exact: true })).toBeDisabled();
  await pager.getByRole('button', { name: '下一页', exact: true }).click();
  await expect(panel.locator('tbody tr').first()).toContainText('甲案件记录 021');
  await panel.getByLabel('官网操作记录每页条数').selectOption('50');
  await expect(panel.locator('tbody tr')).toHaveCount(50);
  await expect(pager).toContainText('第 1 / 5 页');
  await pager.getByRole('button', { name: '末页', exact: true }).click();
  await expect(panel.locator('tbody tr')).toHaveCount(35);
  await expect(panel.locator('tbody tr').first()).toContainText('甲案件记录 201');
  await expect(panel.locator('tbody tr').last()).toContainText('甲案件记录 235');
  await expect(pager.getByRole('button', { name: '下一页', exact: true })).toBeDisabled();
  await panel.getByRole('button', { name: '刷新记录', exact: true }).click();
  await expect(pager).toContainText('第 5 / 5 页');
  await openJobs(page);
  const reopened = await openOperations(page, fixture.projects[0].id);
  await expect(reopened.getByLabel('官网操作记录每页条数')).toHaveValue('50');
  await expect(reopened.locator('tbody tr')).toHaveCount(50);
  expect(fixture.calls.some(call => call.type === 'operations' && call.page === 5 && call.size === 50)).toBeTruthy();
  expect(fixture.errors).toEqual([]);
});

test('changing project resets page and hides previous project while loading', async ({ page, request }) => {
  const fixture = await setup(page, request);
  const panel = await openOperations(page, fixture.projects[0].id);
  const pager = panel.getByRole('navigation', { name: '官网操作记录分页' });
  await expect(panel.locator('tbody tr')).toHaveCount(20);
  await pager.getByRole('button', { name: '下一页', exact: true }).click();
  await expect(panel.locator('tbody tr').first()).toContainText('甲案件记录 021');
  let release!: () => void;
  fixture.state.hold = new Promise<void>(resolve => { release = resolve; });
  fixture.state.holdProject = fixture.projects[1].id;
  try {
    await page.getByLabel('选择项目', { exact: true }).selectOption(fixture.projects[1].id);
    await expect.poll(() => fixture.calls.filter(call => call.project === fixture.projects[1].id).at(-1)?.page).toBe(1);
    await expect(panel.getByText('甲案件记录 021', { exact: true })).toHaveCount(0);
    await expect(panel.getByText('正在读取资料…', { exact: true })).toBeVisible();
    await expect(pager.getByRole('button', { name: '下一页', exact: true })).toBeDisabled();
  } finally { release(); }
  await expect(panel.locator('tbody tr')).toHaveCount(3);
  await expect(panel.locator('tbody tr').first()).toContainText('乙案件记录 001');
  await expect(pager).toContainText('第 1 / 1 页');
  expect(fixture.errors).toEqual([]);
});

test('task history opens old results and refreshes the current page', async ({ page, request }) => {
  const fixture = await setup(page, request);
  const panel = await openJobs(page);
  const pager = panel.getByRole('navigation', { name: '任务记录分页' });
  await expect(panel.locator('tbody tr')).toHaveCount(20);
  await pager.getByRole('button', { name: '下一页', exact: true }).click();
  await expect(panel.locator('tbody tr').first()).toContainText('隔离任务 021');
  await panel.locator('tbody tr').first().getByRole('button', { name: '查看结果' }).click();
  const dialog = page.getByRole('dialog', { name: '任务结果' });
  await expect(dialog).toContainText('隔离任务 021');
  await dialog.getByRole('button', { name: '关闭', exact: true }).last().click();
  await expect(pager).toContainText('第 2 / 7 页');
  await panel.getByLabel('任务记录每页条数').selectOption('50');
  await expect(panel.locator('tbody tr')).toHaveCount(50);
  await pager.getByRole('button', { name: '末页', exact: true }).click();
  await expect(panel.locator('tbody tr').first()).toContainText('隔离任务 101');
  await expect(panel.locator('tbody tr').last()).toContainText('隔离任务 135');
  await panel.getByRole('button', { name: '刷新', exact: true }).click();
  await expect(pager).toContainText('第 3 / 3 页');
  fixture.state.jobsTotal = 55;
  await panel.getByRole('button', { name: '刷新', exact: true }).click();
  await expect(panel.locator('tbody tr')).toHaveCount(5);
  await expect(pager).toContainText('第 2 / 2 页');
  expect(fixture.errors).toEqual([]);
});

test('retrying an old task keeps page and polls progress outside the dashboard feed', async ({ page, request }) => {
  const fixture = await setup(page, request);
  const panel = await openJobs(page);
  const pager = panel.getByRole('navigation', { name: '任务记录分页' });
  await expect(panel.locator('tbody tr')).toHaveCount(20);
  await panel.getByLabel('任务记录每页条数').selectOption('50');
  await expect(panel.locator('tbody tr')).toHaveCount(50);
  await pager.getByRole('button', { name: '末页', exact: true }).click();
  const first = panel.locator('tbody tr').first();
  await expect(first).toContainText('隔离任务 101');
  await first.getByRole('button', { name: '重试', exact: true }).click();
  await expect(first).toContainText('排队中');
  await expect(pager).toContainText('第 3 / 3 页');
  expect(fixture.state.retries).toEqual(['job-0101']);
  fixture.jobs[100].state = 'completed'; fixture.jobs[100].progress = 10;
  await expect(first).toContainText('已完成', { timeout: 10000 });
  await expect(pager).toContainText('第 3 / 3 页');
  expect(fixture.calls.filter(call => call.type === 'jobs' && call.page === 3).length).toBeGreaterThanOrEqual(3);
  expect(fixture.errors).toEqual([]);
});

test('failed record reads can retry and empty history has valid page boundaries', async ({ page, request }) => {
  const fixture = await setup(page, request);
  const operations = await openOperations(page, fixture.projects[0].id);
  await expect(operations.locator('tbody tr')).toHaveCount(20);
  fixture.state.failOperations = true;
  await operations.getByRole('button', { name: '刷新记录', exact: true }).click();
  await expect(operations.getByRole('alert')).toContainText('官网操作记录读取失败');
  await expect(operations.getByText('暂无官网操作记录', { exact: true })).toHaveCount(0);
  await operations.getByRole('button', { name: '重试读取记录' }).click();
  await expect(operations.locator('tbody tr')).toHaveCount(20);
  const jobs = await openJobs(page);
  await expect(jobs.locator('tbody tr')).toHaveCount(20);
  fixture.state.failJobs = true;
  await jobs.getByRole('button', { name: '刷新', exact: true }).click();
  await expect(jobs.getByRole('alert')).toContainText('任务记录读取失败');
  await jobs.getByRole('button', { name: '重试读取记录' }).click();
  await expect(jobs.locator('tbody tr')).toHaveCount(20);
  fixture.state.jobsTotal = 0;
  await jobs.getByRole('button', { name: '刷新', exact: true }).click();
  await expect(jobs.getByText('尚无任务', { exact: true })).toBeVisible();
  const pager = jobs.getByRole('navigation', { name: '任务记录分页' });
  await expect(pager).toContainText('0–0 / 共 0 条');
  await expect(pager).toContainText('第 1 / 1 页');
  for (const name of ['首页', '上一页', '下一页', '末页']) await expect(pager.getByRole('button', { name, exact: true })).toBeDisabled();
  expect(fixture.errors).toEqual([]);
});

test('record pagination remains usable on narrow and landscape screens', async ({ page, request }, testInfo) => {
  const fixture = await setup(page, request);
  for (const kind of ['operations', 'jobs']) {
    const panel = kind === 'operations' ? await openOperations(page, fixture.projects[0].id) : await openJobs(page);
    await expect(panel.locator('tbody tr')).toHaveCount(20);
    const pager = panel.getByRole('navigation');
    for (const [width, height] of [[1440, 1000], [375, 900], [812, 375]]) {
      await page.setViewportSize({ width, height });
      await page.emulateMedia({ reducedMotion: 'reduce' });
      await pager.scrollIntoViewIfNeeded();
      await expect(pager.getByRole('button', { name: '下一页', exact: true })).toBeVisible();
      await expect(pager.getByRole('status')).toBeVisible();
      for (const control of await pager.locator('button,select').all()) expect((await control.boundingBox())!.height).toBeGreaterThanOrEqual(44);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
      await page.screenshot({ path: testInfo.outputPath(`${kind}-${width}.png`), fullPage: true });
      if (width === 375) {
        const summaryUncovered = await pager.getByRole('status').evaluate(element => {
          const range = document.createRange(); range.selectNodeContents(element);
          return [...range.getClientRects()].every(rect => {
            const top = document.elementFromPoint(rect.right - 1, rect.top + rect.height / 2);
            return top === element || !!top && element.contains(top);
          });
        });
        expect(summaryUncovered).toBe(true);
        await pager.getByRole('button', { name: '下一页', exact: true }).click();
        await expect(pager).toContainText('第 2 /');
        await pager.getByRole('button', { name: '上一页', exact: true }).click();
        await expect(pager).toContainText('第 1 /');
      }
    }
    await page.setViewportSize({ width: 1440, height: 1000 });
  }
  expect(fixture.errors).toEqual([]);
});
