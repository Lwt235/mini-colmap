/** Read-only checks against the real local API; never starts a training job. */
import { chromium } from 'playwright';
import assert from 'node:assert/strict';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const base = process.env.WORKBENCH_URL || 'http://127.0.0.1:8765';
assert(['127.0.0.1', 'localhost'].includes(new URL(base).hostname));
const root = fileURLToPath(new URL('../../', import.meta.url));
const artifacts = path.join(
  root,
  '.cache/gaussian-sh-config',
  new Date().toISOString().replaceAll(':', '-'),
);
await mkdir(artifacts, { recursive: true });
const browser = await chromium.launch({
  channel: 'msedge',
  headless: true,
  // This checks form/API behavior; scene interaction has separate WebGL tests.
  args: ['--disable-webgl'],
});
const page = await browser.newPage({ viewport: { width: 1680, height: 1100 } });
const errors = [],
  report = {};
page.on('pageerror', (e) => errors.push(String(e)));
const requests = [];
page.on('requestfinished', (request) => {
  if (request.url().includes('/api/')) requests.push({ url: request.url(), status: 'finished' });
});
page.on('requestfailed', (request) =>
  requests.push({ url: request.url(), error: request.failure()?.errorText }),
);
try {
  const bootstrap = await (await page.request.get(base + '/api/bootstrap')).json();
  assert(
    !bootstrap.runs.some((run) => run.status === 'running'),
    'Run configuration checks while idle',
  );
  const sfm = bootstrap.runs.find((run) => !['nerf', '3dgs'].includes(run.pipeline));
  if (sfm) await page.addInitScript((id) => localStorage.setItem('minicolmap.lastRun', id), sfm.id);
  // The workbench keeps an SSE connection open; wait for its ready UI instead.
  await page.goto(base + "/#workspace", { waitUntil: 'domcontentloaded' });
  await page.locator('.boot-overlay').waitFor({ state: 'detached', timeout: 90000 });
  const panel = page.getByLabel('NeRF 与 3DGS 工作区');
  if (!(await panel.isVisible()))
    await page.getByRole('button', { name: 'NeRF / 3DGS', exact: true }).click();
  await panel.getByLabel('渲染训练方法').selectOption('3dgs');
  const capabilities = await (await page.request.get(base + '/api/rendering/capabilities')).json();
  if (capabilities.gsplat_native_available) {
    await panel
      .getByText('检测到已编译且通过自检的本机 gsplat 扩展；训练时检查 CUDA 设备。')
      .waitFor();
    report.nativeExtensionStatus = true;
  }
  await panel.getByRole('button', { name: '默认', exact: true }).click();
  await panel.getByLabel('渲染训练方法').selectOption('3dgs');
  const capacity = panel.getByLabel('最大高斯数', { exact: true });
  assert.equal(capabilities.limits.max_gaussians, 5000000);
  assert.equal(await capacity.getAttribute('max'), '5000000');
  assert.equal(await capacity.inputValue(), '512');
  await capacity.fill('5000001');
  await panel.getByText('最大高斯数必须是 1 至 5000000 的整数。', { exact: true }).waitFor();
  await capacity.fill('5000000');
  const degree = panel.getByLabel('球谐阶数'),
    interval = panel.getByLabel('球谐升阶间隔');
  assert.equal(await degree.inputValue(), '3');
  assert.equal(await interval.inputValue(), '0');
  assert.equal(await panel.getByLabel('高斯光栅后端').inputValue(), 'reference');
  await degree.selectOption('2');
  await interval.fill('1000');
  const downloading = page.waitForEvent('download');
  await panel.getByRole('button', { name: '导出配置', exact: true }).click();
  const download = await downloading;
  const exported = JSON.parse(await readFile(await download.path(), 'utf8'));
  assert.equal(exported.gaussian.sh_degree, 2);
  assert.equal(exported.gaussian.sh_degree_interval, 1000);
  assert.equal(exported.gaussian.max_gaussians, 5000000);
  const validated = await page.request.post(base + '/api/rendering/config/validate', {
    data: exported,
  });
  assert(validated.ok());
  assert.equal((await validated.json()).gaussian.sh_degree, 2);
  for (const gaussian of [
    { sh_degree: 4 },
    { sh_degree: true },
    { sh_degree_interval: -1 },
    { backend: 'gsplat' },
    { max_gaussians: 5000001 },
    { max_gaussians: true },
  ]) {
    const invalid = await page.request.post(base + '/api/rendering/config/validate', {
      data: { method: '3dgs', device: 'cpu', gaussian },
    });
    assert.equal(invalid.status(), 422);
  }
  await panel.getByLabel('高斯光栅后端').selectOption('gsplat');
  assert((await panel.innerText()).includes('gsplat 光栅后端需要 CUDA 设备'));
  const cuda = await page.request.post(base + '/api/rendering/config/validate', {
    data: { ...exported, device: 'cuda', gaussian: { ...exported.gaussian, backend: 'gsplat' } },
  });
  assert(cuda.ok());
  report.defaultsAndEditing = true;
  report.exportAndServerValidation = true;
  report.invalidSettingsRejected = true;
  report.backendRequiresCuda = true;
  report.fiveMillionCapacityAndExport = true;
  await panel.getByRole('button', { name: '默认', exact: true }).click();
  await panel.getByLabel('渲染训练方法').selectOption('3dgs');
  assert.equal(await degree.inputValue(), '3');
  assert.equal(await interval.inputValue(), '0');
  assert.equal(await capacity.inputValue(), '512');
  report.resetRestoresDefaults = true;
  assert.deepEqual(errors, []);
  await degree.scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(artifacts, 'sh-settings.png'), fullPage: true });
  report.pageErrors = errors;
  report.status = 'passed';
} catch (error) {
  report.status = 'failed';
  report.error = String(error);
  report.requests = requests;
  report.pageErrors = errors;
  process.exitCode = 1;
  await page
    .screenshot({ path: path.join(artifacts, 'failure.png'), fullPage: true })
    .catch(() => {});
} finally {
  await writeFile(path.join(artifacts, 'report.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
  console.log(artifacts);
  await browser.close();
}
