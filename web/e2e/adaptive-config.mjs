/** Exercises UI config validation/import/export and one real 8-view job. */
import { chromium } from 'playwright';
import assert from 'node:assert/strict';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
const root = fileURLToPath(new URL('../../', import.meta.url));
const base = process.env.WORKBENCH_URL || 'http://127.0.0.1:8765';
assert(['127.0.0.1', 'localhost'].includes(new URL(base).hostname));
const artifacts = path.join(
  root,
  '.cache/adaptive-config-e2e',
  new Date().toISOString().replaceAll(':', '-'),
);
await mkdir(artifacts, { recursive: true });
const browser = await chromium.launch({
  channel: process.env.BROWSER_CHANNEL || 'msedge',
  headless: !process.argv.includes('--headed'),
  args: ['--use-angle=swiftshader'],
});
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
const errors = [],
  report = {};
let ownedRun;
page.on('pageerror', (error) => errors.push(String(error)));
async function get(url) {
  const response = await page.request.get(base + url);
  assert(response.ok());
  return response.json();
}
try {
  await page.goto(base + "/#workspace", { waitUntil: 'networkidle' });
  const bootstrap = await get('/api/bootstrap');
  assert(!bootstrap.runs.some((r) => r.status === 'running'), 'Another job is active');
  assert.equal(bootstrap.defaults.geometry.confidence, 0.99);
  assert.equal(bootstrap.defaults.geometry.min_trials, 20);
  const dataset = bootstrap.datasets.find((d) =>
    d.path.replaceAll('\\', '/').endsWith('middlebury_temple_ring/dataset.json'),
  );
  assert(dataset);
  await page.getByLabel('选择输入数据集', { exact: true }).selectOption(dataset.id);
  await page.getByRole('button', { name: '参数', exact: true }).click();
  const confidence = page.getByLabel('RANSAC 置信度', { exact: true });
  const minimum = page.getByLabel('最少采样次数', { exact: true });
  assert.equal(await confidence.inputValue(), '0.99');
  assert.equal(await minimum.inputValue(), '20');
  await confidence.fill('1');
  await page.getByRole('button', { name: '检查参数配置', exact: true }).click();
  await page.getByRole('alert').filter({ hasText: 'confidence' }).waitFor();
  report.rejectsInvalidConfidence = true;
  await confidence.fill('0.975');
  await minimum.fill('37');
  const validated = page.waitForResponse((r) => r.url().endsWith('/api/config/validate'));
  await page.getByRole('button', { name: '检查参数配置', exact: true }).click();
  const config = await (await validated).json();
  assert.equal(config.geometry.confidence, 0.975);
  assert.equal(config.geometry.min_trials, 37);
  const downloaded = page.waitForEvent('download');
  await page.locator('.config-panel').getByRole('button', { name: '导出', exact: true }).click();
  const download = await downloaded;
  await download.saveAs(path.join(artifacts, 'exported-config.json'));
  const exported = JSON.parse(await readFile(path.join(artifacts, 'exported-config.json'), 'utf8'));
  assert.equal(exported.geometry.confidence, 0.975);
  assert.equal(exported.geometry.min_trials, 37);
  report.exportsParameters = true;
  const legacyPath = path.join(artifacts, 'legacy-config.json');
  await writeFile(legacyPath, JSON.stringify({ geometry: { max_trials: 2000 } }));
  await page.locator('.config-panel input[type="file"]').setInputFiles(legacyPath);
  await page.waitForFunction(
    () => document.querySelector('input[aria-label="RANSAC 置信度"]')?.value === '0.99',
  );
  assert.equal(await minimum.inputValue(), '20');
  report.legacyImportFillsDefaults = true;
  await confidence.fill('0.975');
  await minimum.fill('37');
  const started = page.waitForResponse(
    (r) => r.url().endsWith('/api/runs') && r.request().method() === 'POST',
  );
  await page.getByTestId('start-run').click();
  const response = await started;
  const payload = response.request().postDataJSON();
  assert.equal(payload.config.geometry.confidence, 0.975);
  assert.equal(payload.config.geometry.min_trials, 37);
  ownedRun = (await response.json()).id;
  const deadline = Date.now() + 180000;
  let run;
  do {
    run = await get(`/api/runs/${ownedRun}`);
    if (run.status !== 'running') break;
    await new Promise((resolve) => setTimeout(resolve, 500));
  } while (Date.now() < deadline);
  assert.equal(run.status, 'completed', JSON.stringify(run));
  assert.equal(run.registered_images, 8);
  assert(run.point_count > 1000);
  const saved = await get(`/api/runs/${ownedRun}/artifacts/config.json`);
  assert.equal(saved.geometry.confidence, 0.975);
  assert.equal(saved.geometry.min_trials, 37);
  const reports = await get(`/api/runs/${ownedRun}/reports`);
  assert(reports.summary.mean_reprojection_error_px < 0.5);
  assert.equal(reports.summary.positive_depth_ratio, 1);
  report.realRun = {
    id: ownedRun,
    output: run.output_path,
    geometry: saved.geometry,
    summary: reports.summary,
  };
  await page.getByRole('button', { name: '参数', exact: true }).click();
  await confidence.scrollIntoViewIfNeeded();
  await page.waitForTimeout(500);
  await page.screenshot({ path: path.join(artifacts, 'adaptive-parameters.png'), fullPage: true });
  assert.deepEqual(errors, []);
  report.pageErrors = errors;
  report.status = 'passed';
} catch (error) {
  report.status = 'failed';
  report.error = String(error);
  process.exitCode = 1;
  await page
    .screenshot({ path: path.join(artifacts, 'failure.png'), fullPage: true })
    .catch(() => {});
} finally {
  try {
    if (ownedRun && (await get(`/api/runs/${ownedRun}`)).status === 'running')
      await page.request.post(base + `/api/runs/${ownedRun}/cancel`);
  } catch {
    // Preserve the report and close the browser if the local server disconnected.
  }
  await writeFile(path.join(artifacts, 'report.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
  console.log('Adaptive config artifacts:', artifacts);
  await browser.close();
}
