/** Read-only completion of graphical checks on the already reconstructed run. */
import { chromium } from 'playwright';
import assert from 'node:assert/strict';
import { readFile, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
const root = fileURLToPath(new URL('../../', import.meta.url));
const base = process.env.WORKBENCH_URL || 'http://127.0.0.1:8765';
const directory = path.join(root, 'reports/web-performance/browser');
const report = JSON.parse(await readFile(path.join(directory, 'report.json'), 'utf8'));
assert(report.runId && report.finalModelExactlyEqual);
const browser = await chromium.launch({
  channel: 'msedge',
  headless: true,
  args: ['--use-angle=swiftshader'],
});
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
const errors = [];
page.on('pageerror', (e) => errors.push(String(e)));
await page.addInitScript(() => {
  window.__drawCalls = 0;
  for (const name of [
    'drawArrays',
    'drawElements',
    'drawArraysInstanced',
    'drawElementsInstanced',
  ]) {
    const original = WebGL2RenderingContext.prototype[name];
    WebGL2RenderingContext.prototype[name] = function (...args) {
      window.__drawCalls++;
      return original.apply(this, args);
    };
  }
});
try {
  const started = Date.now();
  const response = await page.request.get(
    `${base}/api/runs/${report.runId}/artifacts/runtime.json`,
  );
  assert(response.ok());
  report.runtimeDownloadSeconds = (Date.now() - started) / 1000;
  assert.deepEqual(await response.json(), report.runtime);
  const runResponse = await page.request.get(`${base}/api/runs/${report.runId}`);
  const run = await runResponse.json();
  assert.equal(run.status, 'completed');
  await page.goto(base + "/#workspace", { waitUntil: 'networkidle' });
  await page.getByRole('button', { name: '历史', exact: true }).click();
  await page.locator('.run-row').filter({ hasText: run.name }).click();
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
  const beforeCount = await page.evaluate(() => window.__drawCalls);
  await page.waitForTimeout(1000);
  report.idleDrawCalls = (await page.evaluate(() => window.__drawCalls)) - beforeCount;
  assert.equal(report.idleDrawCalls, 0);
  const canvas = page.getByTestId('scene-canvas');
  const first = createHash('sha256')
    .update(await canvas.screenshot())
    .digest('hex');
  const box = await canvas.boundingBox();
  await page.mouse.move(box.x + box.width * 0.6, box.y + box.height * 0.6);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * 0.6 + 95, box.y + box.height * 0.6 + 35, { steps: 8 });
  await page.mouse.up();
  await page.waitForTimeout(1000);
  const second = createHash('sha256')
    .update(await canvas.screenshot())
    .digest('hex');
  assert.notEqual(first, second);
  report.orbitStillRenders = true;
  assert.equal(report.historyPolls || 0, 0);
  assert(report.requests['/api/runs/:run/scene'] <= run.snapshots.length + 6);
  assert(report.requests['/api/runs/:run/reports'] < 20);
  assert.deepEqual(errors, []);
  await page.screenshot({ path: path.join(directory, 'completed.png'), fullPage: true });
  delete report.error;
  report.passed = true;
  report.graphicalChecks =
    'Read-only reinspection after waiting for the final camera count and scene loading to settle';
  await writeFile(path.join(directory, 'report.json'), JSON.stringify(report, null, 2));
  console.log(
    JSON.stringify({
      passed: true,
      idleDrawCalls: report.idleDrawCalls,
      runtimeDownloadSeconds: report.runtimeDownloadSeconds,
    }),
  );
} finally {
  await browser.close();
}
