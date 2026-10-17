import { test, expect, type Page } from '@playwright/test';

function turn(sequence: number, group = 'a') {
  return { id: `${group}-${sequence}`, sequence, kind: 'chat', state: 'completed', project_id: null,
    connector_id: `group-${group}`, chat_id: `oc_${group}`, binding_revision: 1, conversation_key: '',
    project_name: `项目${group}`, chat_name: group === 'a' ? '照片收集群' : '施工协作群', connector_name: group,
    created_at: new Date(Date.UTC(2026, 9, 17, 10, sequence)).toISOString(), finished_at: new Date(Date.UTC(2026, 9, 17, 10, sequence, 10)).toISOString(),
    source_excerpt: `${group}第${sequence}轮问题`, source_text: `${group}第${sequence}轮问题`, source_raw: `${group}第${sequence}轮问题`,
    reply_excerpt: `${group}第${sequence}轮答复`, reply_text: `${group}第${sequence}轮答复\n已读取本机台账，业务状态和照片用途待核对。`,
    sender_label: sequence % 2 ? '张工' : '李工', sender_name_known: true, conversation_mode: sequence === 20 ? 'topic' : 'member',
    conversation_notice: '', image_count: 0, photos: [] as { id: string }[], model: 'test-model', error: null,
    delivery_state: sequence === 38 ? 'unknown' : 'sent', delivery_error: sequence === 38 ? '需要核对是否收到' : null };
}
function session(id: string, rows: ReturnType<typeof turn>[], kind = 'group') {
  return { id, connector_id: rows[0].connector_id, chat_id: rows[0].chat_id, session_kind: kind,
    title: rows[0].chat_name, subtitle: kind === 'brief' ? '测试项目 · 每日简报' : '群内问答与图片处理',
    turn_count: rows.length, member_count: kind === 'brief' ? 0 : 2, attention_count: rows.filter(r => r.delivery_state === 'unknown').length,
    active_count: 0, image_turns: 0, first_at: rows[0].created_at, last_at: rows.at(-1)!.created_at,
    latest_sequence: rows.at(-1)!.sequence, latest: rows.at(-1)! };
}
async function setup(page: Page, slowFirst = false) {
  const rows = Array.from({ length: 38 }, (_, i) => turn(i + 1));
  rows[31].photos = [{ id: 'test-photo' }]; rows[31].image_count = 1; rows[31].kind = 'images';
  const other = [turn(1, 'b')], brief = [{ ...turn(1, 'a'), kind: 'brief' }];
  const writes: string[] = [], errors: string[] = [];
  let delayed = false, delayFinished = false;
  page.on('request', r => { if (!['GET', 'HEAD', 'OPTIONS'].includes(r.method())) writes.push(r.url()); });
  page.on('pageerror', e => errors.push(e.message));
  await page.route('**/api/photos/test-photo/thumbnail', route => route.fulfill({ contentType: 'image/png', body: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aOKUAAAAASUVORK5CYII=', 'base64') }));
  await page.route('**/api/feishu-agent', route => route.fulfill({ json: { bindings: [], projects: [], model_ready: true, platform: { connected: true, write_enabled: true }, worker_enabled: true, tasks: rows.slice(-12).reverse() } }));
  await page.route('**/api/feishu-agent/sessions**', async route => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith('/sessions')) {
      const q = url.searchParams.get('q') || '', kind = url.searchParams.get('kind') || 'group';
      let groups = [session('fs_a', rows), session('fs_b', other)];
      if (kind === 'brief') groups = [session('fs_brief', brief, 'brief')];
      if (url.searchParams.get('attention') === 'true') groups = groups.filter(s => s.attention_count);
      if (q) groups = groups.filter(s => s.title.includes(q) || s.latest.source_text.includes(q));
      return route.fulfill({ json: { sessions: groups, total: groups.length, counts: { group: 2, brief: 1 }, offset: 0, limit: 20, has_more: false } });
    }
    const id = url.pathname.split('/').at(-1)!;
    const source = id === 'fs_b' ? other : id === 'fs_brief' ? brief : rows;
    if (slowFirst && id === 'fs_a' && !delayed) { delayed = true; await new Promise(resolve => setTimeout(resolve, 900)); delayFinished = true; }
    const before = Number(url.searchParams.get('before') || 0), available = source.filter(row => !before || row.sequence < before), visible = available.slice(-30);
    return route.fulfill({ json: { session: session(id, source, id === 'fs_brief' ? 'brief' : 'group'), turns: visible,
      has_more: available.length > visible.length, before: visible[0]?.sequence || before, latest_sequence: source.at(-1)!.sequence } }).catch(() => {});
  });
  await page.route('**/api/feishu-agent/tasks/*', route => {
    const row = rows.find(r => route.request().url().endsWith('/' + r.id))!;
    return route.fulfill({ json: { ...row, input: { text: row.source_text, conversation: { mode: row.conversation_mode } }, result: { text: row.reply_text }, usage: {}, images: [], drafts: [], steps: [], delivery: { state: row.delivery_state, text: row.reply_text, error: row.delivery_error, remote_message_id: null } } });
  });
  await page.goto('/?view=followup');
  await page.getByRole('button', { name: '群助手', exact: true }).click();
  await expect(page.getByRole('navigation', { name: '群会话列表' })).toBeVisible();
  return { rows, writes, errors, delayFinished: () => delayFinished };
}

test('group sessions retain full conversations, earlier history, independent statuses and safe live updates', async ({ page }, testInfo) => {
  const fixture = await setup(page);
  const panel = page.getByRole('region', { name: '照片收集群会话内容' });
  const timeline = panel.getByRole('region', { name: '会话消息时间线' });
  await expect(panel).toContainText('38 轮记录');
  await expect(timeline.locator('.feishu-session-turn')).toHaveCount(30);
  await expect(timeline.getByText('a第38轮问题', { exact: true })).toBeVisible();
  await expect(timeline).toContainText('送达情况待核对');
  await expect(timeline.locator('img')).toHaveCount(1);
  await timeline.evaluate(el => { el.scrollTop = 0; });
  await panel.getByRole('button', { name: '加载更早记录', exact: true }).click();
  await expect(timeline.locator('.feishu-session-turn')).toHaveCount(38);
  await expect(timeline).toContainText('a第1轮问题');
  await expect(timeline).toContainText('回复话题');
  await timeline.evaluate(el => { el.scrollTop = 0; });
  fixture.rows.push(turn(39));
  await expect(panel.getByRole('button', { name: '有新消息，查看最新', exact: true })).toBeVisible();
  expect(await timeline.evaluate(el => el.scrollTop)).toBeLessThan(30);
  await panel.getByRole('button', { name: '有新消息，查看最新', exact: true }).click();
  await expect(timeline.getByText('a第39轮问题', { exact: true })).toBeVisible();
  await timeline.locator('[data-turn-id="a-38"]').getByRole('button', { name: '处理详情', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByRole('button', { name: '群内已收到', exact: true })).toBeVisible();
  await expect(dialog.getByRole('button', { name: '重试群内发送', exact: true })).toHaveCount(0);
  await dialog.getByRole('button', { name: '关闭', exact: true }).last().click();
  await page.getByRole('group', { name: '会话类型' }).getByRole('button', { name: /每日简报/ }).click();
  await expect(page.locator('.feishu-session-turn')).toHaveCount(1);
  await expect(page.locator('.from-member')).toHaveCount(0);
  await page.getByRole('group', { name: '会话类型' }).getByRole('button', { name: /群会话/ }).click();
  await page.getByLabel('搜索会话').fill('不存在的群');
  await expect(page.getByText('没有符合条件的会话。', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '清空会话筛选', exact: true }).click();
  await expect(panel).toContainText('39 轮记录');
  await page.locator('.feishu-sessions').screenshot({ path: testInfo.outputPath('sessions-desktop.png'), animations: 'disabled' });
  await timeline.evaluate(el => { el.scrollTop = 0; });
  fixture.rows.push(...Array.from({ length: 31 }, (_, i) => turn(i + 40)));
  await expect(panel.getByRole('button', { name: '有较多新记录，查看最新消息', exact: true })).toBeVisible();
  expect(await timeline.evaluate(el => el.scrollTop)).toBeLessThan(30);
  await expect(timeline).not.toContainText('a第70轮问题');
  await panel.getByRole('button', { name: '有较多新记录，查看最新消息', exact: true }).click();
  await expect(panel).toContainText('70 轮记录');
  await expect(timeline.locator('.feishu-session-turn')).toHaveCount(30);
  await expect(timeline.getByText('a第70轮问题', { exact: true })).toBeVisible();
  expect(fixture.writes).toEqual([]); expect(fixture.errors).toEqual([]);
});

test('switching sessions discards late responses and mobile keeps a clear return path', async ({ page }, testInfo) => {
  const fixture = await setup(page, true);
  await page.getByRole('button', { name: '打开会话：施工协作群', exact: true }).click();
  const panel = page.getByRole('region', { name: '施工协作群会话内容' });
  await expect(panel).toContainText('b第1轮答复');
  await expect.poll(fixture.delayFinished).toBe(true);
  await expect(panel).not.toContainText('a第');
  await page.emulateMedia({ reducedMotion: 'reduce' });
  for (const [width, height] of [[375, 812], [812, 375], [320, 900]]) {
    await page.setViewportSize({ width, height });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await expect(panel).toBeVisible();
    if (width < 700) {
      await panel.getByRole('button', { name: '会话列表', exact: true }).click();
      await expect(page.getByRole('navigation', { name: '群会话列表' })).toBeVisible();
      const open = page.getByRole('button', { name: '打开会话：施工协作群', exact: true });
      await open.focus(); await page.keyboard.press('Enter');
      await expect(panel).toBeVisible();
      await page.locator('.feishu-sessions').screenshot({ path: testInfo.outputPath(`sessions-${width}.png`), animations: 'disabled' });
    }
  }
  expect(fixture.writes).toEqual([]); expect(fixture.errors).toEqual([]);
});
