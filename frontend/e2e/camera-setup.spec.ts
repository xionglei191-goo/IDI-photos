import { test, expect, type Page, type APIRequestContext } from '@playwright/test';
import { mkdir } from 'node:fs/promises';
import path from 'node:path';

async function prepare(page: Page, request: APIRequestContext) {
  const auth = await (await request.get('/api/auth/status')).json();
  const login = await request.post(auth.setup_required ? '/api/auth/setup' : '/api/auth/login', {
    headers: { 'X-IDI-Request': '1' },
    data: { username: 'camera-ui-admin', password: 'isolated-camera-ui-password', display_name: '相机测试管理员' },
  });
  expect(login.ok()).toBeTruthy();
  await page.context().addCookies((await request.storageState()).cookies);
}

test('new video time updates after extraction without clearing settings or touching historical rows', async ({ page, request }) => {
  await prepare(page, request);
  let extracted = false;
  let reads = 0;
  await page.route('**/api/camera/admin/official', route => {
    reads++;
    return route.fulfill({ json: {
      config: { enabled: false, auto_verify: false, ready: false },
      captures: [
        { capture_id: 'new-video', time_state: extracted ? 'recognized' : 'queued',
          photo_time: extracted ? '2026-10-28 10:52:14' : null,
          time_basis: extracted ? 'video_watermark_ocr' : 'unavailable' },
        { capture_id: 'old-video', photo_time: null, time_state: null },
        { capture_id: 'unreadable-video', photo_time: null, time_state: 'needs_review' },
        { capture_id: 'app-video', photo_time: '2026-10-28 11:02:03', time_state: 'recorded',
          time_basis: 'app_sdk_watermark_render' },
      ].map(item => ({ ...item, device_id: 'phone', media_type: 'video',
        project_name: '都会A区', location: '9栋 · 4802室', case_number: 'CASE-VIDEO',
        stage_label: '施工过程', device_name: '现场手机', verification_state: 'not_requested', report: {} })),
    } });
  });
  await page.goto('/camera-setup');
  const rows = page.locator('#official-captures .device');
  await expect(rows.first().locator('.capture-time')).toHaveText('拍摄时间：水印时间识别中…');
  await page.locator('#group_watermark_id').fill('unsaved-template');
  extracted = true;
  await expect(rows.first().locator('.capture-time')).toHaveText('拍摄时间：2026-10-28 10:52:14（视频水印识别）');
  await expect(page.locator('#group_watermark_id')).toHaveValue('unsaved-template');
  await expect(rows.nth(1).locator('.capture-time')).toHaveText('拍摄时间：未记录');
  await expect(rows.nth(2).locator('.capture-time')).toHaveText('拍摄时间：水印时间未识别，请查看原视频');
  await expect(rows.nth(3).locator('.capture-time')).toHaveText('拍摄时间：2026-10-28 11:02:03（APP 记录的 SDK 水印时间）');
  const finishedReads = reads;
  await page.waitForTimeout(2300);
  expect(reads).toBe(finishedReads);
  const screenshotDir = path.resolve(import.meta.dirname, '../../artifacts/video-time-forward-20261028/screenshots');
  await mkdir(screenshotDir, { recursive: true });
  await page.locator('#official-captures').screenshot({ path: path.join(screenshotDir, 'video-time-desktop.png') });
  await page.setViewportSize({ width: 375, height: 812 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
  await page.locator('#official-captures').screenshot({ path: path.join(screenshotDir, 'video-time-mobile.png') });
});

test('capture history shows location and photo time with ten-row, empty, and partial pages', async ({ page, request }) => {
  await prepare(page, request);
  let count = 100;
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/api/camera/admin/official', route => route.fulfill({ json: {
    config: { enabled: false, auto_verify: false, ready: false },
    captures: Array.from({ length: count }, (_, i) => ({
      case_number: `CASE-${String(i + 1).padStart(3, '0')}`, stage_label: '施工过程', device_name: '现场手机',
      project_name: i === 1 ? '' : '都会A区', location: i === 1 ? '' : `1栋 · ${201 + i}室`,
      photo_time: i === 1 ? null : i === 2 ? '2026年10月28日 08:00' : '2026-10-28 09:12:34',
      time_basis: i === 1 ? 'unavailable' : i === 2 ? 'xhey_verification' : 'sdk_returned_unverified',
      captured_at: '2030-01-01T00:00:00+08:00', received_at: '2035-01-01T00:00:00+00:00',
      device_id: 'test-device', capture_id: `capture-${i}`, media_type: 'image', verification_state: 'not_requested', report: {},
    })),
  } }));
  await page.goto('/camera-setup');
  const rows = page.locator('#official-captures .device');
  await expect(rows).toHaveCount(10);
  await expect(rows.first().locator('.capture-location')).toHaveText('都会A区 · 1栋 · 201室');
  await expect(rows.first().locator('.capture-time')).toHaveText('拍摄时间：2026-10-28 09:12:34');
  await expect(rows.first().locator('.capture-title')).toHaveText('CASE-001 · 施工过程 · 现场手机');
  await expect(rows.nth(1).locator('.capture-location')).toHaveText('小区未记录 · 户号未记录');
  await expect(rows.nth(1).locator('.capture-time')).toHaveText('拍摄时间：未记录');
  await expect(rows.nth(2).locator('.capture-time')).toHaveText('拍摄时间：2026年10月28日 08:00');
  await expect(page.getByRole('button', { name: '上一页' })).toBeDisabled();
  for (let n = 1; n < 10; n++) await page.getByRole('button', { name: '下一页' }).click();
  await expect(rows.first()).toContainText('CASE-091');
  await expect(rows.first().locator('.capture-location')).toHaveText('都会A区 · 1栋 · 291室');
  await expect(rows.last()).toContainText('CASE-100');
  await expect(page.locator('#capture-page-info')).toHaveText('第 10 / 10 页 · 共 100 条');
  await expect(page.getByRole('button', { name: '下一页' })).toBeDisabled();
  await page.getByRole('button', { name: '上一页' }).click();
  await expect(rows.first()).toContainText('CASE-081');
  count = 11;
  await page.getByRole('button', { name: '刷新拍摄记录' }).click();
  await expect(rows.first()).toContainText('CASE-001');
  await page.getByRole('button', { name: '下一页' }).click();
  await expect(rows).toHaveCount(1);
  await expect(rows.first()).toContainText('CASE-011');
  await expect(rows.first().locator('.capture-location')).toHaveText('都会A区 · 1栋 · 211室');
  await expect(rows.first().locator('.capture-time')).toHaveText('拍摄时间：2026-10-28 09:12:34');
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
  count = 0;
  await page.getByRole('button', { name: '刷新拍摄记录' }).click();
  await expect(rows).toHaveCount(0);
  await expect(page.locator('#capture-pagination')).toBeHidden();
  await expect(page.locator('#official-captures')).toContainText('还没有官方 SDK 拍摄记录');
  expect(errors).toEqual([]);
});

for (const timezoneId of ['Asia/Shanghai', 'America/Los_Angeles']) {
  test.describe(`capture details in ${timezoneId}`, () => {
    test.use({ timezoneId });

    test('long location wraps and record text stays literal without changing photo time', async ({ page, request }) => {
      await prepare(page, request);
      const project = '都会A区 · 很长的小区名称用于核对手机拍摄记录的自动换行';
      const room = '二十六号楼 · 三单元 · 1802室';
      const literalProject = '<img src=x onerror="window.captureHistoryInjected=true">';
      const photoTime = '2026-10-28 00:12:34';
      const errors: string[] = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.route('**/api/camera/admin/official', route => route.fulfill({ json: {
        config: { enabled: false, auto_verify: false, ready: false },
        captures: [
          { project_name: project, location: room, photo_time: photoTime, time_basis: 'sdk_returned_unverified' },
          { project_name: literalProject, location: '1栋 · 201室', photo_time: '2026年10月28日 08:00', time_basis: 'xhey_verification' },
          { project_name: null, location: null, photo_time: null, time_basis: 'unavailable' },
        ].map((details, i) => ({
          ...details, case_number: `CASE-LONG-${i}`, stage_label: '完工照片', device_name: '姜柏安的手机',
          device_id: 'test-device', capture_id: `literal-${i}`, media_type: 'image',
          verification_state: i === 1 ? 'verified' : 'not_requested', report: {},
          captured_at: '2030-01-01T00:00:00+08:00', received_at: '2035-01-01T00:00:00+00:00',
        })),
      } }));
      await page.goto('/camera-setup');
      const rows = page.locator('#official-captures .device');
      await expect(rows).toHaveCount(3);
      await expect(rows.first().locator('.capture-location')).toHaveText(`${project} · ${room}`);
      await expect(rows.nth(1).locator('.capture-location')).toHaveText(`${literalProject} · 1栋 · 201室`);
      await expect(page.locator('#official-captures img')).toHaveCount(0);
      expect(await page.evaluate(() => 'captureHistoryInjected' in window)).toBeFalsy();
      await expect(rows.nth(2).locator('.capture-time')).toHaveText('拍摄时间：未记录');

      const screenshotDir = path.resolve(import.meta.dirname, '../../artifacts/video-time-forward-20261028/screenshots');
      if (timezoneId === 'Asia/Shanghai') await mkdir(screenshotDir, { recursive: true });
      for (const viewport of [
        { width: 1440, height: 1000, name: 'desktop' },
        { width: 375, height: 812, name: 'mobile-portrait' },
        { width: 812, height: 375, name: 'mobile-landscape' },
      ]) {
        await page.setViewportSize({ width: viewport.width, height: viewport.height });
        await expect(rows.first().locator('.capture-time')).toHaveText(`拍摄时间：${photoTime}`);
        await expect(rows.nth(1).locator('.capture-time')).toHaveText('拍摄时间：2026年10月28日 08:00');
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
        expect(await page.locator('#official-captures .device > div').evaluateAll(details =>
          details.every(detail => detail.scrollWidth <= detail.clientWidth + 1))).toBeTruthy();
        if (timezoneId === 'Asia/Shanghai') {
          await page.locator('#official-captures').screenshot({ path: path.join(screenshotDir, `capture-history-${viewport.name}.png`) });
        }
      }
      expect(errors).toEqual([]);
    });
  });
}

test('revoked phone can be deleted after confirmation and stays removed on reload', async ({ page, request }) => {
  await prepare(page, request);
  const headers = { 'X-IDI-Request': '1' };
  const projectResponse = await request.post('/api/projects', { headers, data: { name: '手机删除测试项目' } });
  expect(projectResponse.ok()).toBeTruthy();
  const project = await projectResponse.json();
  const admin = await (await request.get('/api/camera/admin')).json();
  const user = admin.users.find((u: { username: string }) => u.username === 'camera-ui-admin');
  const pairResponse = await request.post('/api/camera/admin/pairings', {
    headers, data: { name: '删除测试手机', user_id: user.id, project_ids: [project.id] },
  });
  expect(pairResponse.ok()).toBeTruthy();
  const paired = await request.post('/api/camera/pair', { headers, data: { code: (await pairResponse.json()).code } });
  expect(paired.ok()).toBeTruthy();
  const device = await paired.json();
  await page.goto('/camera-setup');
  const row = page.locator('#devices .device').filter({ hasText: '删除测试手机' });
  await expect(row.getByRole('button', { name: '删除', exact: true })).toHaveCount(0);
  await row.getByRole('button', { name: '停用连接' }).click();
  await expect(row).toContainText('已停用');
  page.once('dialog', dialog => dialog.dismiss());
  await row.getByRole('button', { name: '删除', exact: true }).click();
  await expect(row).toHaveCount(1);
  page.once('dialog', dialog => dialog.accept());
  await row.getByRole('button', { name: '删除', exact: true }).click();
  await expect(row).toHaveCount(0);
  await page.reload();
  await expect(page.locator('#devices')).toContainText('还没有已连接的手机');
  const access = await request.get('/api/camera/cases', { headers: { Authorization: 'Bearer ' + device.token } });
  expect(access.status()).toBe(401);
});
