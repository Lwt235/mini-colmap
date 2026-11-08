/** History reconciliation against a real local service with an empty history.
 * Only deletes the two model-import directories created by this test.
 */
import { chromium } from 'playwright';
import assert from 'node:assert/strict';
import { access, mkdir, rm, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('../../', import.meta.url));
const base = 'http://127.0.0.1:8765';
const artifacts = path.join(root, '.cache/history-refresh-browser');
await mkdir(artifacts, { recursive: true });
const initial = await (await fetch(base + '/api/runs?discover=true')).json();
assert.equal(
  initial.length,
  0,
  'Use an empty history or an isolated project; no existing runs are touched',
);
const R = [
  [1, 0, 0],
  [0, 1, 0],
  [0, 0, 1],
];
const model = {
  schema_version: 1,
  pose_convention: 'world_to_camera',
  cameras: [
    {
      id: 1,
      width: 64,
      height: 48,
      K: [
        [100, 0, 32],
        [0, 100, 24],
        [0, 0, 1],
      ],
    },
  ],
  images: [
    { id: 1, name: 'a.png', camera_id: 1, keypoints: [[32, 24]] },
    { id: 2, name: 'b.png', camera_id: 1, keypoints: [[7, 24]] },
  ],
  poses: [
    { image_id: 1, R, t: [0, 0, 0] },
    { image_id: 2, R, t: [-1, 0, 0] },
  ],
  points: [
    {
      id: 1,
      xyz: [0, 0, 4],
      color: [200, 100, 50],
      observations: [
        [1, 0],
        [2, 0],
      ],
    },
  ],
};
const owned = [];
const exists = (file) =>
  access(file).then(
    () => true,
    () => false,
  );
async function removeOutput(run) {
  const expected = path.join(root, 'outputs', 'imported_' + run.id);
  assert.match(run.id, /^[a-f0-9]{16}$/);
  assert.equal(path.resolve(run.output_path).toLowerCase(), path.resolve(expected).toLowerCase());
  await rm(expected, { recursive: true, force: true });
}
const browser = await chromium.launch({
  channel: 'msedge',
  headless: true,
  args: ['--use-angle=swiftshader'],
});
const page = await browser.newPage({ viewport: { width: 1500, height: 1000 } });
const errors = [];
const report = {};
page.on('pageerror', (error) => errors.push(error.message));
try {
  for (const name of ['history-keep.json', 'history-remove.json']) {
    const data = new FormData();
    data.append('file', new Blob([JSON.stringify(model)], { type: 'application/json' }), name);
    const response = await fetch(base + '/api/results/import', { method: 'POST', body: data });
    if (!response.ok) throw new Error(await response.text());
    owned.push(await response.json());
  }
  const [kept, removed] = owned;
  await page.addInitScript((id) => localStorage.setItem('minicolmap.lastRun', id), removed.id);
  await page.goto(base + "/#workspace", { waitUntil: 'networkidle' });
  await page.getByRole('button', { name: '历史', exact: true }).click();
  await page.locator('.run-row').nth(1).waitFor();
  await removeOutput(removed);
  await page.getByRole('button', { name: '刷新历史运行', exact: true }).click();
  await page.waitForFunction((id) => localStorage.getItem('minicolmap.lastRun') === id, kept.id);
  assert.equal(await page.locator('.run-row').count(), 1);
  assert.equal(
    await exists(path.join(root, '.cache/mini-colmap-ui/runs', removed.id + '.json')),
    false,
  );
  assert.equal(await exists(kept.output_path), true);
  report.removedIndexAndSelectedValidReplacement = true;
  await removeOutput(kept);
  await page.getByRole('button', { name: '刷新历史运行', exact: true }).click();
  await page.waitForFunction(
    () =>
      document.querySelectorAll('.run-row').length === 0 &&
      localStorage.getItem('minicolmap.lastRun') === null,
  );
  report.emptyHistoryClearsSelectedModelAndStorage = true;
  await page.reload({ waitUntil: 'networkidle' });
  await page.getByRole('button', { name: '历史', exact: true }).click();
  assert.equal(await page.locator('.run-row').count(), 0);
  report.refreshDoesNotRestoreDeletedRecords = true;
  assert.deepEqual(errors, []);
  await page.screenshot({ path: path.join(artifacts, 'empty-history.png'), fullPage: true });
  report.status = 'passed';
} catch (error) {
  report.status = 'failed';
  report.error = String(error);
  process.exitCode = 1;
} finally {
  await browser.close();
  for (const run of owned) await removeOutput(run);
  await fetch(base + '/api/runs?discover=true');
  report.pageErrors = errors;
  await writeFile(path.join(artifacts, 'report.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
}
