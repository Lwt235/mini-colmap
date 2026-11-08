/** Real browser: restore import/reconstruction/training and use an independent scene. */
import { chromium } from 'playwright';
import assert from 'node:assert/strict';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const root = fileURLToPath(new URL('../../', import.meta.url));
const out = path.join(root, 'reports/new-scenes-20261022');
await mkdir(out, { recursive: true });
const pipeline = JSON.parse(await readFile(path.join(out, 'real-pipeline.json'), 'utf8'));
const rows = JSON.parse(await readFile(path.join(out, 'query-checks.json'), 'utf8'));
let candidate = rows.find((v) => v.result?.selection)?.result.selection;
assert(candidate, 'A real independent-scene candidate was generated');
const result = { status: 'running' };
let browser;
let page;
const errors = [];
async function frame(id, mode, after = 0) {
  await page.waitForFunction(
    ({ id, mode, after }) => {
      const seq = Number(
        document.querySelector('[data-testid=gaussian-live-metrics]')?.dataset.lastSeq,
      );
      const f = window.sceneFrames.get(seq);
      return (
        seq > after &&
        f &&
        (mode === 'none' ? !f.selection : f.selection?.id === id && f.selection?.mode === mode)
      );
    },
    { id, mode, after },
    { timeout: 60000 },
  );
  return page.evaluate(() =>
    Number(document.querySelector('[data-testid=gaussian-live-metrics]').dataset.lastSeq),
  );
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
  page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  page.setDefaultTimeout(45000);
  page.on('pageerror', (e) => errors.push(String(e)));
  await page.addInitScript(() => {
    window.sceneFrames = new Map();
    const Native = window.WebSocket;
    window.WebSocket = class extends Native {
      constructor(...a) {
        super(...a);
        window.sceneFrames.clear();
        this.addEventListener('message', (e) => {
          if (typeof e.data === 'string') {
            const f = JSON.parse(e.data);
            if (f.type === 'frame') window.sceneFrames.set(f.seq, f);
          }
        });
      }
    };
  });
  await page.goto('http://127.0.0.1:8765/');
  await page.getByRole('heading', { name: '用文字找到三维场景中的物体', exact: true }).waitFor();
  const gpu = await (await page.request.get('http://127.0.0.1:8765/api/gpu')).json();
  assert.equal(gpu.selected, 7, 'GPU smoke checks must use physical GPU 7');
  assert(!gpu.busy, 'Another task is active');
  await page.getByRole('button', { name: '重建与训练', exact: true }).click();
  await page.getByLabel('选择输入数据集', { exact: true }).waitFor();
  const runs = await (await page.request.get('http://127.0.0.1:8765/api/runs')).json();
  const rgb = runs.find((r) => r.id === pipeline.rgb);
  assert(rgb);
  await page.getByRole('button', { name: '历史', exact: true }).click();
  await page
    .locator('.run-row')
    .filter({ has: page.getByText(rgb.name, { exact: true }) })
    .click();
  await page.getByTestId('open-scene-search').click();
  await page.getByRole('heading', { name: '在自己的场景中查找物体', exact: true }).waitFor();
  await page.getByLabel('物体描述', { exact: true }).waitFor();
  await page.getByLabel('物体描述', { exact: true }).fill('column');
  const queryResponse = page.waitForResponse(
    (r) =>
      r.url().includes('/api/scenes/') &&
      r.url().endsWith('/query') &&
      r.request().method() === 'POST',
  );
  await page.getByRole('button', { name: '查找物体', exact: true }).click();
  const response = await queryResponse;
  assert(response.ok(), await response.text());
  const launched = await response.json();
  let query;
  for (let i = 0; i < 120; i++) {
    query = await (
      await page.request.get('http://127.0.0.1:8765/api/scene-jobs/' + launched.id)
    ).json();
    if (query.status !== 'running') break;
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  assert.equal(query.status, 'completed', JSON.stringify(query));
  assert(query.result?.selection, JSON.stringify(query));
  candidate = query.result.selection;
  await page.getByRole('button', { name: '高亮', exact: true }).click();
  let seq = await frame(candidate.id, 'highlight');
  await page.screenshot({ path: path.join(out, 'independent-highlight.png'), fullPage: true });
  await page.getByRole('button', { name: '单独查看', exact: true }).click();
  seq = await frame(candidate.id, 'isolate', seq);
  await page.getByRole('button', { name: '恢复场景', exact: true }).click();
  await frame(null, 'none', seq);
  const exported = await page.request.get('http://127.0.0.1:8765' + candidate.export_url);
  assert(exported.ok());
  const bytes = await exported.body();
  assert.equal(bytes.readUInt32LE(0), 0x04034b50);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: path.join(out, 'independent-mobile.png'), fullPage: true });
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 2));
  await page.getByLabel('当前场景', { exact: true }).selectOption('kitchen');
  await page.getByRole('heading', { name: '用文字找到三维场景中的物体', exact: true }).waitFor();
  await page.getByTestId('demo-example-sink').click();
  const catalogue = await (await page.request.get('http://127.0.0.1:8765/api/showcase')).json();
  const sink = catalogue.selections.find((s) => s.id === 'sink');
  await frame(sink.selection_id, 'highlight');
  assert.deepEqual(errors, []);
  Object.assign(result, {
    status: 'passed',
    completed_rgb_scene_handoff: true,
    real_text_query: true,
    independent_scene: pipeline.scene.id,
    selection: candidate.id,
    actual_modes: ['highlight', 'isolate', 'none'],
    export_bytes: bytes.length,
    mobile_no_horizontal_overflow: true,
    kitchen_still_works: true,
    page_errors: errors,
  });
} catch (e) {
  Object.assign(result, { status: 'failed', error: String(e), page_errors: errors });
  if (page)
    await page
      .screenshot({ path: path.join(out, 'browser-failure.png'), fullPage: true })
      .catch(() => {});
  process.exitCode = 1;
} finally {
  await browser?.close();
  await writeFile(path.join(out, 'browser-check.json'), JSON.stringify(result, null, 2));
  console.log(JSON.stringify(result, null, 2));
}
