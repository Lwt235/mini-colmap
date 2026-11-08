/** Exercise real optimization form/config transport, without starting GPU work. */
import assert from 'node:assert/strict';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';

const base = process.env.WORKBENCH_URL || 'http://127.0.0.1:8765';
assert(['127.0.0.1', 'localhost'].includes(new URL(base).hostname));
const artifacts = '../.cache/gaussian-optimization-config';
await mkdir(artifacts, { recursive: true });
const browser = await chromium.launch({
  channel: 'msedge',
  headless: true,
  args: ['--disable-webgl'],
});
try {
  const page = await browser.newPage({ viewport: { width: 1680, height: 1100 } });
  const failures = [];
  page.on('pageerror', (error) => failures.push(String(error)));
  const bootstrap = await (await page.request.get(base + '/api/bootstrap')).json();
  const source = bootstrap.runs.find((run) => run.pipeline === 'sfm' && run.status === 'completed');
  if (source)
    await page.addInitScript((id) => localStorage.setItem('minicolmap.lastRun', id), source.id);
  await page.goto(base + "/#workspace", { waitUntil: 'domcontentloaded' });
  await page.locator('.boot-overlay').waitFor({ state: 'detached', timeout: 90000 });
  const panel = page.getByLabel('NeRF 与 3DGS 工作区');
  if (!(await panel.isVisible()))
    await page.getByRole('button', { name: 'NeRF / 3DGS', exact: true }).click();
  await panel.getByRole('button', { name: '默认', exact: true }).click();
  await panel.getByLabel('渲染训练方法').selectOption('3dgs');
  await panel.getByLabel('增密时的 Adam 状态').selectOption('preserve');
  await panel.getByLabel('高斯学习率策略').selectOption('grouped');
  await panel.getByLabel('密度控制策略').selectOption('adaptive');
  await panel.getByLabel('图像训练损失').selectOption('l1_ssim');
  await panel.getByText('优化与密度控制高级参数', { exact: true }).click();
  await panel.getByLabel('SSIM 损失权重').fill('0.25');
  await panel.getByLabel('自适应增密开始步').fill('200');
  await panel.getByLabel('自适应增密结束步').fill('2000');
  const downloading = page.waitForEvent('download');
  await panel.getByRole('button', { name: '导出配置', exact: true }).click();
  const exported = JSON.parse(await readFile(await (await downloading).path(), 'utf8'));
  assert.equal(exported.gaussian.optimizer_state, 'preserve');
  assert.equal(exported.gaussian.lr_mode, 'grouped');
  assert.equal(exported.gaussian.loss, 'l1_ssim');
  assert.equal(exported.gaussian.density_strategy, 'adaptive');
  assert.equal(exported.gaussian.ssim_weight, 0.25);
  const response = await page.request.post(base + '/api/rendering/config/validate', {
    data: exported,
  });
  assert(response.ok(), await response.text());
  assert.deepEqual((await response.json()).gaussian, exported.gaussian);
  const invalid = await page.request.post(base + '/api/rendering/config/validate', {
    data: { ...exported, gaussian: { ...exported.gaussian, densify_stop: 100 } },
  });
  assert.equal(invalid.status(), 422);
  await panel.screenshot({ path: artifacts + '/settings.png' });
  assert.deepEqual(failures, []);
  await writeFile(
    artifacts + '/report.json',
    JSON.stringify(
      { exported, valid_status: response.status(), invalid_status: invalid.status(), failures },
      null,
      2,
    ),
  );
  console.log(JSON.stringify({ status: 'passed', artifacts }));
} finally {
  await browser.close();
}
