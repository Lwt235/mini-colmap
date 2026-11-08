/** Real saved 3DGS camera -> CPU reference PNG. No training or fake renderer. */
import { chromium } from 'playwright';
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const root = fileURLToPath(new URL('../../', import.meta.url));
const base = 'http://127.0.0.1:8765';
const runId = process.env.GSPLAT_RUN_ID || '6fac7b8d077e421e';
const output = path.join(root, '.cache/gaussian-camera-preview');
await mkdir(output, { recursive: true });
const browser = await chromium.launch({
  channel: 'msedge',
  headless: true,
  args: ['--use-angle=swiftshader'],
});
const page = await browser.newPage({ viewport: { width: 1600, height: 1100 } });
const errors = [];
const report = { run_id: runId };
page.on('pageerror', (error) => errors.push(error.message));
try {
  await page.addInitScript((id) => localStorage.setItem('minicolmap.lastRun', id), runId);
  await page.goto(base + "/#workspace", { waitUntil: 'domcontentloaded' });
  await page.getByTestId('rendering-workspace').waitFor();
  await page.getByLabel('待渲染相机', { exact: true }).selectOption('16');
  const response = page.waitForResponse(
    (item) =>
      item.url().endsWith(`/api/rendering/runs/${runId}/render`) &&
      item.request().method() === 'POST',
    { timeout: 180000 },
  );
  const started = Date.now();
  await page.getByRole('button', { name: '渲染此相机', exact: true }).click();
  const rendered = await response;
  assert.equal(rendered.status(), 200, await rendered.text());
  const preview = await rendered.json();
  await page.waitForFunction(
    (url) => document.querySelector('img[alt="实际渲染输出"]')?.getAttribute('src') === url,
    preview.url,
  );
  const image = page.getByAltText('实际渲染输出');
  await image.evaluate((element) => element.decode());
  const dimensions = await image.evaluate((element) => [
    element.naturalWidth,
    element.naturalHeight,
  ]);
  assert.deepEqual(dimensions, [160, 120]);
  assert.equal(preview.image_id, 16);
  await image.scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(output, 'original-camera.png'), fullPage: true });
  report.seconds = (Date.now() - started) / 1000;
  report.preview = preview;
  report.dimensions = dimensions;
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.getByTestId('rendering-workspace').waitFor({ timeout: 90000 });
  await page.getByAltText('实际渲染输出').waitFor({ timeout: 90000 });
  assert.equal(await page.getByAltText('实际渲染输出').getAttribute('src'), preview.url);
  report.originalCameraAndHistoryRestore = true;
  assert.deepEqual(errors, []);
  report.status = 'passed';
} catch (error) {
  report.status = 'failed';
  report.error = String(error);
  report.body = await page.locator('body').innerText();
  await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true });
  process.exitCode = 1;
} finally {
  report.pageErrors = errors;
  await writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
  await browser.close();
}
