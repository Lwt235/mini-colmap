/** Real Web reconstruction, workload equality, network cadence and idle rendering. */
import { chromium } from 'playwright';
import assert from 'node:assert/strict';
import { readFile, mkdir, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const root = fileURLToPath(new URL('../../', import.meta.url));
const base = process.env.WORKBENCH_URL || 'http://127.0.0.1:8765';
assert(['127.0.0.1', 'localhost'].includes(new URL(base).hostname));
const baseline = path.join(root, 'outputs', 'ui_20261017_102109_14c560');
const source = JSON.parse(await readFile(path.join(baseline, 'run_status.json'), 'utf8'));
const config = JSON.parse(await readFile(path.join(baseline, 'config.json'), 'utf8'));
const artifacts = path.join(root, 'reports', 'web-performance', 'browser');
await mkdir(artifacts, { recursive: true });
const report = { baseline, requests: {} };
const browser = await chromium.launch({
  channel: 'msedge',
  headless: true,
  args: ['--use-angle=swiftshader'],
});
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
await page.addInitScript(() => {
  window.__drawCalls = 0;
  for (const name of [
    'drawArrays',
    'drawElements',
    'drawArraysInstanced',
    'drawElementsInstanced',
  ]) {
    const original = WebGL2RenderingContext.prototype[name];
    if (!original) continue;
    WebGL2RenderingContext.prototype[name] = function (...args) {
      window.__drawCalls++;
      return original.apply(this, args);
    };
  }
});
const errors = [];
page.on('pageerror', (e) => errors.push(String(e)));
let ownedRun;
async function get(url) {
  const response = await page.request.get(base + url);
  assert(response.ok(), `${url}: ${response.status()}`);
  return response.json();
}
page.on('request', (request) => {
  const pathname = new URL(request.url()).pathname;
  if (ownedRun && pathname.includes(ownedRun)) {
    const key = pathname.replace(ownedRun, ':run');
    report.requests[key] = (report.requests[key] || 0) + 1;
  } else if (ownedRun && pathname === '/api/runs' && request.method() === 'GET') {
    report.historyPolls = (report.historyPolls || 0) + 1;
  }
});
try {
  const bootstrap = await get('/api/bootstrap');
  assert(!bootstrap.runs.some((r) => r.status === 'running'), 'Another task is active');
  const dataset = bootstrap.datasets.find((d) => d.path === source.dataset);
  assert(dataset, 'Missing original 47-image dataset');
  await page.goto(base + "/#workspace");
  await page.getByLabel('选择输入数据集', { exact: true }).selectOption(dataset.id);
  await page.getByRole('button', { name: '参数', exact: true }).click();
  await page
    .locator('.config-panel input[type="file"]')
    .setInputFiles(path.join(baseline, 'config.json'));
  const started = page.waitForResponse(
    (r) => r.url().endsWith('/api/runs') && r.request().method() === 'POST',
  );
  await page.getByTestId('start-run').click();
  const response = await started;
  assert.deepEqual(response.request().postDataJSON().config, config);
  ownedRun = (await response.json()).id;
  report.runId = ownedRun;
  console.log(`Started real Web run ${ownedRun}`);
  const deadline = Date.now() + 600000;
  let run, previousStage;
  do {
    run = await get(`/api/runs/${ownedRun}`);
    if (run.stage !== previousStage) {
      console.log(`${run.stage}: ${run.elapsed_seconds.toFixed(2)} s`);
      previousStage = run.stage;
    }
    if (run.status !== 'running') break;
    await new Promise((resolve) => setTimeout(resolve, 3000));
  } while (Date.now() < deadline);
  assert.equal(run.status, 'completed', JSON.stringify(run));
  assert.equal(run.registered_images, 47);
  const reports = await get(`/api/runs/${ownedRun}/reports`);
  report.timings = reports.timings;
  report.summary = reports.summary;
  report.output = run.output_path;
  report.runtime = await get(`/api/runs/${ownedRun}/artifacts/runtime.json`);
  assert.equal(report.runtime.qos_applied, true);
  const model = JSON.parse(await readFile(path.join(run.output_path, 'model.json'), 'utf8'));
  const expected = JSON.parse(await readFile(path.join(baseline, 'model.json'), 'utf8'));
  assert.deepEqual(model, expected);
  report.finalModelExactlyEqual = true;
  await page.locator('.scene-breadcrumb .run-state.completed').waitFor({ timeout: 30000 });
  await page.waitForFunction(() =>
    document.querySelector('.scene-stats')?.textContent.includes('6,570'),
  );
  await page.waitForFunction(
    () =>
      document
        .querySelectorAll('.scene-stats strong')[1]
        ?.textContent.split('/')
        .map(Number)
        .join('/') === '47/47',
  );
  await page.locator('.scene-breadcrumb .spin').waitFor({ state: 'hidden' });
  await page.waitForTimeout(1500);
  const first = await page.evaluate(() => window.__drawCalls);
  await page.waitForTimeout(1000);
  report.idleDrawCalls = (await page.evaluate(() => window.__drawCalls)) - first;
  assert(report.idleDrawCalls <= 2, `Idle view rendered ${report.idleDrawCalls} times`);
  const canvas = page.getByTestId('scene-canvas');
  const before = createHash('sha256')
    .update(await canvas.screenshot())
    .digest('hex');
  const box = await canvas.boundingBox();
  await page.mouse.move(box.x + box.width * 0.6, box.y + box.height * 0.6);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * 0.6 + 95, box.y + box.height * 0.6 + 35, { steps: 8 });
  await page.mouse.up();
  await page.waitForTimeout(1000);
  const after = createHash('sha256')
    .update(await canvas.screenshot())
    .digest('hex');
  assert.notEqual(before, after);
  report.orbitStillRenders = true;
  assert.equal(report.historyPolls || 0, 0);
  assert((report.requests['/api/runs/:run/reports'] || 0) < 20);
  assert((report.requests['/api/runs/:run/scene'] || 0) <= run.snapshots.length + 6);
  assert.deepEqual(errors, []);
  await page.screenshot({ path: path.join(artifacts, 'completed.png'), fullPage: true });
  report.passed = true;
} catch (error) {
  report.error = String(error.stack || error);
  await page
    .screenshot({ path: path.join(artifacts, 'failure.png'), fullPage: true })
    .catch(() => {});
  throw error;
} finally {
  await writeFile(path.join(artifacts, 'report.json'), JSON.stringify(report, null, 2));
  if (ownedRun) {
    const run = await get(`/api/runs/${ownedRun}`).catch(() => null);
    if (run?.status === 'running') await page.request.post(base + `/api/runs/${ownedRun}/cancel`);
  }
  await browser.close();
}
console.log(
  JSON.stringify({ passed: true, timings: report.timings, requests: report.requests, artifacts }),
);
