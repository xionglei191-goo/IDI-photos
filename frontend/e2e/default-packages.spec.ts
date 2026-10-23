import { test, expect } from '@playwright/test';
import fs from 'node:fs/promises';
import path from 'node:path';
import { execFileSync } from 'node:child_process';

const headers = { 'X-IDI-Request': '1' };
test('project requirement is saved and submitted repairs are prepared before downloading', async ({ page, request }, testInfo) => {
  const auth = await (await request.get('/api/auth/status')).json();
  expect((await request.post(auth.setup_required ? '/api/auth/setup' : '/api/auth/login', { headers,
    data: { username: 'upload-ui-admin', password: 'isolated-upload-ui-password', display_name: '隔离测试管理员' } })).ok()).toBeTruthy();
  await page.context().addCookies((await request.storageState()).cookies);
  await page.goto('/');
  await page.getByRole('button', { name: '系统设置', exact: true }).click();
  await page.getByRole('button', { name: /项目与资料/ }).click();
  await page.getByRole('region', { name: '系统设置', exact: true }).getByRole('button', { name: '新建项目', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByLabel('保司资料要求', { exact: true })).toHaveValue('insurer-v2');
  await dialog.getByLabel('项目 / 小区名称').fill('默认资料页面验收');
  await dialog.getByLabel('保司资料要求', { exact: true }).selectOption('picc-record-v1');
  await dialog.getByRole('button', { name: '保存项目', exact: true }).click();
  const row = page.locator('tbody tr').filter({ hasText: '默认资料页面验收' });
  await expect(row).toContainText('人保查勘及施工记录表');
  await expect(page.getByRole('columnheader', { name: '保司资料要求', exact: true })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('project-default-column.png'), fullPage: true, animations: 'disabled' });
  await row.getByRole('button', { name: '编辑', exact: true }).click();
  await expect(dialog.getByLabel('保司资料要求', { exact: true })).toHaveValue('picc-record-v1');
  await dialog.getByRole('button', { name: '取消', exact: true }).click();
  const project = (await (await request.get('/api/projects')).json()).find((p: { name: string }) => p.name === '默认资料页面验收');
  const value = await (await request.post('/api/cases', { headers, data: { project_id: project.id, title: '默认资料隔离验收', building: '1', room: '201', platform_report_code: 'UIAUTO001' } })).json();
  expect(value.insurer_template_id).toBe('picc-record-v1');
  const source = await fs.readFile(new URL('./workbench.spec.ts', import.meta.url), 'utf8');
  const png = Buffer.from(source.match(/Buffer.from\('([^']+)', 'base64'\)/)![1], 'base64');
  expect((await request.post('/api/import/upload', { headers, multipart: { case_id: value.id, auto_ocr: 'false', files: { name: '默认资料.png', mimeType: 'image/png', buffer: png } } })).ok()).toBeTruthy();
  await expect.poll(async () => (await (await request.get(`/api/photos?case_id=${value.id}`)).json()).total).toBe(1);
  const beforeSurvey = await (await request.get(`/api/workflow/cases/${value.id}`)).json();
  expect((await request.post(`/api/workflow/cases/${value.id}/result`, { headers,
    data: { action: 'accepted', event_at: '2026-10-23T08:00:00', note: '隔离测试受理', revision: beforeSurvey.case.revision } })).ok()).toBeTruthy();
  await page.goto('/');
  await page.getByRole('navigation', { name: '工作看板' }).getByRole('button', { name: '查勘维修', exact: true }).click();
  await page.locator(`[data-case-id="${value.id}"] .case-link`).click();
  await page.getByRole('button', { name: /^保司资料/ }).click();
  const card = page.getByRole('region', { name: '默认资料', exact: true });
  await expect(card).toContainText('修复成果提交并经官网确认后');
  // Seed only the isolated browser-test database with a simulated official readback.
  // The production scheduler and document worker perform the actual preparation.
  const system = await (await request.get('/api/system')).json();
  const root = path.resolve(import.meta.dirname, '../..');
  const data = path.resolve(system.data_dir);
  expect(data.startsWith(path.join(root, 'tmp', 'browser-test-'))).toBeTruthy();
  const python = path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
  execFileSync(python, ['-X', 'utf8', '-c', `import sqlite3,json,sys
from datetime import datetime,timezone
db=sqlite3.connect(sys.argv[1]); case_id=sys.argv[2]; stamp=datetime.now(timezone.utc).isoformat()
snapshot={'scope':'repair','row':{'repairAcceptanceFlowStatus':'1','repairAcceptanceState':'1'},'fetched_at':stamp}
db.execute("UPDATE cases SET workflow_source='platform',workflow_stage='acceptance',workflow_verified_at=? WHERE id=?",(stamp,case_id))
db.execute('INSERT INTO platform_links VALUES(?,?,?,?,?,?,?,?)',(case_id,'isolated-auto-readback','UIAUTO001','',json.dumps(snapshot),stamp,1,stamp))
db.commit(); db.close()`, path.join(data, 'archive.sqlite3'), value.id]);
  const link = card.getByRole('link', { name: '下载默认记录表 v1', exact: true });
  await expect(link).toBeVisible({ timeout: 20000 });
  const downloadEvent = page.waitForEvent('download');
  await link.click();
  const download = await downloadEvent;
  expect(download.suggestedFilename()).toContain('UIAUTO001-v1.docx');
  await download.saveAs(testInfo.outputPath('default-record.docx'));
  await expect(card).toContainText('已提前准备，可直接下载');
  const packages = await (await request.get(`/api/packages?case_id=${value.id}`)).json();
  expect(packages).toHaveLength(1);
  // Switching the case's manual export choice must not regenerate the project
  // default. Observe multiple real scheduler scans after each UI change.
  for (const preference of ['insurer-v2', 'picc-record-v1']) {
    await page.getByLabel('保司资料要求', { exact: true }).selectOption(preference);
    await expect(page.getByLabel('保司资料要求', { exact: true })).toHaveValue(preference);
    let observations = 0;
    await expect.poll(async () => {
      const status = await (await request.get(`/api/cases/${value.id}/default-package`)).json();
      expect(status.state).toBe('ready');
      expect(status.package.id).toBe(packages[0].id);
      expect(await (await request.get(`/api/packages?case_id=${value.id}`)).json()).toHaveLength(1);
      return ++observations;
    }, { intervals: [1000], timeout: 12000 }).toBe(7);
    await expect(link).toBeVisible();
  }
  for (const width of [1366, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await card.scrollIntoViewIfNeeded();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
    await page.screenshot({ path: testInfo.outputPath(`default-ready-${width}.png`), fullPage: true, animations: 'disabled' });
  }
});
