/** Restore editable SfM/3DGS controls and verify real live training previews. */
import { chromium } from 'playwright';
import assert from 'node:assert/strict';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const root = fileURLToPath(new URL('../../', import.meta.url));
const report = path.join(root, 'reports/training-workspace-20261022');
await mkdir(report, { recursive: true });
const fixture = JSON.parse(
  await readFile(path.join(root, 'reports/new-scenes-20261022/real-pipeline.json'), 'utf8'),
);
const result = { status: 'running' };
let browser, page;
const errors = [];
const owned = [];
async function get(route) {
  const r = await page.request.get('http://127.0.0.1:8765' + route);
  assert(r.ok(), await r.text());
  return r.json();
}
async function terminal(id) {
  for (let n = 0; n < 300; n++) {
    const v = await get('/api/runs/' + id);
    if (v.status !== 'running') {
      assert.equal(v.status, 'completed', JSON.stringify(v));
      return v;
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
  throw Error('training timeout');
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
  page = await browser.newPage({ viewport: { width: 1680, height: 1100 } });
  page.setDefaultTimeout(60000);
  page.on('pageerror', (e) => errors.push(String(e)));
  await page.addInitScript(() => {
    window.workspaceEvents = [];
    window.previewUpdates = [];
    window.metricUpdates = [];
    const Native = window.EventSource;
    window.EventSource = class extends Native {
      constructor(...a) {
        super(...a);
        this.addEventListener('message', (e) => {
          try {
            window.workspaceEvents.push(JSON.parse(e.data));
          } catch {}
        });
      }
    };
    window.addEventListener('DOMContentLoaded', () => {
      new MutationObserver(() => {
        const image = document.querySelector('img[alt="实际渲染输出"]');
        const src = image?.getAttribute('src');
        if (src && !window.previewUpdates.includes(src)) window.previewUpdates.push(src);
        const metrics = document.querySelector('.rendering-metrics')?.textContent;
        if (metrics && window.metricUpdates.at(-1) !== metrics) window.metricUpdates.push(metrics);
      }).observe(document.body, {
        subtree: true,
        childList: true,
        characterData: true,
        attributes: true,
        attributeFilter: ['src'],
      });
    });
  });
  await page.goto('http://127.0.0.1:8765/#workspace');
  assert.equal((await get('/api/gpu')).selected, 7, 'GPU smoke checks use physical GPU 7');
  await page.getByLabel('选择输入数据集', { exact: true }).selectOption(fixture.dataset);
  await page.getByRole('button', { name: '参数', exact: true }).click();
  await page.getByLabel('最大特征数', { exact: true }).fill('2500');
  await page.getByLabel('匹配距离比', { exact: true }).fill('0.75');
  await page.getByRole('checkbox', { name: '启用最终 BA', exact: true }).uncheck();
  await page.screenshot({ path: path.join(report, 'sfm-parameters.png'), fullPage: true });
  let response = page.waitForResponse(
    (r) => r.url().endsWith('/api/runs') && r.request().method() === 'POST',
  );
  await page.getByTestId('start-run').click();
  let received = await response;
  assert(received.ok(), await received.text());
  const sfm = await received.json();
  owned.push(sfm.id);
  const sfmRequest = received.request().postDataJSON();
  assert.equal(sfmRequest.config.features.max_features, 2500);
  assert.equal(sfmRequest.config.features.ratio, 0.75);
  await page.getByTestId('actual-progress').waitFor();
  await page.waitForFunction(
    () => window.workspaceEvents.some((e) => e.kind === 'snapshot'),
    null,
    { timeout: 90000 },
  );
  const completeSfm = await terminal(sfm.id);
  assert(completeSfm.registered_images >= 2);
  await page.getByTestId('open-rendering').click();
  await page.getByLabel('渲染训练方法', { exact: true }).waitFor();
  await page
    .locator('.rendering-config-actions input[type=file]')
    .setInputFiles(path.join(root, 'configs/3dgs_linux_smoke.json'));
  await page.waitForFunction(
    () => document.querySelector('[aria-label="渲染训练方法"]')?.value === '3dgs',
  );
  await page.getByLabel('渲染来源结果', { exact: true }).selectOption(sfm.id);
  await page.getByLabel('训练迭代数', { exact: true }).fill('600');
  await page.getByLabel('图像降采样', { exact: true }).fill('2');
  await page.getByLabel('预览输出间隔', { exact: true }).fill('50');
  await page.getByLabel('学习率', { exact: true }).fill('0.0008');
  assert.equal(await page.getByLabel('最大高斯数', { exact: true }).inputValue(), '20000');
  assert.equal(await page.getByLabel('高斯光栅后端', { exact: true }).inputValue(), 'gsplat');
  await page.screenshot({ path: path.join(report, '3dgs-parameters.png'), fullPage: true });
  response = page.waitForResponse(
    (r) => r.url().endsWith('/api/rendering/runs') && r.request().method() === 'POST',
  );
  await page.getByTestId('start-training').click();
  received = await response;
  assert(received.ok(), await received.text());
  const rgb = await received.json();
  owned.push(rgb.id);
  const request = received.request().postDataJSON();
  assert.equal(request.config.iterations, 600);
  assert.equal(request.config.learning_rate, 0.0008);
  assert.equal(request.config.preview_every, 50);
  assert.equal(request.config.gaussian.max_gaussians, 20000);
  await page.getByAltText('实际渲染输出', { exact: true }).waitFor({ timeout: 120000 });
  await page.waitForFunction(() => window.previewUpdates.length >= 2, null, { timeout: 120000 });
  const during = await get('/api/runs/' + rgb.id);
  assert.equal(during.status, 'running', 'Preview must update while training is running');
  assert(await page.getByLabel('训练迭代数', { exact: true }).isDisabled());
  await page.screenshot({ path: path.join(report, 'live-training.png'), fullPage: true });
  const done = await terminal(rgb.id);
  await page.getByTestId('open-scene-search').waitFor({ timeout: 30000 });
  await page.getByLabel('渲染预览快照', { exact: true }).waitFor();
  const previewCount = await page
    .getByLabel('渲染预览快照', { exact: true })
    .locator('option')
    .count();
  assert(previewCount >= 2);
  const observed = await page.evaluate(() => ({
    previews: window.previewUpdates,
    metrics: window.metricUpdates,
    events: window.workspaceEvents.map((e) => ({
      kind: e.kind,
      stage: e.stage,
      completed: e.completed,
    })),
  }));
  assert(observed.events.some((e) => e.kind === 'preview'));
  assert(observed.metrics.length >= 2);
  await page.screenshot({ path: path.join(report, 'completed-training.png'), fullPage: true });
  await page.getByTestId('open-scene-search').click();
  await page.getByRole('heading', { name: '在自己的场景中查找物体', exact: true }).waitFor();
  await page.getByRole('button', { name: '生成语义索引', exact: true }).waitFor();
  assert.deepEqual(errors, []);
  Object.assign(result, {
    status: 'passed',
    sfm_id: sfm.id,
    rgb_id: rgb.id,
    sfm_registered: completeSfm.registered_images,
    submitted_sfm_config: sfmRequest.config,
    submitted_render_config: request.config,
    live_preview_while_running: true,
    ui_preview_updates: observed.previews.filter((src) => src.includes(`/api/runs/${rgb.id}/`)),
    ui_metric_updates: observed.metrics.length,
    preview_snapshots: previewCount,
    completed_metrics: done.training_metrics,
    semantic_scene_handoff: true,
    page_errors: errors,
  });
} catch (e) {
  Object.assign(result, { status: 'failed', error: String(e), page_errors: errors });
  if (page)
    await page
      .screenshot({ path: path.join(report, 'browser-failure.png'), fullPage: true })
      .catch(() => {});
  process.exitCode = 1;
} finally {
  for (const id of owned) {
    try {
      const r = await get('/api/runs/' + id);
      if (r.status === 'running')
        await page.request.post('http://127.0.0.1:8765/api/runs/' + id + '/cancel');
    } catch {}
  }
  await browser?.close();
  await writeFile(path.join(report, 'browser-check.json'), JSON.stringify(result, null, 2));
  console.log(JSON.stringify(result, null, 2));
}
