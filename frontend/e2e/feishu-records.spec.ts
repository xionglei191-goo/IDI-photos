import { test, expect } from '@playwright/test';
import type { FeishuTask } from '../src/FeishuTaskHistory';

const task = (id: string, extra: Partial<FeishuTask> = {}): FeishuTask => ({
  id, kind: 'chat', state: 'completed', project_id: null, project_name: '照片收集群',
  connector_id: 'group-a', connector_name: '照片收集群', chat_name: '照片收集群',
  error: null, created_at: '2026-10-17T13:30:00Z', finished_at: '2026-10-17T13:31:00Z', delivery_state: 'sent',
  source_excerpt: `案件 JMDHS-A3-${id} 现在是什么进度？`, reply_excerpt: '目前在查勘阶段，现场照片已归档，业务字段待核对。',
  sender_label: '成员91ab730d', sender_name_known: false, conversation_mode: 'member', image_count: 0,
  ...extra,
});
const records = [
  task('latest', { source_excerpt: '', mention_only: true }),
  task('partial', { kind: 'images', state: 'partial', source_excerpt: '', image_count: 2, sender_label: '张工', sender_name_known: true, reply_excerpt: '2 张图片已归档，需核对案件用途。' }),
  task('unknown', { delivery_state: 'unknown', delivery_error: '请求中断，请先核对群内是否已收到。', source_excerpt: 'JMDHS-A3-3001 是否完成查勘？', conversation_mode: 'topic' }),
  task('brief', { kind: 'brief', project_id: 'project-a', project_name: '测试项目', sender_label: '定时任务', brief_date: '2026-10-17', connector_id: 'group-b', chat_name: '施工群', reply_excerpt: '查勘维修跟进：7 个案件。' }),
  ...Array.from({ length: 12 }, (_, i) => task(String(i), { created_at: '2026-10-16T16:30:00Z' })),
  task('old', { created_at: '2026-10-16T15:30:00Z' }),
];
async function openHistory(page: import('@playwright/test').Page, tasks = records) {
  const writes: string[] = [];
  page.on('request', request => { if (!['GET', 'HEAD', 'OPTIONS'].includes(request.method())) writes.push(request.url()); });
  await page.route('**/api/feishu-agent', route => route.fulfill({ json: { bindings: [], projects: [], model_ready: true, platform: { connected: true, write_enabled: true }, worker_enabled: true, tasks } }));
  await page.route('**/api/feishu-agent/tasks/*', route => {
    const row = records.find(value => route.request().url().endsWith('/' + value.id))!;
    return route.fulfill({ json: { ...row, input: { text: row.source_excerpt, conversation: { mode: row.conversation_mode } }, result: { text: '完整答复：仍待补齐字段，未正式提交。\n以下内容仅用于核对。' }, usage: { requests: 2 }, model: 'test-model', images: [], drafts: [], steps: [{ step: 1, tool: 'read_case', outcome: '读取成功', created_at: row.created_at }], delivery: { state: row.delivery_state, text: '', error: row.delivery_error, remote_message_id: null } } });
  });
  await page.goto('/?view=followup');
  await page.getByRole('button', { name: '群助手', exact: true }).click();
  await expect(page.getByRole('heading', { name: '飞书群助手', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '逐条记录', exact: true }).click();
  return writes;
}

test('history exposes questions and replies with combined filters and separate delivery truth', async ({ page }, testInfo) => {
  const errors: string[] = []; page.on('pageerror', error => errors.push(error.message));
  const writes = await openHistory(page);
  const history = page.getByRole('region', { name: /处理记录/ });
  await expect(history.locator('.feishu-agent-task')).toHaveCount(12);
  await expect(history).toContainText('仅 @ 机器人，接续上下文');
  await expect(history).toContainText('回复：目前在查勘阶段');
  await expect(page.locator('.feishu-agent-configuration')).not.toHaveAttribute('open', '');
  await history.getByRole('button', { name: '待核对 2', exact: true }).click();
  await expect(history.locator('.feishu-agent-task')).toHaveCount(2);
  await expect(history).toContainText('处理完成');
  await expect(history).toContainText('送达情况待核对');
  await history.getByLabel('搜索记录').fill('3001');
  await expect(history.locator('.feishu-agent-task')).toHaveCount(1);
  await page.getByRole('button', { name: '刷新', exact: true }).click();
  await expect(history.getByLabel('搜索记录')).toHaveValue('3001');
  await expect(history.getByLabel('处理情况')).toHaveValue('attention');
  await history.getByRole('button', { name: /查看群对话记录/ }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByRole('heading', { name: '来源消息', exact: true })).toBeVisible();
  await expect(dialog.getByRole('heading', { name: '助手答复', exact: true })).toBeVisible();
  await expect(dialog).toContainText('完整答复：仍待补齐字段，未正式提交。');
  await expect(dialog.getByRole('button', { name: '群内已收到', exact: true })).toBeVisible();
  await expect(dialog.getByRole('button', { name: '重试群内发送', exact: true })).toHaveCount(0);
  await dialog.getByRole('button', { name: '关闭', exact: true }).last().click();
  await history.getByRole('button', { name: '清空筛选', exact: true }).click();
  await history.getByRole('group', { name: '记录类型' }).getByRole('button', { name: /每日简报/ }).click();
  await expect(history.locator('.feishu-agent-task')).toHaveCount(1);
  await expect(history).toContainText('测试项目 · 2026-10-17 简报');
  await history.getByLabel('来源群组').selectOption('group-a');
  await expect(history).toContainText('没有符合条件的记录');
  await history.getByRole('button', { name: '清空筛选', exact: true }).first().click();
  await history.getByRole('button', { name: '下一页', exact: true }).click();
  await expect(history.locator('.feishu-agent-task')).toHaveCount(5);
  await expect(history.getByRole('heading', { name: /2026\/10\/16/ })).toHaveCount(1);
  await expect(history.getByRole('heading', { name: /2026\/10\/17/ })).toHaveCount(1);
  await history.getByLabel('搜索记录').fill('不存在的编号');
  await expect(history).toContainText('没有符合条件的记录');
  await history.getByRole('button', { name: '清空筛选', exact: true }).first().click();
  await history.getByLabel('处理情况').selectOption('completed');
  await expect(history).not.toContainText('送达情况待核对');
  await history.getByRole('button', { name: '清空筛选', exact: true }).click();
  for (const [width, height] of [[1440, 1000], [375, 812], [812, 375], [320, 900]]) {
    await page.setViewportSize({ width, height });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    const smallButtons = await history.locator('button').evaluateAll(elements => elements.filter(e => e.getBoundingClientRect().height > 0 && e.getBoundingClientRect().height < 44).length);
    expect(smallButtons).toBe(0);
    if (height > 375) {
      await history.scrollIntoViewIfNeeded();
      await page.screenshot({ path: testInfo.outputPath(`feishu-history-${width}.png`), animations: 'disabled' });
    }
  }
  expect(errors).toEqual([]); expect(writes).toEqual([]);
});

test('empty history keeps setup accessible', async ({ page }) => {
  const writes = await openHistory(page, []);
  await expect(page.locator('.feishu-agent-configuration')).toHaveAttribute('open', '');
  await expect(page.getByText('开启群助手后，新的群内 @ 消息和图片将在这里显示。')).toBeVisible();
  expect(writes).toEqual([]);
});
