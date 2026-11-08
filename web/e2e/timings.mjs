/** Read-only check against an existing timing run; see docs/evaluation.md. */
import { chromium } from 'playwright';
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
const root = fileURLToPath(new URL('../../', import.meta.url));
const base = process.env.WORKBENCH_URL || 'http://127.0.0.1:8765';
assert(['127.0.0.1', 'localhost'].includes(new URL(base).hostname));
const artifacts = path.join(
  root,
  '.cache/timing-e2e',
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
page.on('pageerror', (error) => errors.push(String(error)));
async function get(url) {
  const response = await page.request.get(base + url);
  assert(response.ok());
  return response.json();
}
async function select(name) {
  await page.getByRole('button', { name: '历史', exact: true }).click();
  await page.locator('.run-row').filter({ hasText: name }).click();
  await page.getByRole('button', { name: '质量与诊断', exact: true }).click();
}
try {
  await page.goto(base + "/#workspace", { waitUntil: 'networkidle' });
  const { runs } = await get('/api/bootstrap');
  const run = runs.find(
    (r) => r.name === (process.env.TIMING_RUN_NAME || 'timing_baseline_20261016_01'),
  );
  assert(run, 'Set TIMING_RUN_NAME to an existing completed SfM run; see docs/evaluation.md');
  const { timings, summary } = await get(`/api/runs/${run.id}/reports`);
  assert.equal(timings.status, 'completed');
  assert.equal(timings.total_seconds, summary.elapsed_seconds);
  assert(
    Math.abs(
      Object.values(timings.stages_seconds).reduce((a, b) => a + b, 0) - timings.total_seconds,
    ) < 1e-6,
  );
  await select(run.name);
  const panel = page.getByTestId('pipeline-timings');
  await panel.waitFor();
  assert.equal(await panel.locator('tbody tr').count(), Object.keys(timings.stages_seconds).length);
  assert((await panel.innerText()).includes('F-RANSAC'));
  assert((await panel.innerText()).includes('描述子匹配'));
  assert.deepEqual(await get(`/api/runs/${run.id}/artifacts/timings.json`), timings);
  await page.getByLabel('调整日志区域', { exact: true }).click();
  await page.waitForTimeout(700);
  await panel.scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(artifacts, 'timings-panel.png'), fullPage: true });
  report.timings = timings;
  const old = runs.find((r) => r.name === 'temple_ring_8_arc_no_ba_01');
  assert(old);
  await select(old.name);
  await panel.waitFor({ state: 'hidden' });
  report.oldRunsHaveNoInventedTiming = true;
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
  await writeFile(path.join(artifacts, 'report.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
  console.log('Timing artifacts:', artifacts);
  await browser.close();
}
