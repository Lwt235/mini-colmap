import { useCallback, useEffect, useState } from 'react';
import { ArrowRight, FolderInput, LoaderCircle } from 'lucide-react';
import { api, json } from './api';
import ImportDialog from './ImportDialog';
import type { Config, Dataset, DatasetSummary, Run } from './types';
import type { SearchScene } from './sceneTypes';
type Bootstrap = { datasets: DatasetSummary[]; runs: Run[]; defaults: Config };
export default function SceneBuilder({ onOpen }: { onOpen: (scene: SearchScene) => void }) {
  const [data, setData] = useState<Bootstrap | null>(null);
  const [dataset, setDataset] = useState('');
  const [source, setSource] = useState('');
  const [rgb, setRgb] = useState('');
  const [importing, setImporting] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [name, setName] = useState('我的新场景');
  const [steps, setSteps] = useState(10000);
  const [downscale, setDownscale] = useState(2);
  const refresh = useCallback(() => {
    void api<Bootstrap>('/api/bootstrap')
      .then(setData)
      .catch((e) => setError(String(e)));
  }, []);
  useEffect(() => {
    refresh();
    const t = setInterval(() => {
      void api<Run[]>('/api/runs')
        .then((runs) => setData((previous) => (previous ? { ...previous, runs } : previous)))
        .catch((e) => setError(String(e)));
    }, 3000);
    return () => clearInterval(t);
  }, [refresh]);
  const maps =
    data?.runs.filter(
      (r) =>
        (!r.pipeline || r.pipeline === 'sfm') &&
        ['completed', 'imported'].includes(r.status) &&
        (!dataset || r.dataset_id === dataset),
    ) ?? [];
  const models =
    data?.runs.filter(
      (r) => r.pipeline === '3dgs' && ['completed', 'imported'].includes(r.status),
    ) ?? [];
  const current = data?.runs.find((r) => r.status === 'running');
  async function action(fn: () => Promise<void>) {
    setBusy(true);
    setError('');
    try {
      await fn();
      void api<Run[]>('/api/runs').then((runs) =>
        setData((previous) => (previous ? { ...previous, runs } : previous)),
      );
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }
  async function reconstruct() {
    await action(async () => {
      const run = await api<Run>(
        '/api/runs',
        json({ dataset_id: dataset, config: data?.defaults }),
      );
      setSource(run.id);
    });
  }
  async function train() {
    await action(async () => {
      const config = await api<Record<string, unknown>>(
        '/api/rendering/config/validate',
        json({
          method: '3dgs',
          device: 'cuda',
          iterations: steps,
          image_downscale: downscale,
          holdout_every: 8,
          gaussian: {
            backend: 'gsplat',
            scene_normalization: 'camera_extent',
            initial_scale_mode: 'knn',
            sh_degree: 3,
            max_gaussians: 300000,
            density_strategy: 'adaptive',
            optimizer_state: 'preserve',
            lr_mode: 'grouped',
            loss: 'l1_ssim',
          },
        }),
      );
      const run = await api<Run>('/api/rendering/runs', json({ source_run_id: source, config }));
      setRgb(run.id);
    });
  }
  async function open() {
    await action(async () => {
      const scene = await api<SearchScene>('/api/scenes', json({ rgb_run_id: rgb, name }));
      const catalogue = await api<{ scenes: SearchScene[] }>('/api/scenes');
      onOpen(
        catalogue.scenes.find((value) => value.id === scene.id) ?? {
          ...scene,
          ready: false,
          preview_url: `/api/scenes/${scene.id}/photo`,
        },
      );
    });
  }
  const waiting = busy || !!current;
  return (
    <main className="simple-demo scene-builder">
      <div className="simple-intro">
        <h1>从自己的照片创建场景</h1>
        <p>导入照片，重建并训练三维模型，再为这个场景生成语义索引。</p>
      </div>
      {!data && (
        <p role="status" className="simple-notice">
          正在读取已有照片和模型…
        </p>
      )}
      <div className="scene-build-steps">
        <section>
          <span>01</span>
          <h2>导入照片</h2>
          <button className="simple-primary" onClick={() => setImporting(true)} disabled={waiting}>
            <FolderInput size={16} />
            导入照片或数据集
          </button>
          <label>
            已有照片
            <select
              aria-label="已有照片"
              value={dataset}
              onChange={(e) => {
                setDataset(e.target.value);
                setSource('');
              }}
            >
              <option value="">选择照片集</option>
              {data?.datasets.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.name} · {d.image_count} 张
                </option>
              ))}
            </select>
          </label>
          <small>照片需有重叠、清晰且已去畸变。导入时填写真实相机内参。</small>
          <button disabled={!dataset || waiting} onClick={() => void reconstruct()}>
            开始重建
          </button>
        </section>
        <section>
          <span>02</span>
          <h2>训练 3DGS</h2>
          <label>
            重建结果
            <select
              aria-label="重建结果"
              value={source}
              onChange={(e) => setSource(e.target.value)}
            >
              <option value="">选择重建结果</option>
              {maps.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.name ?? r.id} · {r.registered_images} 个视角
                </option>
              ))}
            </select>
          </label>
          <div className="scene-build-options">
            <label>
              训练步数
              <input
                aria-label="训练步数"
                type="number"
                min={1000}
                max={30000}
                step={1000}
                value={steps}
                onChange={(e) => setSteps(Number(e.target.value))}
              />
            </label>
            <label>
              图像缩小
              <select value={downscale} onChange={(e) => setDownscale(Number(e.target.value))}>
                <option value={1}>原始尺寸</option>
                <option value={2}>2 倍</option>
                <option value={4}>4 倍</option>
                <option value={8}>8 倍</option>
              </select>
            </label>
          </div>
          <button
            disabled={!maps.some((r) => r.id === source) || waiting}
            onClick={() => void train()}
          >
            开始训练
          </button>
        </section>
        <section>
          <span>03</span>
          <h2>打开场景</h2>
          <label>
            场景名称
            <input
              aria-label="场景名称"
              maxLength={80}
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          <label>
            已训练模型
            <select aria-label="已训练模型" value={rgb} onChange={(e) => setRgb(e.target.value)}>
              <option value="">选择 3DGS 模型</option>
              {models.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.name ?? r.id} · {r.id.slice(0, 8)}
                </option>
              ))}
            </select>
          </label>
          <p>可立即浏览三维场景；生成语义索引后即可输入文字检索。</p>
          <button
            className="simple-primary"
            disabled={!models.some((r) => r.id === rgb) || waiting}
            onClick={() => void open()}
          >
            打开场景 <ArrowRight size={16} />
          </button>
        </section>
      </div>
      {current && (
        <div role="status" className="scene-build-progress">
          <LoaderCircle size={16} className="spin" />
          {current.pipeline === '3dgs' ? '正在训练三维模型' : '正在重建'} · {current.stage}{' '}
          {current.message}
          <button
            onClick={() =>
              void action(async () => {
                await api(`/api/runs/${current.id}/cancel`, { method: 'POST' });
              })
            }
          >
            停止任务
          </button>
        </div>
      )}
      {data?.runs
        .filter(
          (r) =>
            [source, rgb].includes(r.id) && ['failed', 'todo', 'interrupted'].includes(r.status),
        )
        .map((r) => (
          <p key={r.id} role="alert" className="simple-error">
            {r.error ?? '任务未完成，请检查照片后重试'}
          </p>
        ))}
      {error && (
        <p role="alert" className="simple-error">
          {error}
        </p>
      )}
      {importing && (
        <ImportDialog
          onClose={() => setImporting(false)}
          onImported={(d: Dataset) => {
            setDataset(d.id);
            setSource('');
            refresh();
          }}
        />
      )}
    </main>
  );
}
