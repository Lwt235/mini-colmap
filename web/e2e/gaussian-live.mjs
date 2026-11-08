/** Real trained Gaussian rendering and OrbitControls; never starts training. */
import { chromium } from 'playwright';
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const root = fileURLToPath(new URL('../../', import.meta.url));
const base = 'http://127.0.0.1:8765';
const runId = process.env.GSPLAT_RUN_ID || '695361f9be8042ed';
const output =
  process.env.MINICOLMAP_E2E_REPORT || path.join(root, 'reports/2026-10-18-3dgs-resolution-live');
await mkdir(output, { recursive: true });
const run = await (await fetch(`${base}/api/runs/${runId}`)).json();
assert.equal(run.pipeline, '3dgs');
assert.equal(run.status, 'completed');
const browser = await chromium.launch({ channel: 'msedge', headless: true });
const page = await browser.newPage({ viewport: { width: 1680, height: 1100 } });
const errors = [];
const report = { run_id: runId, checks: [] };
page.on('pageerror', (error) => errors.push(error.message));

const frameCount = () =>
  page.getByTestId('gaussian-live-metrics').getAttribute('data-frame-count').then(Number);
const waitForFrame = (previous = 0) =>
  page.waitForFunction(
    (value) =>
      Number(
        document
          .querySelector('[data-testid="gaussian-live-metrics"]')
          ?.getAttribute('data-frame-count'),
      ) > value,
    previous,
    { timeout: 75000 },
  );
const imageHash = () =>
  page.getByTestId('gaussian-live-image').evaluate(async (image) => {
    // The display can advance while decode() is pending; capture its already
    // decoded blob instead of waiting on an element whose src changes live.
    const bytes = await (await fetch(image.src)).arrayBuffer();
    const digest = await crypto.subtle.digest('SHA-256', bytes);
    return [...new Uint8Array(digest)].map((value) => value.toString(16).padStart(2, '0')).join('');
  });
const exercise = async (name, action) => {
  const before = await frameCount();
  const hash = await imageHash();
  await action();
  await waitForFrame(before);
  assert.notEqual(await imageHash(), hash, name + ' must change the actual rendered image');
  report.checks.push(name);
};

try {
  await page.addInitScript((id) => localStorage.setItem('minicolmap.lastRun', id), runId);
  await page.goto(base + "/#workspace", { waitUntil: 'domcontentloaded' });
  await page.getByTestId('gaussian-live').waitFor();
  await page.getByTestId('gaussian-live-toggle').click({ timeout: 90000 });
  await waitForFrame();
  assert.match(await page.getByTestId('gaussian-live-status').innerText(), /gsplat.*cuda/);
  report.checks.push('selected real Gaussian checkpoint loads once on CUDA');
  const canvas = page.getByTestId('gaussian-live-canvas');
  await canvas.scrollIntoViewIfNeeded();
  let box = await canvas.boundingBox();
  assert(box);
  const center = () => ({ x: box.x + box.width / 2, y: box.y + box.height / 2 });
  const drag = async (button, dx, dy, steps = 15) => {
    const { x, y } = center();
    await page.mouse.move(x, y);
    await page.mouse.down({ button });
    for (let step = 1; step <= steps; step++) {
      await page.mouse.move(x + (dx * step) / steps, y + (dy * step) / steps);
      await page.waitForTimeout(25);
    }
    await page.mouse.up({ button });
  };
  await exercise('left drag rotates and produces new RGB frames', () => drag('left', 110, 30));
  await exercise('right drag pans and produces new RGB frames', () => drag('right', 35, 12));
  await exercise('mouse wheel zooms the actual Gaussian render', async () => {
    const { x, y } = center();
    await page.mouse.move(x, y);
    await page.mouse.wheel(0, -160);
  });
  const countBeforeContinuous = await frameCount();
  await drag('left', -95, -20, 45);
  const countAfterContinuous = await frameCount();
  assert(
    countAfterContinuous - countBeforeContinuous >= 3,
    'continuous input must not starve displayed frames',
  );
  report.checks.push('frames continue displaying during continuous drag');
  await exercise('saved camera alignment uses its true pose', async () => {
    await page.getByTestId('gaussian-live-camera').selectOption('16');
    await page.getByTestId('gaussian-live-align').click();
  });
  await exercise('scene fit requests a free camera', () =>
    page.getByTestId('gaussian-live-fit').click(),
  );
  let previous = await frameCount();
  await page.getByTestId('gaussian-live-resolution').selectOption('1280');
  await waitForFrame(previous);
  previous = await frameCount();
  await page.setViewportSize({ width: 1500, height: 1000 });
  await waitForFrame(previous);
  report.checks.push('output resolution and window resize update K and frame dimensions');
  await page.getByTestId('gaussian-live-canvas').scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(output, 'live-workbench.png'), fullPage: true });
  report.metrics = await page
    .getByTestId('gaussian-live-metrics')
    .evaluate((node) => ({ ...node.dataset }));
  report.dimensions = await page
    .getByTestId('gaussian-live-image')
    .evaluate((img) => [img.naturalWidth, img.naturalHeight]);
  await page.getByTestId('gaussian-live-reconnect').click();
  await waitForFrame();
  report.checks.push('manual reconnect reloads GPU session and returns fresh frames');
  await page.getByTestId('gaussian-live-toggle').click();
  await page.getByTestId('gaussian-live-canvas').waitFor({ state: 'detached' });
  await page.waitForTimeout(500);
  await page.getByTestId('gaussian-live-toggle').click();
  await waitForFrame();
  report.checks.push('close releases session and permits reopening');
  await page.getByTestId('gaussian-live-toggle').click();
  assert.deepEqual(errors, []);
  report.status = 'passed';
} catch (error) {
  report.status = 'failed';
  report.error = String(error);
  report.body = await page.locator('body').innerText();
  await page.screenshot({ path: path.join(output, 'live-browser-failure.png'), fullPage: true });
  process.exitCode = 1;
} finally {
  report.page_errors = errors;
  await writeFile(
    path.join(output, 'live-browser-verification.json'),
    JSON.stringify(report, null, 2),
  );
  console.log(JSON.stringify(report, null, 2));
  await browser.close();
}
