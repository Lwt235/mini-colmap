/** Read the real completed recovery run and render a held-out camera; no fixture. */
import { chromium } from 'playwright';
import assert from 'node:assert/strict';
import { readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const root = fileURLToPath(new URL('../../', import.meta.url));
const report = path.join(root, 'reports/2026-10-17-nerf-quality-recovery');
const run = JSON.parse(await readFile(path.join(report, 'final-run.json'), 'utf8'));
const base = 'http://127.0.0.1:8765';
const status = await (await fetch(`${base}/api/runs/${run.id}`)).json();
assert.equal(status.status, 'completed');
const browser = await chromium.launch({
  channel: 'msedge',
  headless: true,
  args: ['--use-angle=swiftshader'],
});
const page = await browser.newPage({ viewport: { width: 1680, height: 1100 } });
const errors = [];
page.on('pageerror', (e) => errors.push(e.message));
try {
  await page.addInitScript((id) => localStorage.setItem('minicolmap.lastRun', id), run.id);
  await page.goto(base + "/#workspace", { waitUntil: 'domcontentloaded' });
  // Restoring a NeRF history entry opens its rendering workspace automatically.
  await page.getByTestId('rendering-workspace').waitFor();
  await page.getByAltText('实际渲染输出').waitFor();
  assert.match(await page.locator('.rendering-result-heading').innerText(), new RegExp(run.name));
  assert.equal(await page.getByLabel('图像背景', { exact: true }).inputValue(), 'black');
  assert.equal(await page.getByLabel('训练深度采样', { exact: true }).inputValue(), 'random');
  assert.equal(await page.getByLabel('光线批次', { exact: true }).inputValue(), 'all_images');
  assert.equal(await page.getByLabel('图像降采样', { exact: true }).inputValue(), '4');
  await page.getByLabel('待渲染相机', { exact: true }).selectOption('16');
  const response = page.waitForResponse(
    (r) =>
      r.url().endsWith(`/api/rendering/runs/${run.id}/render`) && r.request().method() === 'POST',
    { timeout: 180000 },
  );
  const started = Date.now();
  await page.getByRole('button', { name: '渲染此相机', exact: true }).click();
  const rendered = await response;
  assert.equal(rendered.status(), 200);
  const preview = await rendered.json();
  await page.waitForFunction(
    (url) => document.querySelector('img[alt="实际渲染输出"]')?.getAttribute('src') === url,
    preview.url,
  );
  await page.getByAltText('实际渲染输出').evaluate((img) => img.decode());
  const dimensions = await page
    .getByAltText('实际渲染输出')
    .evaluate((img) => [img.naturalWidth, img.naturalHeight]);
  assert.deepEqual(dimensions, [160, 120]);
  await page.screenshot({ path: path.join(report, 'workbench.png'), fullPage: true });
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.getByAltText('实际渲染输出').waitFor();
  assert.equal(await page.getByAltText('实际渲染输出').getAttribute('src'), preview.url);
  assert.deepEqual(errors, []);
  await writeFile(
    path.join(report, 'browser-live.json'),
    JSON.stringify(
      {
        run_id: run.id,
        preview,
        dimensions,
      render_and_refresh_seconds: (Date.now() - started) / 1000,
        errors,
        checks: [
          'real trained checkpoint',
          'saved background/sampling/ray-batch configuration',
          'held-out camera #16 rendering',
          'actual 160x120 image',
          'preview restored on refresh',
        ],
      },
      null,
      2,
    ),
  );
  console.log('Real NeRF result, camera render, and history refresh passed.');
} catch (error) {
  await page.screenshot({ path: path.join(report, 'browser-debug.png'), fullPage: true });
  await writeFile(
    path.join(report, 'browser-debug.json'),
    JSON.stringify(
      { error: String(error), errors, body: await page.locator('body').innerText() },
      null,
      2,
    ),
  );
  throw error;
} finally {
  await browser.close();
}
