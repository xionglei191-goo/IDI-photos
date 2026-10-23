import { test, expect } from '@playwright/test';

test('proposal shows Feishu delivery and supports failure recovery without submitting', async ({ page, request }, testInfo) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  const auth = await (await request.get('/api/auth/status')).json();
  expect((await request.post(auth.setup_required ? '/api/auth/setup' : '/api/auth/login', {
    headers: { 'X-IDI-Request': '1' }, data: { username: 'upload-ui-admin', password: 'isolated-upload-ui-password', display_name: '隔离测试管理员' },
  })).ok()).toBeTruthy();
  await page.context().addCookies((await request.storageState()).cookies);
  const notification = { id: 'notification-1', state: 'failed', name: '项目负责人', chat_name: '项目照片收集群', error: '负责人已不在群中，请重新绑定。' };
  const status = {
    policy: { enabled: true, notify_review_ready: true }, connected: true, write_enabled: true,
    counts: { awaiting_confirmation: 1 }, items: [{
      case_id: 'notification-case', enabled: true, state: 'awaiting_confirmation', stage: 'survey',
      short_code: 'JMDHSJC-1-1101-01', platform_report_code: 'ZLBH-TEST', review_id: 'review-1', save_review_id: 'save-1',
      notification, detail: { saved_count: 5, files: [{ label: '楼栋打卡图片', count: 1 }, { label: '入户打卡图片', count: 1 }, { label: '缺陷照片', count: 3 }] },
    }],
  };
  const deliveries: unknown[] = [], submissions: string[] = [], errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/api/workflow/uploads', route => route.fulfill({ json: status }));
  await page.route('**/api/workflow/uploads/notifications/notification-1/retry', route => {
    deliveries.push('retry');
    notification.state = 'sent'; notification.error = '';
    return route.fulfill({ json: { state: 'queued' } });
  });
  await page.route('**/api/workflow/uploads/notifications/notification-1/confirm', route => {
    deliveries.push(route.request().postDataJSON());
    notification.state = 'sent'; notification.error = '';
    return route.fulfill({ json: { state: 'sent' } });
  });
  page.on('request', request => { if (/\/reviews\/[^/]+\/confirm$/.test(request.url())) submissions.push(request.url()); });
  await page.goto('/');
  const proposals = page.getByRole('region', { name: '拟提交事项', exact: true });
  await expect(proposals.getByText('飞书提醒发送失败：负责人已不在群中，请重新绑定。')).toBeVisible();
  await proposals.getByRole('button', { name: '重试飞书提醒', exact: true }).click();
  await expect(proposals.getByText('已在“项目照片收集群” @ 项目负责人 提醒核对')).toBeVisible();
  expect(deliveries).toEqual(['retry']);
  for (const width of [1440, 390, 320]) {
    await page.setViewportSize({ width, height: 1000 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`review-notification-${width}.png`), fullPage: true });
  }
  notification.state = 'unknown';
  await proposals.getByRole('button', { name: '核对提醒送达情况', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '核对飞书提醒送达情况', exact: true });
  await expect(dialog).toBeVisible();
  await dialog.getByRole('button', { name: '群内已收到', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  expect(deliveries).toEqual(['retry', { received: true }]);
  expect(submissions).toEqual([]);
  expect(errors).toEqual([]);
});
