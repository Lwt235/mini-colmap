/** Current product acceptance. Creates and deletes only its own isolated tasks. */
import { chromium } from 'playwright';
import assert from 'node:assert/strict';
import { mkdir, readFile, writeFile, access } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const root = fileURLToPath(new URL('../../', import.meta.url));
const out = path.join(root, 'reports/workbench-cleanup-20261022');
const base = process.env.WORKBENCH_URL || 'http://127.0.0.1:8765';
await mkdir(out, { recursive: true });
const pipeline = JSON.parse(
  await readFile(path.join(root, 'reports/new-scenes-20261022/real-pipeline.json'), 'utf8'),
);
const result = { status: 'running' };
const owned = [];
const errors = [];
let browser, page;
async function request(route, method = 'get', data) {
  const r = await page.request.fetch(base + route, { method, data });
  assert(r.ok(), `${r.status()} ${method} ${route}: ${await r.text()}`);
  return r.json();
}
async function missing(p) {
  try {
    await access(p);
    return false;
  } catch {
    return true;
  }
}
const hash = async (p) =>
  createHash('sha256')
    .update(await readFile(p))
    .digest('hex');
async function waitIdle() {
  for (let n = 0; n < 60; n++) {
    const gpu = await request('/api/gpu');
    if (!gpu.busy) return;
    await new Promise((r) => setTimeout(r, 1000));
  }
  throw Error('GPU did not become idle');
}
async function remove(id) {
  const p = await request(`/api/runs/${id}/deletion`);
  assert.deepEqual(p.blockers, []);
  return request(`/api/runs/${id}`, 'DELETE', { token: p.token });
}
try {
  browser = await chromium.launch({
    executablePath: path.join(
      root,
      '.cache/demo-browser/chrome-headless-shell-linux64/chrome-headless-shell',
    ),
    headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--use-angle=swiftshader'],
  });
  page = await browser.newPage({ viewport: { width: 1600, height: 1080 } });
  page.setDefaultTimeout(45000);
  page.on('pageerror', (e) => errors.push(String(e)));
  await page.goto(base + '/#workspace');
  const gpu = await request('/api/gpu');
  assert.equal(gpu.selected, 7);
  assert(!gpu.busy, 'Close active work before acceptance');
  assert.equal(gpu.devices.length, 8);
  await page.getByLabel('选择物理显卡', { exact: true }).waitFor();
  assert.equal(await page.getByLabel('选择物理显卡').inputValue(), '7');
  assert.equal((await request('/api/gpu', 'POST', { index: 7 })).selected, 7);
  await page.getByLabel('选择输入数据集', { exact: true }).waitFor();
  assert(!/尚未补完|待实现|教学实现/.test(await page.locator('body').innerText()));
  const source = await request(`/api/runs/${pipeline.sfm}`);
  const model = await readFile(path.join(source.output_path, 'model.json'));
  const originalHash = await hash(path.join(source.output_path, 'model.json'));
  const r = await page.request.post(base + '/api/results/import', {
    multipart: { file: { name: '任务删除验收.json', mimeType: 'application/json', buffer: model } },
  });
  assert(r.ok(), await r.text());
  const sfm = await r.json();
  owned.push(sfm.id);
  const cfg = JSON.parse(await readFile(path.join(root, 'configs/3dgs_linux_smoke.json'), 'utf8'));
  Object.assign(cfg, { iterations: 40, image_downscale: 8, preview_every: 10 });
  Object.assign(cfg.gaussian, {
    max_gaussians: 2000,
    density_strategy: 'adaptive',
    densify_start: 100,
    densify_stop: 200,
  });
  const rgb = await request('/api/rendering/runs', 'POST', {
    source_run_id: sfm.id,
    photo_dataset_id: pipeline.dataset,
    config: cfg,
  });
  owned.push(rgb.id);
  assert.equal(rgb.gpu_index, 7);
  assert.equal((await page.request.post(base + '/api/gpu', { data: { index: 7 } })).status(), 409);
  const activePlan = await request(`/api/runs/${rgb.id}/deletion`);
  assert(activePlan.blockers.length);
  let done;
  for (let n = 0; n < 180; n++) {
    done = await request(`/api/runs/${rgb.id}`);
    if (done.status !== 'running') break;
    await new Promise((r) => setTimeout(r, 1000));
  }
  assert.equal(done.status, 'completed', JSON.stringify(done));
  const runtime = JSON.parse(await readFile(path.join(done.output_path, 'runtime.json'), 'utf8'));
  assert.equal(runtime.cuda_visible_devices, '7');
  const scene = await request('/api/scenes', 'POST', { rgb_run_id: rgb.id, name: '任务清理验收' });
  const sceneFolder = path.join(root, 'data/semantic_scenes', scene.id);
  const configFile = path.join(root, 'configs', scene.config + '.json');
  const parentPlan = await request(`/api/runs/${sfm.id}/deletion`);
  assert(parentPlan.blockers.some((v) => v.includes('依赖')));
  await page.reload();
  await page.getByRole('button', { name: '历史', exact: true }).click();
  await page.getByRole('button', { name: `删除任务 ${rgb.name}`, exact: true }).click();
  const modal = page.getByRole('dialog', { name: '删除历史任务' });
  await modal.waitFor();
  assert((await modal.innerText()).includes(scene.id));
  assert((await modal.innerText()).includes('原始照片与共享数据集保留'));
  await page.screenshot({ path: path.join(out, 'delete-preview.png'), fullPage: true });
  const deletionResponse = page.waitForResponse(
    (r) => r.url().endsWith(`/api/runs/${rgb.id}`) && r.request().method() === 'DELETE',
  );
  await modal.getByTestId('confirm-delete-run').click();
  assert((await deletionResponse).ok());
  await modal.waitFor({ state: 'hidden' });
  for (const p of [
    done.output_path,
    sceneFolder,
    configFile,
    path.join(root, '.cache/mini-colmap-ui/runs', rgb.id + '.json'),
    path.join(root, '.cache/mini-colmap-ui/logs', rgb.id + '.log'),
  ])
    assert(await missing(p), p + ' should be removed');
  assert.equal(await hash(path.join(source.output_path, 'model.json')), originalHash);
  assert(!(await request('/api/runs?discover=true')).some((v) => v.id === rgb.id));
  assert(!(await request('/api/scenes')).scenes.some((v) => v.id === scene.id));
  await page.getByRole('button', { name: `删除任务 ${sfm.name}`, exact: true }).click();
  await modal.waitFor();
  await modal.getByTestId('confirm-delete-run').click();
  await modal.waitFor({ state: 'hidden' });
  assert(await missing(sfm.output_path));
  await page.screenshot({ path: path.join(out, 'history-cleaned.png'), fullPage: true });
  await page.getByLabel('当前场景', { exact: true }).selectOption('kitchen');
  await page.getByTestId('demo-example-sink').click();
  await page.waitForFunction(
    () =>
      Number(document.querySelector('[data-testid=gaussian-live-metrics]')?.dataset.lastSeq) > 0,
    null,
    { timeout: 90000 },
  );
  await page.waitForFunction(
    () => document.querySelector('[aria-label="选择物理显卡"]')?.disabled,
    null,
    { timeout: 15000 },
  );
  assert((await request('/api/gpu')).busy);
  await page.screenshot({ path: path.join(out, 'gpu-rendering-locked.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: path.join(out, 'mobile.png'), fullPage: true });
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 2));
  assert.deepEqual(errors, []);
  Object.assign(result, {
    status: 'passed',
    gpu_devices: gpu.devices.length,
    selected_physical_gpu: 7,
    training_runtime_gpu: runtime.cuda_visible_devices,
    live_gpu_switch_locked: true,
    active_deletion_blocked: true,
    parent_dependency_blocked: true,
    own_history_and_files_deleted: true,
    semantic_scene_cascade: true,
    original_sfm_preserved: true,
    history_not_rediscovered: true,
    mobile_no_overflow: true,
    page_errors: errors,
  });
} catch (e) {
  Object.assign(result, { status: 'failed', error: String(e), page_errors: errors });
  process.exitCode = 1;
  if (page)
    await page.screenshot({ path: path.join(out, 'failure.png'), fullPage: true }).catch(() => {});
} finally {
  if (page) await page.goto('about:blank').catch(() => {});
  for (const id of owned.toReversed()) {
    try {
      const r = await request(`/api/runs/${id}`);
      if (r.status === 'running') await request(`/api/runs/${id}/cancel`, 'POST');
      await waitIdle();
      await remove(id);
    } catch (e) {
      if (!String(e).includes('404')) errors.push('cleanup ' + id + ': ' + e);
    }
  }
  await browser?.close();
  await writeFile(path.join(out, 'browser-check.json'), JSON.stringify(result, null, 2) + '\n');
  console.log(JSON.stringify(result, null, 2));
}
