/** Read-only alignment acceptance; never starts or cancels a reconstruction. */
import { chromium } from 'playwright';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdir, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
const root = fileURLToPath(new URL('../../', import.meta.url));
const base = (process.env.WORKBENCH_URL || 'http://127.0.0.1:8765').replace(/\/$/, '');
assert(['127.0.0.1', 'localhost'].includes(new URL(base).hostname));
const artifacts = path.join(
  root,
  '.cache/camera-align-e2e',
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
async function get(url) {
  const response = await page.request.get(base + url);
  assert(response.ok());
  return response.json();
}
const canvas = page.getByTestId('scene-canvas');
const waitAligned = () =>
  page.waitForFunction(
    () =>
      !document.querySelector('[data-testid="scene-canvas"]')?.hasAttribute('data-camera-aligning'),
  );
async function movingFrame() {
  return canvas.evaluate((element) => {
    const gl = element.getContext('webgl2');
    const pixels = new Uint8Array(element.width * element.height * 4);
    gl.readPixels(0, 0, element.width, element.height, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
    let signature = 2166136261;
    for (const value of pixels) signature = Math.imul(signature ^ value, 16777619);
    return { signature, moving: element.hasAttribute('data-camera-aligning') };
  });
}
const hash = async () =>
  createHash('sha256')
    .update(await canvas.screenshot())
    .digest('hex');
async function alignedPixels(save = false) {
  return canvas.evaluate((element, save) => {
    const gl = element.getContext('webgl2');
    const pixels = new Uint8Array(element.width * element.height * 4);
    gl.readPixels(0, 0, element.width, element.height, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
    if (save) {
      window.__cameraAlignmentPixels = pixels;
      return null;
    }
    const baseline = window.__cameraAlignmentPixels;
    let changed = 0,
      maxDelta = 0;
    for (let i = 0; i < pixels.length; i += 4) {
      let delta = 0;
      for (let c = 0; c < 4; c++)
        delta = Math.max(delta, Math.abs(pixels[i + c] - baseline[i + c]));
      if (delta) changed++;
      maxDelta = Math.max(maxDelta, delta);
    }
    return { changed, maxDelta };
  }, save);
}
async function assertAlignedPixels(message) {
  const result = await alignedPixels();
  // Idle OrbitControls rounding can change one antialiased pixel by one level.
  assert(result.changed <= 8 && result.maxDelta <= 2, `${message}: ${JSON.stringify(result)}`);
}
async function drag(button, dx, dy) {
  const box = await canvas.boundingBox();
  await page.mouse.move(box.x + box.width * 0.5, box.y + box.height * 0.5);
  await page.mouse.down({ button });
  await page.mouse.move(box.x + box.width * dx, box.y + box.height * dy, { steps: 15 });
  await page.mouse.up({ button });
  await page.waitForTimeout(700);
}
try {
  await page.goto(base + "/#workspace", { waitUntil: 'networkidle' });
  const { runs } = await get('/api/bootstrap');
  const baseline = runs.find((r) => r.name === 'temple_ring_8_arc_no_ba_01');
  assert(baseline);
  await page.getByRole('button', { name: '历史', exact: true }).click();
  await page.locator('.run-row').filter({ hasText: baseline.name }).click();
  await page.waitForFunction(() =>
    document.querySelector('.scene-stats')?.textContent.includes('1,283'),
  );
  await page.getByRole('button', { name: '照片', exact: true }).click();
  await page.getByTestId('image-20').click();
  const align = page.getByRole('button', { name: '对齐至此相机', exact: true });
  await align.waitFor();
  await page.waitForTimeout(400);
  const before = await hash();
  await align.click();
  await page.waitForTimeout(100);
  const intermediate1 = await movingFrame();
  await page.waitForTimeout(120);
  const intermediate2 = await movingFrame();
  assert(
    intermediate1.moving && intermediate2.moving,
    'Alignment must pass through intermediate frames',
  );
  assert.notEqual(intermediate1.signature, intermediate2.signature);
  await canvas.screenshot({ path: path.join(artifacts, 'alignment-in-progress.png') });
  await waitAligned();
  const finalFrame = await movingFrame();
  assert.notEqual(intermediate2.signature, finalFrame.signature);
  report.smoothTransition = true;
  const aligned = await hash();
  await alignedPixels(true);
  await canvas.screenshot({ path: path.join(artifacts, 'aligned-initial.png') });
  assert.notEqual(aligned, before);
  assert.equal(await page.locator('.camera-image-frame, .camera-view-banner').count(), 0);
  await page.waitForTimeout(700);
  await canvas.screenshot({ path: path.join(artifacts, 'aligned-idle.png') });
  await assertAlignedPixels('Aligned pose remains stable without input');
  const idleDraws = await page.evaluate(() => window.__drawCalls);
  await page.waitForTimeout(700);
  report.idleDrawCalls = (await page.evaluate(() => window.__drawCalls)) - idleDraws;
  assert.equal(report.idleDrawCalls, 0, 'Completed alignment must stop drawing');
  report.alignsWithoutLock = true;
  let previous = await hash();
  // The aligned camera has a narrow calibrated FOV. Large orbit steps move
  // the entire model off-screen, making later blank-frame hashes inconclusive.
  await drag('left', 0.51, 0.505);
  assert.notEqual(await hash(), previous);
  report.rotation = true;
  await align.click();
  await waitAligned();
  previous = await hash();
  await drag('right', 0.54, 0.53);
  assert.notEqual(await hash(), previous);
  report.pan = true;
  previous = await hash();
  await page.mouse.wheel(0, -300);
  await page.waitForTimeout(700);
  assert.notEqual(await hash(), previous);
  report.zoom = true;
  await align.click();
  await waitAligned();
  await assertAlignedPixels('Another click realigns after free movement');
  report.realignsOnClick = true;
  // Interrupt well before the destination, using the very first pointer event.
  await page.getByLabel('重置视角', { exact: true }).click();
  await page.waitForTimeout(300);
  await align.click();
  await page.waitForTimeout(150);
  assert.equal(await canvas.getAttribute('data-camera-aligning'), 'true');
  await drag('left', 0.51, 0.505);
  assert.equal(await canvas.getAttribute('data-camera-aligning'), null);
  await page.waitForTimeout(500);
  const interrupted = await hash();
  await page.waitForTimeout(900);
  assert.equal(await hash(), interrupted, 'Interrupted motion must not resume or snap back');
  assert((await alignedPixels()).changed > 8, 'Interruption must retain a free pose');
  report.dragInterruptsTransition = true;
  await align.click();
  await page.waitForTimeout(150);
  const wheelBox = await canvas.boundingBox();
  await page.mouse.move(wheelBox.x + wheelBox.width / 2, wheelBox.y + wheelBox.height / 2);
  await page.mouse.wheel(0, -100);
  assert.equal(await canvas.getAttribute('data-camera-aligning'), null);
  await page.waitForTimeout(1000);
  assert((await alignedPixels()).changed > 8);
  report.wheelInterruptsTransition = true;
  await align.click();
  await page.waitForTimeout(120);
  await page.getByTestId('image-13').click();
  await align.click();
  await page.waitForTimeout(120);
  await page.getByTestId('image-20').click();
  await align.click();
  await waitAligned();
  await assertAlignedPixels('The latest camera wins when alignments overlap');
  report.retargetsWhileMoving = true;
  await page.setViewportSize({ width: 1360, height: 900 });
  await page.waitForTimeout(500);
  const scene = await get(`/api/runs/${baseline.id}/scene`),
    camera = scene.cameras.find((c) => c.image_id === 20);
  const box = await canvas.boundingBox();
  // Independent world-to-camera projection in the full-panel free perspective.
  const focal = (box.height * camera.K[1][1]) / (camera.height * camera.K[2][2]);
  let picked = false;
  for (let i = 0; i < scene.points.ids.length; i += 17) {
    const xyz = scene.points.positions.slice(i * 3, i * 3 + 3);
    const p = camera.R.map((row, j) =>
      row.reduce((sum, value, k) => sum + value * xyz[k], camera.t[j]),
    );
    if (p[2] <= 0) continue;
    const x = box.width / 2 + (focal * p[0]) / p[2],
      y = box.height / 2 + (focal * p[1]) / p[2];
    if (x < 50 || y < 90 || x > box.width - 50 || y > box.height - 100) continue;
    const detail = await get(`/api/runs/${baseline.id}/points/${scene.points.ids[i]}`);
    if (!detail.observations.some((o) => o.image_id === 20)) continue;
    const pointLoaded = page.waitForResponse(
      (response) =>
        response.url().includes(`/api/runs/${baseline.id}/points/`) &&
        response.url().includes('?snapshot='),
      { timeout: 10000 },
    );
    await page.mouse.click(box.x + x, box.y + y);
    const loaded = await pointLoaded.catch(() => null);
    if (!loaded) continue;
    const selected = page.locator('.observation-list .selected');
    await selected.waitFor({ state: 'visible', timeout: 10000 });
    if ((await selected.count()) && (await selected.innerText()).includes(camera.name)) {
      picked = true;
      break;
    }
  }
  assert(
    picked,
    'Picking still matches the aligned pose after resize and selects the reference photo',
  );
  report.responsivePoseAndPointPicking = true;
  await page.getByRole('button', { name: '对齐至此观测相机', exact: true }).click();
  await waitAligned();
  previous = await hash();
  await drag('left', 0.59, 0.56);
  assert.notEqual(await hash(), previous);
  report.observationEntryAlsoFree = true;
  await page.getByTestId('image-13').click();
  await align.click();
  await waitAligned();
  await page.screenshot({ path: path.join(artifacts, 'aligned-free-view.png'), fullPage: true });
  previous = await hash();
  await page.getByLabel('重置视角', { exact: true }).click();
  await page.waitForTimeout(500);
  assert.notEqual(await hash(), previous);
  previous = await hash();
  await drag('left', 0.6, 0.55);
  assert.notEqual(await hash(), previous);
  report.switchCameraResetAndOrbit = true;
  await align.click();
  await page.waitForTimeout(150);
  await page.getByLabel('重置视角', { exact: true }).click();
  assert.equal(await canvas.getAttribute('data-camera-aligning'), null);
  await page.waitForTimeout(1000);
  const reset = await hash();
  await page.waitForTimeout(800);
  assert.equal(await hash(), reset, 'Reset must cancel the unfinished transition');
  report.resetInterruptsTransition = true;
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
  console.log('Alignment artifacts:', artifacts);
  await browser.close();
}
