import { test, expect, type Page } from '@playwright/test';
import fs from 'node:fs/promises';
import path from 'node:path';

const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAHgAAABkCAIAAADCEmNlAAAA80lEQVR4nO3SQQ3AMAADsazIC30oei+bQKRTvu2O906wgdAdj44IHRE6InRE6IjQEaEjQkeEjggdEToidEToiNARoSNCR4SOCB0ROiJ0ROiI0BGhI0JHhI4IHRE6InRE6IjQEaEjQkeEjggdEToidEToiNARoSNCR4SOCB0ROiJ0ROiI0BGhI0JHhI4IHRE6InRE6IjQEaEjQkeEjggdEToidEToiNARoSNCR4SOCB0ROiJ0ROiI0BGhI0JHhI4IHRE6InRE6IjQEaEjQkeEjggdEToidEToiNARoSNCR4SOCB0ROiJ0ROiI0BGhI0JHhF7jByp2AUhQ6fjGAAAAAElFTkSuQmCC', 'base64');
const headers = { 'X-IDI-Request': '1' };
async function utility(page: Page, name: string) {
  if (['系统设置', '资料要求', '照片文件夹'].includes(name)) {
    await page.getByRole('button', { name: '系统设置', exact: true }).click();
    await page.getByRole('navigation', { name: '设置分类' }).getByRole('button', { name: name === '照片文件夹' ? '连接与收图' : '项目与资料', exact: true }).click();
    if (name === '资料要求') await page.getByRole('button', { name: '资料要求', exact: true }).click();
    return;
  }
  await page.locator('.utility-menu summary').click();
  await page.locator('.utility-menu').getByRole('button', { name, exact: true }).click();
}
async function externalResult(page: Page, result: string) {
  await page.getByRole('button', { name: '办理记录', exact: true }).click();
  await page.getByRole('button', { name: '记录外部办理结果', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByRole('combobox', { name: '办理结果', exact: true }).selectOption(result);
  await dialog.getByLabel('实际办理时间').fill('2026-10-15T10:00');
  await dialog.getByLabel('办理凭据或说明').fill('浏览器验收：实际外部办理记录');
  await dialog.getByRole('button', { name: '保存办理记录' }).click();
  await expect(dialog).toHaveCount(0);
}

test('create case, upload, review, select, export, receive, and restore saved views', async ({ page, request }, testInfo) => {
  const auth = await (await request.get('/api/auth/status')).json();
  const login = await request.post(auth.setup_required ? '/api/auth/setup' : '/api/auth/login', {
    headers, data: { username: 'upload-ui-admin', password: 'isolated-upload-ui-password', display_name: '隔离测试管理员' },
  });
  expect(login.ok()).toBeTruthy();
  await page.context().addCookies((await request.storageState()).cookies);
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/');
  await page.getByRole('button', { name: '新建项目', exact: true }).click();
  let dialog = page.getByRole('dialog');
  await dialog.getByLabel('项目 / 小区名称').fill('浏览器验收项目');
  await dialog.getByLabel('项目别名').fill('验收别名');
  await dialog.getByRole('button', { name: '保存项目' }).click();
  await expect(dialog).toHaveCount(0);
  await page.getByRole('button', { name: '新建案件', exact: true }).first().click();
  dialog = page.getByRole('dialog');
  await dialog.getByRole('combobox', { name: '所属项目', exact: true }).selectOption({ label: '浏览器验收项目' });
  await dialog.getByLabel('案件内容', { exact: true }).fill('客厅油漆起皮');
  await dialog.getByLabel('栋号', { exact: true }).fill('1');
  await dialog.getByLabel('房号 / 区域').fill('201');
  await dialog.getByLabel('维修部位', { exact: true }).fill('客厅沙发旁');
  await dialog.getByLabel('报修编号（平台）', { exact: true }).fill('ZLBH202610132005010577');
  await expect(dialog.getByText('资料编号', { exact: true })).toHaveCount(0);
  await dialog.getByRole('button', { name: '保存案件' }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole('heading', { name: 'LLQYSXM-1-201-01', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '本案照片', exact: true }).click();
  await page.getByRole('button', { name: '导入资料', exact: true }).first().click();
  dialog = page.getByRole('dialog');
  await dialog.locator('input[type=file]').first().setInputFiles({ name: '施工过程.png', mimeType: 'image/png', buffer: png });
  await dialog.getByRole('checkbox', { name: '导入后进行本地OCR识别' }).uncheck();
  await dialog.getByRole('button', { name: '开始导入' }).click();
  await expect(page.locator('.photo-table tbody tr')).toHaveCount(1);
  await expect(page.locator('.photo-table thead th').first()).toContainText('缩略图');
  await page.getByRole('button', { name: '显示列', exact: true }).click();
  await page.getByRole('checkbox', { name: '原文件名', exact: true }).check();
  await page.getByRole('button', { name: '显示列', exact: true }).click();
  await page.getByRole('button', { name: '查看施工过程.png', exact: true }).click();
  dialog = page.getByRole('dialog');
  await dialog.getByRole('combobox', { name: '照片分类', exact: true }).selectOption('construction');
  await dialog.getByLabel('维修部位', { exact: true }).fill('客厅沙发旁');
  await dialog.getByLabel('施工内容', { exact: true }).fill('打磨刷油漆');
  await dialog.getByLabel('水印时间', { exact: true }).fill('2026-09-22T16:50');
  await dialog.getByRole('button', { name: '保存并确认', exact: true }).click();
  await expect(dialog.getByText('人工已确认', { exact: true })).toBeVisible();
  await dialog.getByRole('button', { name: '来源', exact: true }).click();
  await expect(dialog.getByText('批量上传', { exact: true })).toBeVisible();
  await dialog.getByRole('button', { name: '修改记录', exact: true }).click();
  await expect(dialog.locator('summary').filter({ hasText: '人工确认' })).toBeVisible();
  await dialog.getByRole('button', { name: '关闭', exact: true }).last().click();
  await page.getByLabel('搜索照片', { exact: true }).fill('无匹配内容');
  await expect(page.getByText('没有符合当前条件的资料', { exact: true })).toBeVisible();
  await page.getByLabel('搜索照片', { exact: true }).fill('');
  await expect(page.locator('.photo-table tbody tr')).toHaveCount(1);
  await page.getByRole('checkbox', { name: '全选本页', exact: true }).check();
  await page.getByRole('button', { name: '批量核对', exact: true }).click();
  dialog = page.getByRole('dialog');
  await dialog.getByRole('checkbox', { name: '将当前信息标为人工已确认', exact: true }).check();
  await dialog.getByRole('button', { name: '应用到所选资料', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await page.getByRole('button', { name: '提取所选', exact: true }).click();
  dialog = page.getByRole('dialog');
  await expect(dialog.getByText(/^打包1张照片，涉及1个案件/)).toBeVisible();
  await expect(dialog.getByText('父文件夹：浏览器验收项目-1-201-客厅油漆起皮-ZLBH202610132005010577')).toBeVisible();
  await expect(dialog.getByText(/目录：查勘照片/)).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('photo-package-preview.png'), fullPage: true });
  await dialog.getByRole('button', { name: '生成ZIP资料包', exact: true }).click();
  await expect(page.getByRole('link', { name: '下载', exact: true })).toBeVisible();
  const downloaded = page.waitForEvent('download');
  await page.getByRole('link', { name: '下载', exact: true }).click();
  await (await downloaded).saveAs(testInfo.outputPath('package.zip'));
  await page.getByRole('button', { name: '登记进展', exact: true }).click();
  dialog = page.getByRole('dialog');
  await dialog.getByRole('combobox', { name: '进展', exact: true }).selectOption('accepted');
  await dialog.getByLabel('实际时间', { exact: true }).fill('2026-10-15T10:30');
  await dialog.getByLabel('说明', { exact: true }).fill('验收测试：资料收件');
  await dialog.getByLabel('回执附件', { exact: true }).setInputFiles({ name: '回执.txt', mimeType: 'text/plain', buffer: Buffer.from('received') });
  await dialog.getByRole('button', { name: '保存记录', exact: true }).click();
  await expect(page.locator('.reporting-table').getByText('已接收', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '客厅油漆起皮', exact: true }).click();
  dialog = page.getByRole('dialog');
  await expect(dialog.getByRole('link', { name: '下载回执附件', exact: true })).toBeVisible();
  await dialog.getByRole('button', { name: '关闭', exact: true }).last().click();
  const cases = await (await request.get('/api/cases')).json();
  expect(cases[0].status).toBe('pending');
  await page.reload();
  await utility(page, '全部照片');
  await expect(page.locator('.photo-table thead')).toContainText('原文件名');
  await page.screenshot({ path: testInfo.outputPath('workbench.png'), fullPage: true });
  expect(errors).toEqual([]);
});

test('site survey acceptance, ledger identifiers, visits, insurer requirements, and folder collection', async ({ page, request }, testInfo) => {
  const project = await (await request.post('/api/projects', { headers, data: { name: '流程验收项目' } })).json();
  await page.goto('/');
  await expect(page.getByRole('button', { name: '流程验收项目 0', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '导入照片', exact: true }).click();
  let dialog = page.getByRole('dialog');
  await dialog.getByRole('button', { name: '案件台账', exact: true }).click();
  await dialog.getByRole('combobox', { name: '所属项目', exact: true }).selectOption(project.id);
  await dialog.locator('input[type=file]').first().setInputFiles({ name: '台账.csv', mimeType: 'text/csv', buffer: Buffer.from('\uFEFF案件内容,报案号,保单号,报修编号,栋号,房号\n卫生间渗漏,01234567890123456789,000000123,ZLBH-UI-02,1,202\n') });
  await dialog.getByRole('button', { name: '预览台账' }).click();
  await expect(dialog.getByText('01234567890123456789', { exact: true })).toBeVisible();
  await dialog.getByRole('button', { name: '导入1行有效记录' }).click();
  let item: { id: string; short_code: string };
  await expect.poll(async () => { const cases = await (await request.get('/api/cases?query=ZLBH-UI-02')).json(); item = cases[0]; return cases.length; }).toBe(1);
  await page.locator(`[data-case-id="${item!.id}"] .case-link`).click();
  await expect(page.locator('.case-identifiers')).toContainText('000000123');
  await page.getByRole('button', { name: '水印填写内容' }).click();
  dialog = page.getByRole('dialog');
  await expect(dialog.locator('pre')).toContainText('编号：' + item!.short_code);
  await expect(dialog.getByText('XXX问题', { exact: true })).toBeVisible();
  await dialog.getByRole('button', { name: '关闭', exact: true }).last().click();
  await externalResult(page, 'accepted');
  await expect(page.locator('.workflow-steps button')).toHaveCount(4);
  await expect(page.locator('.case-waiting')).toContainText('当前办理环节：现场查勘');
  await page.getByRole('button', { name: '前往查勘维修', exact: true }).click();
  await page.getByRole('button', { name: /^保司资料/ }).click();
  await page.locator('.case-detail-heading').getByRole('button', { name: '进场与维修记录', exact: true }).click();
  dialog = page.getByRole('dialog');
  await dialog.getByLabel('进场日期', { exact: true }).fill('2026-10-15');
  await dialog.getByLabel('本次标记', { exact: true }).fill('第二次上门');
  await dialog.getByRole('button', { name: '建立进场记录', exact: true }).click();
  await expect(dialog.locator('.visit-record-list').getByText('2026-10-15 · 第二次上门', { exact: true })).toBeVisible();
  await dialog.getByRole('button', { name: '关闭', exact: true }).last().click();
  await utility(page, '资料要求');
  await page.getByRole('button', { name: '新增资料要求' }).click();
  dialog = page.getByRole('dialog');
  await dialog.getByLabel('名称', { exact: true }).fill('测试保司过程要求');
  await dialog.getByRole('button', { name: '增加要求' }).click();
  await dialog.locator('tbody select').selectOption('construction');
  await dialog.locator('input[placeholder="例如：防水"]').fill('防水');
  await dialog.getByRole('button', { name: '保存要求' }).click();
  await expect(page.getByText('测试保司过程要求', { exact: true })).toBeVisible();
  await page.getByRole('navigation', { name: '设置分类' }).getByRole('button', { name: '识别与模型', exact: true }).click();
  await expect(page.getByRole('checkbox', { name: '启用视觉模型接口，供照片识别和项目审核调用', exact: true })).not.toBeChecked();
  const inbox = path.resolve(testInfo.outputPath('folder-inbox'));
  await fs.mkdir(inbox, { recursive: true });
  await utility(page, '照片文件夹');
  await page.getByRole('button', { name: '新增文件夹' }).click();
  dialog = page.getByRole('dialog');
  await dialog.getByLabel('名称', { exact: true }).fill('浏览器收图文件夹');
  await dialog.getByLabel('照片文件夹路径').fill(inbox);
  await dialog.getByLabel('固定案件').selectOption(item!.id);
  await dialog.getByRole('checkbox', { name: '收到图片后进行本地OCR' }).uncheck();
  await dialog.getByRole('checkbox', { name: '启用此入口' }).check();
  await dialog.getByRole('button', { name: '保存配置' }).click();
  await fs.writeFile(path.join(inbox, '新收照片.png'), png);
  await expect.poll(async () => (await (await request.get(`/api/photos?case_id=${item!.id}`)).json()).total, { timeout: 20000 }).toBe(1);
  const photos = await (await request.get(`/api/photos?case_id=${item!.id}`)).json();
  await request.patch(`/api/photos/${photos.items[0].id}`, { headers, data: { photo_group: 'survey_problem', confirm: true } });
  await page.getByRole('navigation', { name: '工作看板' }).getByRole('button', { name: '查勘维修', exact: true }).click();
  await page.getByRole('button', { name: item!.short_code + ' 查勘问题 1张', exact: true }).click();
  await expect(page.locator('.photo-table tbody tr')).toHaveCount(1);
  await expect(page.locator('.photo-table')).toContainText('查勘问题');
});

test('desktop and narrow boards, navigation, and a recoverable photos error', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/');
  for (const width of [1440, 1100, 736, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await expect(page.locator('.app-header')).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${width}px`).toBe(true);
  }
  await page.getByRole('button', { name: '展开快速导航', exact: true }).click();
  await page.getByRole('navigation', { name: '工作看板' }).getByRole('button', { name: '查勘维修', exact: true }).click();
  await expect(page.getByRole('heading', { name: '查勘维修', exact: true })).toBeVisible();
  await utility(page, '全部照片');
  await page.route('**/api/photos?**', route => route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: '模拟暂时连接失败' }) }));
  await page.getByRole('button', { name: '刷新', exact: true }).click();
  await expect(page.getByRole('alert').filter({ hasText: '模拟暂时连接失败' })).toBeVisible();
  expect(errors).toEqual([]);
});

test('directory tree follows later imports and case edits, exposes conflicts, and opens from the workbench', async ({ page, request }, testInfo) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  const project = await (await request.post('/api/projects', { headers, data: { name: '目录验收项目' } })).json();
  const caseItem = await (await request.post('/api/cases', { headers, data: { project_id: project.id, title: '墙面起皮', building: '2', room: '301' } })).json();
  await request.post('/api/import/upload', { headers, multipart: { files: { name: '后续照片.png', mimeType: 'image/png', buffer: png }, auto_ocr: 'false', project_id: project.id } });
  let photoId = '';
  await expect.poll(async () => {
    const value = await (await request.get(`/api/photos?project_id=${project.id}`)).json();
    photoId = value.items[0]?.id || '';
    return value.total;
  }).toBe(1);
  await page.goto('/');
  await utility(page, '系统设置');
  await page.getByRole('button', { name: '资料目录', exact: true }).click();
  await expect(page.getByTestId('archive-tree-state')).toHaveText('已同步');
  const tree = await (await request.get('/api/archive-tree')).json();
  async function photoCopies() {
    return (await fs.readdir(tree.root, { recursive: true })).filter(p => p.endsWith('.png') && p.includes(photoId.slice(0, 12)));
  }
  await expect.poll(photoCopies).toHaveLength(1);
  const [pendingPath] = await photoCopies();
  expect(pendingPath).toContain('待归档');
  expect(await fs.readFile(path.join(tree.root, pendingPath))).toEqual(png);
  await request.patch(`/api/photos/${photoId}`, { headers, data: { case_id: caseItem.id, stage: 'work' } });
  await expect.poll(async () => (await photoCopies()).join('/')).toContain('施工');
  await expect.poll(async () => (await photoCopies()).includes(pendingPath)).toBe(false);
  await request.patch(`/api/cases/${caseItem.id}`, { headers, data: { title: '墙面修复' } });
  await expect.poll(async () => (await photoCopies()).join('/')).toContain('墙面修复');
  await expect.poll(async () => (await photoCopies()).length).toBe(1);
  const [currentPath] = await photoCopies();
  const current = path.join(tree.root, currentPath);
  await fs.writeFile(current, '人工标注副本');
  await page.getByRole('button', { name: '重新同步目录', exact: true }).click();
  await expect(page.getByText(/目录中的副本已被手动修改/)).toBeVisible();
  expect(await fs.readFile(current, 'utf8')).toBe('人工标注副本');
  const original = await request.get(`/api/photos/${photoId}/original`);
  expect(await original.body()).toEqual(png);
  await fs.unlink(current);
  await page.getByRole('button', { name: '重新同步目录', exact: true }).click();
  await expect(page.getByTestId('archive-tree-state')).toHaveText('已同步');
  expect(await fs.readFile(current)).toEqual(png);
  let openRequests = 0;
  await page.route('**/api/archive-tree/open', async route => { openRequests++; await route.fulfill({ json: { path: tree.root } }); });
  await page.getByRole('button', { name: '打开资料目录', exact: true }).click();
  await expect.poll(() => openRequests).toBe(1);
  await page.screenshot({ path: testInfo.outputPath('archive-tree-settings.png'), fullPage: true });
  expect(errors).toEqual([]);
});


test('platform material preparation saves by stage and keeps PDFs separate from finished photos', async ({ page, request }, testInfo) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  const project = await (await request.post('/api/projects', { headers, data: { name: '官网字段验收项目' } })).json();
  const item = await (await request.post('/api/cases', { headers, data: { project_id: project.id, title: '墙面修复官网资料', building: '3', room: '501' } })).json();
  await request.post('/api/import/upload', { headers, multipart: { files: { name: '修复后照片.png', mimeType: 'image/png', buffer: png }, auto_ocr: 'false', case_id: item.id } });
  await request.post('/api/import/upload', { headers, multipart: { files: { name: '业主验收材料.pdf', mimeType: 'application/pdf', buffer: Buffer.from('%PDF-1.4\nfixture\n%%EOF') }, auto_ocr: 'false', case_id: item.id } });
  let imageId = '';
  await expect.poll(async () => { const result = await (await request.get(`/api/photos?case_id=${item.id}`)).json(); imageId = result.items.find((p: { media_type: string }) => p.media_type === 'image')?.id || ''; return result.total; }).toBe(2);
  await request.patch(`/api/photos/${imageId}`, { headers, data: { stage: 'acceptance', role: 'finished' } });
  async function advance(action: string) {
    const current = await (await request.get(`/api/workflow/cases/${item.id}`)).json();
    const result = await request.post(`/api/workflow/cases/${item.id}/result`, { headers, data: {
      action, revision: current.case.revision, event_at: '2026-10-15T10:00', note: '浏览器验收：已完成外部办理',
    } });
    expect(result.ok()).toBe(true);
  }
  async function openWork() {
    await page.goto('/');
    await page.getByRole('navigation', { name: '工作看板' }).getByRole('button', { name: '查勘维修', exact: true }).click();
    await page.locator(`[data-case-id="${item.id}"]`).getByRole('button', { name: '处理', exact: true }).click();
  }
  await page.goto('/');
  await page.locator(`[data-case-id="${item.id}"] .case-link`).click();
  await expect(page.getByRole('button', { name: '现场查勘', exact: true })).toBeDisabled();
  await advance('accepted');
  await openWork();
  await expect(page.getByRole('heading', { name: '现场查勘 · 业务信息' })).toBeVisible();
  await page.getByLabel('维修方式', { exact: true }).selectOption('3');
  await page.getByLabel('零注拒原因').fill('仅本地测试填写的业务说明');
  await expect(page.getByLabel('计划维修日期')).toHaveCount(0);
  await page.getByRole('button', { name: '保存本地草稿' }).click();
  await expect(page.getByText('已保存本地官网资料草稿', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '修复成果', exact: true })).toBeDisabled();
  await advance('survey_completed');
  await openWork();
  await expect(page.getByRole('heading', { name: '修复成果 · 业务信息' })).toBeVisible();
  await page.getByRole('button', { name: '现场查勘', exact: true }).click();
  await expect(page.getByRole('button', { name: '保存本地草稿' })).toHaveCount(0);
  await expect(page.locator('.platform-form input, .platform-form select, .platform-form textarea')).toHaveCount(0);
  await expect(page.locator('.platform-output').filter({ hasText: '仅本地测试填写的业务说明' })).toBeVisible();
  await page.getByRole('button', { name: '修复成果', exact: true }).click();
  await page.getByRole('button', { name: '采用本环节照片建议' }).click();
  await expect(page.locator('[data-field="recoveredFileInfo"] tbody tr')).toHaveCount(1);
  await page.getByLabel('进场维修人数').fill('2');
  await page.getByLabel('进场开工时间').fill('2026-09-22T10:00');
  await page.getByLabel('完工时间', { exact: true }).fill('2026-09-23T12:00');
  await expect(page.locator('.platform-output').filter({ hasText: /^2$/ })).toBeVisible();
  await page.getByRole('button', { name: '保存本地草稿' }).click();
  await expect(page.getByRole('button', { name: '维修成果验收', exact: true })).toBeDisabled();
  await advance('repair_completed');
  await openWork();
  await expect(page.getByRole('heading', { name: '维修成果验收 · 业务信息' })).toBeVisible();
  const ownerField = page.locator('[data-field="acceptanceCheckFileInfo"]');
  await expect(ownerField.getByText('尚未选择本地文件', { exact: true })).toBeVisible();
  await ownerField.getByRole('button', { name: '选择文件', exact: true }).click();
  let dialog = page.getByRole('dialog');
  await expect(dialog.getByRole('checkbox', { name: '选择修复后照片.png', exact: true })).toHaveCount(0);
  await dialog.getByRole('checkbox', { name: '选择业主验收材料.pdf', exact: true }).check();
  await dialog.getByRole('button', { name: '使用所选资料' }).click();
  await page.getByLabel('验收时间', { exact: true }).fill('2026-09-24T16:30');
  await expect(ownerField.locator('tbody tr')).toHaveCount(1);
  await page.getByRole('button', { name: '保存本地草稿' }).click();
  const downloadEvent = page.waitForEvent('download');
  await page.getByRole('button', { name: '下载按栏位资料' }).click();
  const download = await downloadEvent;
  expect(download.suggestedFilename()).toContain('本地准备');
  await download.saveAs(testInfo.outputPath('platform-materials.zip'));
  await expect(page.locator('.platform-downloads')).toContainText('未上传官网');
  await page.getByLabel('验收时间', { exact: true }).fill('2026-09-24T17:00');
  await page.getByRole('button', { name: '返回看板', exact: true }).click();
  await expect(page.getByRole('heading', { name: '查勘维修', exact: true })).toBeVisible();
  const saved = await (await request.get(`/api/platform/cases/${item.id}/stages/acceptance`)).json();
  expect(saved.draft.values.acceptanceDate).toBe('2026-09-24T17:00');
  await page.locator(`[data-case-id="${item.id}"] .case-link`).click();
  await page.getByRole('button', { name: '关联 / 核对案件' }).click();
  dialog = page.getByRole('dialog');
  await dialog.getByText('手动记录编号（保留为未核对状态）').click();
  await dialog.getByLabel('报修编号（平台）', { exact: true }).fill('ZLBH-LOCAL-TEST');
  await dialog.getByLabel('报案编号（保司）', { exact: true }).fill('0001234567890123456789');
  await dialog.getByRole('button', { name: '仅记录编号' }).click();
  await expect(page.locator('.platform-binding')).toContainText('ZLBH-LOCAL-TEST');
  await expect(page.locator('.platform-binding')).toContainText('0001234567890123456789');
  await expect(page.getByRole('button', { name: '预提交审核', exact: true })).toBeDisabled();
  await page.screenshot({ path: testInfo.outputPath('platform-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(() => page.locator('.sidebar').evaluate(el => el.getBoundingClientRect().right)).toBeLessThanOrEqual(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('platform-mobile.png'), fullPage: true });
  expect(errors).toEqual([]);
});

test('photo sources stay separate and both header rows remain aligned while scrolling', async ({ page, request }, testInfo) => {
  const project = await (await request.post('/api/projects', { headers, data: { name: '照片对照验收项目' } })).json();
  const item = await (await request.post('/api/cases', { headers, data: { project_id: project.id, title: '对照照片', building: '1', room: '101' } })).json();
  await request.post(`/api/workflow/cases/${item.id}/result`, { headers, data: { action: 'accepted', revision: item.revision, event_at: '2026-10-16T09:00', note: '隔离浏览器测试' } });
  const data = await (await request.get(`/api/workflow/board?board=work&source=local&query=${encodeURIComponent(item.short_code)}`)).json();
  const sample = data.items[0];
  expect(sample.online_photos.groups.survey_problem.state).toBe('unlinked');
  const known = (count: number, videos = 0) => ({ state: 'known', count, videos, other: 0, unknown: 0, note: '测试官网附件快照' });
  const groups = { survey_checkin: known(2), survey_problem: known(0, 1), work_checkin: { ...known(0), state: 'unread', count: null },
    construction: { ...known(0), state: 'unsupported', count: null }, completion: { ...known(0), state: 'partial', count: 1, unknown: 1 } };
  await page.route('**/api/workflow/board?**', async route => {
    if (new URL(route.request().url()).searchParams.get('board') !== 'work') { await route.continue(); return; }
    await route.fulfill({ json: { ...data, total: 30, source_total: 30, items: Array.from({ length: 30 }, (_, i) => ({
      ...sample, id: i ? `fixture-${i}` : sample.id, short_code: i ? `CASE-${i}` : sample.short_code,
      online_photos: i === 1 ? sample.online_photos : { read_at: '2026-10-16T09:00:00+08:00', groups },
    })) } });
  });
  await page.goto('/');
  await page.getByRole('navigation', { name: '工作看板' }).getByRole('button', { name: '查勘维修', exact: true }).click();
  const row = page.locator(`[data-case-id="${item.id}"]`);
  await expect(page.locator('.case-photo-group')).toHaveCount(30);
  await expect(row.locator(':scope > tr')).toHaveCount(2);
  await expect(row.locator('[data-photo-source="official"] [data-photo-group="survey_checkin"]')).toHaveText('2');
  await expect(row.locator('[data-photo-source="official"] [data-photo-group="survey_problem"]')).toContainText('1 视频');
  await expect(row.locator('[data-photo-source="official"] [data-photo-group="work_checkin"]')).toHaveText('未读取');
  await expect(row.locator('[data-photo-source="official"] [data-photo-group="construction"]')).toHaveText('—');
  await expect(row.locator('[data-photo-source="official"] [data-photo-group="completion"]')).toHaveText('≥1');
  await expect(row.locator('[data-photo-source="local"] [data-photo-group="survey_checkin"]')).toHaveText('0');
  await expect(page.locator('[data-case-id="fixture-1"] [data-photo-source="official"] [data-photo-group="survey_checkin"]')).toHaveText('未关联');
  const measure = () => page.locator('.board-table-wrap').evaluate(wrap => {
    const rect = (selector: string) => { const b = wrap.querySelector(selector)!.getBoundingClientRect(); return { x: b.x, y: b.y, bottom: b.bottom, width: b.width }; };
    return { scrollTop: wrap.scrollTop, scrollLeft: wrap.scrollLeft, head: rect('thead'), group: rect('thead tr:first-child .photo-count-heading'),
      photo: rect('thead tr:nth-child(2) th'), body: rect('tbody:first-of-type'), cell: rect('tbody:first-of-type [data-photo-group="survey_checkin"]') };
  });
  const before = await measure();
  await page.locator('.board-table-wrap').evaluate(wrap => { wrap.scrollTop = 180; wrap.scrollLeft = 160; });
  await expect.poll(async () => (await measure()).scrollTop).toBe(180);
  const after = await measure();
  expect(Math.abs(after.head.y - before.head.y)).toBeLessThan(1);
  expect(after.group.bottom).toBeLessThanOrEqual(after.photo.y + 1);
  expect(after.body.y).toBeLessThan(before.body.y - 170);
  expect(Math.abs(after.photo.x - after.cell.x)).toBeLessThan(1);
  expect(after.scrollLeft).toBeGreaterThan(0);
  await page.screenshot({ path: testInfo.outputPath('photo-source-scroll.png') });
  for (const width of [1100, 736, 390]) {
    await page.setViewportSize({ width, height: 900 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    const geometry = await measure();
    expect(geometry.group.bottom).toBeLessThanOrEqual(geometry.photo.y + 1);
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.locator('.board-table-wrap').evaluate(wrap => { wrap.scrollTop = 0; wrap.scrollLeft = 0; });
  await row.locator('[data-photo-source="local"] [data-photo-group="survey_checkin"] button').click();
  await expect(page.getByRole('heading', { name: '照片资料', exact: true })).toBeVisible();
  await expect(page.getByText('没有符合当前条件的资料', { exact: true })).toBeVisible();
});
