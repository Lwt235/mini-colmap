/** Sustained real UI rendering: 10s warmup + 60s measurement, no training. */
import { chromium } from 'playwright';
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const base = 'http://127.0.0.1:8765';
const runId = process.env.GSPLAT_RUN_ID || '695361f9be8042ed';
const root = fileURLToPath(new URL('../../', import.meta.url));
const output =
  process.env.MINICOLMAP_E2E_REPORT || path.join(root, 'reports/2026-10-18-3dgs-resolution-live');
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ channel: 'msedge', headless: true });
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
const errors = [];
page.on('pageerror', (error) => errors.push(error.message));
const report = {
  run_id: runId,
  scope: 'Headless local Edge, production OrbitControls/JPEG UI, RAF submissions',
  warmup_seconds: 10,
  measurement_seconds: 60,
};
const percentile = (values, p) => {
  const ordered = [...values].sort((a, b) => a - b);
  return ordered[Math.floor((ordered.length - 1) * p)];
};
try {
  await page.addInitScript((id) => {
    localStorage.setItem('minicolmap.lastRun', id);
    window.liveBenchmarkMetadata = new Map();
    const OriginalWebSocket = window.WebSocket;
    window.WebSocket = class extends OriginalWebSocket {
      constructor(...args) {
        super(...args);
        this.addEventListener('message', (event) => {
          if (typeof event.data !== 'string') return;
          const data = JSON.parse(event.data);
          if (data.type === 'frame') window.liveBenchmarkMetadata.set(data.seq, data);
          if (data.type === 'ready') window.liveBenchmarkReady = data;
        });
      }
    };
  }, runId);
  await page.goto(base + "/#workspace", { waitUntil: 'domcontentloaded' });
  await page.getByTestId('gaussian-live-toggle').click({ timeout: 90000 });
  await page.waitForFunction(
    () =>
      Number(
        document
          .querySelector('[data-testid="gaussian-live-metrics"]')
          ?.getAttribute('data-frame-count'),
      ) > 0,
    null,
    { timeout: 75000 },
  );
  await page.getByTestId('gaussian-live-resolution').selectOption('1280');
  await page.getByRole('button', { name: '实时渲染全屏', exact: true }).click();
  await page.waitForFunction(
    () => {
      const img = document.querySelector('[data-testid="gaussian-live-image"]');
      return img?.naturalWidth === 1280 && img?.naturalHeight === 720;
    },
    null,
    { timeout: 15000 },
  );
  const canvas = page.getByTestId('gaussian-live-canvas');
  const box = await canvas.boundingBox();
  assert(box);
  const cx = box.x + box.width / 2,
    cy = box.y + box.height / 2;
  await page.mouse.move(cx, cy);
  await page.mouse.down();
  console.log('Real Edge 720p benchmark started: 10s warmup + 60s sample.');
  const samples = await page.evaluate(
    async ({ cx, cy }) => {
      const node = document.querySelector('[data-testid="gaussian-live-metrics"]');
      const canvas = document.querySelector('[data-testid="gaussian-live-canvas"]');
      const started = performance.now();
      const records = [];
      let previous = 0;
      const observer = new MutationObserver(() => {
        const count = Number(node.getAttribute('data-frame-count'));
        if (count === previous) return;
        previous = count;
        const elapsed = performance.now() - started;
        if (elapsed >= 10000)
          records.push({
            elapsed_ms: elapsed - 10000,
            count,
            seq: Number(node.getAttribute('data-last-seq')),
            latency_ms: Number(node.getAttribute('data-latency-ms')),
            ...window.liveBenchmarkMetadata.get(Number(node.getAttribute('data-last-seq'))),
          });
      });
      observer.observe(node, { attributes: true });
      await new Promise((resolve) => {
        const move = () => {
          const seconds = (performance.now() - started) / 1000;
          if (seconds >= 70) {
            observer.disconnect();
            resolve();
            return;
          }
          canvas.dispatchEvent(
            new PointerEvent('pointermove', {
              bubbles: true,
              pointerId: 1,
              pointerType: 'mouse',
              buttons: 1,
              clientX: cx + Math.sin(seconds * 0.8) * 65,
              clientY: cy + Math.sin(seconds * 0.45) * 15,
            }),
          );
          requestAnimationFrame(move);
        };
        requestAnimationFrame(move);
      });
      return records;
    },
    { cx, cy },
  );
  await page.mouse.up();
  assert(samples.length > 30);
  const gaps = samples.slice(1).map((item, i) => item.elapsed_ms - samples[i].elapsed_ms);
  report.ready = await page.evaluate(() => window.liveBenchmarkReady);
  report.resolution = [1280, 720];
  report.new_image_submissions = samples.length;
  report.actual_duration_seconds = (samples.at(-1).elapsed_ms - samples[0].elapsed_ms) / 1000;
  report.browser_fps = (samples.length - 1) / report.actual_duration_seconds;
  report.frame_interval_p95_ms = percentile(gaps, 0.95);
  for (const key of ['latency_ms', 'render_ms', 'encode_ms', 'readback_ms']) {
    const values = samples.map((item) => item[key]);
    report[key] = {
      mean: values.reduce((sum, value) => sum + value, 0) / values.length,
      p50: percentile(values, 0.5),
      p95: percentile(values, 0.95),
    };
  }
  report.target_30fps_met = report.browser_fps >= 30;
  report.samples = samples;
  await page.screenshot({ path: path.join(output, 'live-720p.png') });
  await page.evaluate(() => document.fullscreenElement && document.exitFullscreen());
  await page.getByTestId('gaussian-live-toggle').click();
  assert.deepEqual(errors, []);
  report.status = 'passed';
} catch (error) {
  report.status = 'failed';
  report.error = String(error);
  process.exitCode = 1;
} finally {
  report.page_errors = errors;
  await writeFile(
    path.join(output, 'live-browser-performance.json'),
    JSON.stringify(report, null, 2),
  );
  console.log(JSON.stringify({ ...report, samples: undefined }, null, 2));
  await browser.close();
}
