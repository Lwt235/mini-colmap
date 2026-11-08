import { useEffect, useRef, useState } from 'react';
import { Download, Image as ImageIcon, LoaderCircle, Play, RotateCcw, Upload } from 'lucide-react';
import { api, downloadJSON, json, number } from './api';
import GaussianLiveViewer from './GaussianLiveViewer';
import {
  isRenderingRun,
  MAX_GAUSSIANS,
  parseRenderCamera,
  renderingConfigError,
  renderingSourceRequest,
  renderingSources,
} from './rendering';
import type {
  DatasetSummary,
  RenderingConfig,
  RenderingResult,
  RenderPreview,
  Run,
  Scene,
} from './types';

type Capabilities = {
  torch_available: boolean;
  gsplat_available?: boolean;
  gsplat_native_available?: boolean;
  cuda_available: boolean | null;
  cuda_note?: string;
  limits?: { max_gaussians: number };
  methods: ('nerf' | '3dgs')[];
  defaults: RenderingConfig;
};
type Props = {
  runs: Run[];
  datasets: DatasetSummary[];
  selectedDatasetId: string;
  run: Run | null;
  scene: Scene | null;
  activeRun: Run | undefined;
  selectedCameraId: number | null;
  onStarted: (run: Run) => void;
  onError: (message: string) => void;
  onPrepareSfM: (datasetId?: string) => void;
  onImportModel: () => void;
  onImportDataset: () => void;
};
const fields: {
  key: keyof RenderingConfig;
  name: string;
  hint: string;
  step?: number;
  min?: number;
}[] = [
  { key: 'iterations', name: '训练迭代数', hint: '步', min: 1 },
  { key: 'learning_rate', name: '学习率', hint: 'Adam 基准', step: 0.0001, min: 0.000001 },
  { key: 'image_downscale', name: '图像降采样', hint: '倍；越大越快', min: 1 },
  { key: 'rays_per_batch', name: '每批光线数', hint: 'NeRF / 批', min: 1 },
  { key: 'samples_per_ray', name: '每条光线采样', hint: 'NeRF / 点', min: 1 },
  { key: 'near', name: '近裁面 near', hint: '归一化场景单位', step: 0.1, min: 0.001 },
  { key: 'far', name: '远裁面 far', hint: '归一化场景单位', step: 0.1, min: 0.001 },
  { key: 'holdout_every', name: '验证图像间隔', hint: '每 N 张留一张', min: 1 },
  { key: 'preview_every', name: '预览输出间隔', hint: '步', min: 1 },
  { key: 'seed', name: '随机种子', hint: '整数', min: 0 },
];

export default function RenderingPanel({
  runs,
  datasets,
  selectedDatasetId,
  run,
  scene,
  activeRun,
  selectedCameraId,
  onStarted,
  onError,
  onPrepareSfM,
  onImportModel,
  onImportDataset,
}: Props) {
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [config, setConfig] = useState<RenderingConfig | null>(null);
  const [sourceId, setSourceId] = useState('');
  const [sourceKind, setSourceKind] = useState<'result' | 'dataset'>('result');
  const [datasetSourceId, setDatasetSourceId] = useState(selectedDatasetId);
  const [photoDatasetId, setPhotoDatasetId] = useState('');
  const [result, setResult] = useState<RenderingResult | null>(null);
  const [preview, setPreview] = useState<RenderPreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [capabilityError, setCapabilityError] = useState('');
  const [cameraId, setCameraId] = useState('');
  const [customCamera, setCustomCamera] = useState('');
  const input = useRef<HTMLInputElement>(null);
  const restoredRunId = useRef('');
  const method = config?.method ?? 'nerf';
  const sources = renderingSources(runs, method);
  const trainingRun = isRenderingRun(run) ? run : null;
  const disabled = busy || !!activeRun;
  const source = sources.find((item) => item.id === sourceId);
  let sourceError = '';
  let sourceRequest: ReturnType<typeof renderingSourceRequest> | null = null;
  try {
    sourceRequest = renderingSourceRequest(
      method,
      sourceKind,
      sourceKind === 'dataset' ? datasetSourceId : sourceId,
      photoDatasetId,
      runs,
      datasets,
    );
  } catch (error) {
    sourceError = (error as Error).message;
  }

  useEffect(() => {
    let cancelled = false;
    api<Capabilities>('/api/rendering/capabilities')
      .then((value) => {
        if (!cancelled) {
          setCapabilities(value);
          setConfig((current) => current ?? structuredClone(value.defaults));
        }
      })
      .catch((error) => {
        if (!cancelled) setCapabilityError(String(error));
      });
    return () => {
      cancelled = true;
    };
  }, []);
  useEffect(() => {
    if (trainingRun?.source_kind === 'posed_dataset') {
      setSourceKind('dataset');
      setDatasetSourceId(trainingRun.source_dataset_id ?? trainingRun.dataset_id ?? '');
      return;
    }
    const selected = trainingRun?.source_run_id ?? (!isRenderingRun(run) ? run?.id : undefined);
    if (selected) setSourceKind('result');
    else if (!sources.length && method === 'nerf' && datasets.length) setSourceKind('dataset');
    if (selected && sources.some((item) => item.id === selected)) setSourceId(selected);
    else
      setSourceId((old) =>
        sources.some((item) => item.id === old) ? old : (sources[0]?.id ?? ''),
      );
    // Keep edited parameters while unrelated run progress changes.
  }, [run?.id, trainingRun?.source_run_id, runs.map((r) => r.id).join(',')]);
  useEffect(() => {
    if (method === '3dgs') setSourceKind('result');
    setSourceId((old) => (sources.some((item) => item.id === old) ? old : (sources[0]?.id ?? '')));
  }, [method]);
  useEffect(() => {
    if (trainingRun?.source_kind !== 'posed_dataset') {
      setDatasetSourceId((old) =>
        datasets.some((item) => item.id === old)
          ? old
          : selectedDatasetId ||
            datasets.find((item) => (item.pose_count ?? 0) >= 2)?.id ||
            datasets[0]?.id ||
            '',
      );
    }
    if (trainingRun?.source_kind === 'sfm_result' && trainingRun.dataset_id)
      setPhotoDatasetId(trainingRun.dataset_id);
  }, [datasets.map((item) => item.id).join(','), selectedDatasetId, trainingRun?.id]);
  useEffect(() => {
    if (trainingRun?.config) setConfig(structuredClone(trainingRun.config));
  }, [trainingRun?.id]);
  useEffect(() => {
    setResult(null);
    setPreview(null);
    restoredRunId.current = '';
  }, [trainingRun?.id]);
  useEffect(() => {
    if (!trainingRun) return;
    let cancelled = false;
    api<RenderingResult>(`/api/rendering/runs/${encodeURIComponent(trainingRun.id)}/result`)
      .then((value) => {
        if (!cancelled) {
          setResult(value);
          if (value.config && restoredRunId.current !== trainingRun.id) {
            setConfig(structuredClone(value.config));
            restoredRunId.current = trainingRun.id;
          }
          setPreview(value.previews.at(-1) ?? trainingRun.latest_preview ?? null);
        }
      })
      .catch((error) => {
        if (!cancelled) onError(String(error));
      });
    return () => {
      cancelled = true;
    };
  }, [
    trainingRun?.id,
    trainingRun?.status,
    trainingRun?.latest_preview?.url,
    trainingRun?.latest_preview?.step,
    onError,
  ]);
  useEffect(() => {
    const selected = scene?.cameras.find(
      (camera) => camera.image_id === selectedCameraId && camera.registered,
    );
    const camera = selected ?? scene?.cameras.find((item) => item.registered);
    if (!camera) return;
    setCameraId(String(camera.image_id));
    setCustomCamera(
      JSON.stringify(
        { K: camera.K, R: camera.R, t: camera.t, width: camera.width, height: camera.height },
        null,
        2,
      ),
    );
  }, [scene?.revision, selectedCameraId]);

  async function start() {
    if (!config || !sourceRequest) return;
    const error = renderingConfigError(config);
    if (error) {
      onError(error);
      return;
    }
    setBusy(true);
    try {
      const checked = await api<RenderingConfig>('/api/rendering/config/validate', json(config));
      setConfig(checked);
      const created = await api<Run>(
        '/api/rendering/runs',
        json({ ...sourceRequest, config: checked }),
      );
      onStarted(created);
    } catch (error) {
      onError(String(error));
    } finally {
      setBusy(false);
    }
  }
  async function render(custom: boolean) {
    if (!trainingRun) return;
    setBusy(true);
    try {
      const body = custom
        ? { camera: parseRenderCamera(customCamera) }
        : { image_id: Number(cameraId) };
      const image = await api<RenderPreview>(
        `/api/rendering/runs/${encodeURIComponent(trainingRun.id)}/render`,
        json(body),
      );
      setPreview(image);
    } catch (error) {
      onError(String(error));
    } finally {
      setBusy(false);
    }
  }

  const metrics = trainingRun?.training_metrics ?? result?.metrics;
  const validation = config ? renderingConfigError(config) : null;
  return (
    <section
      className="rendering-workspace"
      data-testid="rendering-workspace"
      aria-label="NeRF 与 3DGS 工作区"
    >
      <div className="rendering-settings">
        <div className="rendering-title">
          <strong>NeRF / 3DGS</strong>
          <span>训练与新视角渲染</span>
        </div>
        <p className="rendering-note">
          {method === 'nerf'
            ? 'NeRF 使用照片、内参与真实相机位姿，不要求点云。可直接使用已含位姿的照片数据集。'
            : '3DGS 使用照片、内参、相机位姿与初始点云。请先构建点云，或加载已有模型并关联原图。'}
        </p>
        {capabilityError && (
          <p role="alert" className="rendering-error">
            {capabilityError}
          </p>
        )}
        {!capabilities && !capabilityError && <p>正在检查训练环境…</p>}
        {capabilities && !capabilities.torch_available && (
          <p role="alert" className="rendering-error">
            未安装渲染依赖，请完成环境配置后重启服务。
          </p>
        )}
        <fieldset disabled={disabled || !config}>
          {config && (
            <>
              <label className="rendering-wide">
                训练方法
                <select
                  aria-label="渲染训练方法"
                  value={config.method}
                  onChange={(e) =>
                    setConfig({ ...config, method: e.target.value as RenderingConfig['method'] })
                  }
                >
                  <option value="nerf">NeRF · MLP 与体渲染</option>
                  <option value="3dgs">3DGS · 可微高斯渲染</option>
                </select>
              </label>
              {method === 'nerf' && (
                <label className="rendering-wide">
                  NeRF 数据源类型
                  <select
                    aria-label="NeRF 数据源类型"
                    value={sourceKind}
                    onChange={(e) => setSourceKind(e.target.value as 'result' | 'dataset')}
                  >
                    <option value="dataset">已含相机位姿的照片数据集</option>
                    <option value="result">已有相机位姿结果（不要求点云）</option>
                  </select>
                </label>
              )}
              {sourceKind === 'dataset' && method === 'nerf' ? (
                <label className="rendering-wide">
                  照片与相机位姿
                  <select
                    aria-label="NeRF 位姿照片数据集"
                    value={datasetSourceId}
                    onChange={(e) => setDatasetSourceId(e.target.value)}
                  >
                    <option value="">选择数据集</option>
                    {datasets.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.name} · {item.image_count} 张 / {item.pose_count ?? 0} 位姿
                      </option>
                    ))}
                  </select>
                </label>
              ) : (
                <label className="rendering-wide">
                  {method === 'nerf' ? '相机位姿结果' : '初始点云模型'}
                  <select
                    aria-label="渲染来源结果"
                    value={sourceId}
                    onChange={(e) => setSourceId(e.target.value)}
                  >
                    <option value="">
                      {method === 'nerf'
                        ? '选择至少含两台位姿的模型'
                        : '选择含点云与相机位姿的模型'}
                    </option>
                    {sources.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.name} · {item.registered_images} 相机
                        {method === '3dgs'
                          ? ` / ${item.point_count} 点`
                          : item.point_count === 0
                            ? ' · 无点云（可训练）'
                            : ''}
                        {!item.images_available ? ' · 待关联原图' : ''}
                      </option>
                    ))}
                  </select>
                </label>
              )}
              {sourceKind === 'result' && source && !source.images_available && (
                <label className="rendering-wide">
                  关联原始照片数据集
                  <select
                    aria-label="关联原始照片数据集"
                    value={photoDatasetId}
                    onChange={(e) => setPhotoDatasetId(e.target.value)}
                  >
                    <option value="">请选择匹配原图与内参的数据集</option>
                    {datasets.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.name} · {item.image_count} 张
                      </option>
                    ))}
                  </select>
                  <small>
                    模型已有位姿；照片集无需另含位姿。后端严格核对图像 ID、文件名、相机与尺寸。
                  </small>
                </label>
              )}
              {sourceError && (
                <div className="rendering-source-help" data-testid="rendering-source-help">
                  <p className="rendering-note">{sourceError}</p>
                  <div className="rendering-source-actions">
                    <button
                      type="button"
                      className="outline"
                      onClick={() =>
                        onPrepareSfM(sourceKind === 'dataset' ? datasetSourceId : undefined)
                      }
                    >
                      {method === 'nerf' ? '先估计相机位姿' : '先构建点云'}
                    </button>
                    <button type="button" className="outline" onClick={onImportModel}>
                      {method === 'nerf' ? '加载已有位姿模型' : '加载已有模型'}
                    </button>
                    <button type="button" className="outline" onClick={onImportDataset}>
                      导入照片 / 位姿数据集
                    </button>
                  </div>
                </div>
              )}
              <label className="rendering-wide">
                计算设备
                <select
                  aria-label="渲染计算设备"
                  value={config.device}
                  onChange={(e) =>
                    setConfig({ ...config, device: e.target.value as RenderingConfig['device'] })
                  }
                >
                  <option value="cpu">CPU</option>
                  <option value="cuda" disabled={capabilities?.cuda_available === false}>
                    CUDA{' '}
                    {capabilities?.cuda_available == null
                      ? '· 启动时检查'
                      : capabilities.cuda_available
                        ? '· 可用'
                        : '· 不可用'}
                  </option>
                </select>
              </label>
              <div className="rendering-fields">
                <label>
                  图像背景
                  <select
                    aria-label="图像背景"
                    value={config.background ?? 'white'}
                    onChange={(e) =>
                      setConfig({ ...config, background: e.target.value as 'white' | 'black' })
                    }
                  >
                    <option value="white">白色</option>
                    <option value="black">黑色</option>
                  </select>
                  <small>匹配照片的背景颜色</small>
                </label>
                {method === 'nerf' && (
                  <>
                    <label>
                      训练深度采样
                      <select
                        aria-label="训练深度采样"
                        value={config.stratified_sampling ? 'random' : 'fixed'}
                        onChange={(e) =>
                          setConfig({ ...config, stratified_sampling: e.target.value === 'random' })
                        }
                      >
                        <option value="fixed">固定等距</option>
                        <option value="random">随机分层</option>
                      </select>
                      <small>渲染预览始终使用固定采样</small>
                    </label>
                    <label>
                      光线批次
                      <select
                        aria-label="光线批次"
                        value={config.ray_sampling ?? 'image'}
                        onChange={(e) =>
                          setConfig({
                            ...config,
                            ray_sampling: e.target.value as 'image' | 'all_images',
                          })
                        }
                      >
                        <option value="image">每批单张照片</option>
                        <option value="all_images">混合训练照片</option>
                      </select>
                      <small>混合模式仅使用训练照片</small>
                    </label>
                  </>
                )}
                {fields
                  .filter(
                    (field) =>
                      method === 'nerf' ||
                      !['rays_per_batch', 'samples_per_ray'].includes(field.key),
                  )
                  .map((field) => (
                    <label key={field.key}>
                      {field.name}
                      <input
                        aria-label={field.name}
                        type="number"
                        step={field.step ?? 1}
                        min={field.min}
                        value={config[field.key] as number}
                        onChange={(e) =>
                          setConfig({ ...config, [field.key]: e.target.valueAsNumber })
                        }
                      />
                      <small>{field.hint}</small>
                    </label>
                  ))}
              </div>
              {method === 'nerf' && (
                <details open>
                  <summary>NeRF 网络参数</summary>
                  <div className="rendering-fields">
                    {(
                      [
                        ['levels_xyz', '位置编码层数'],
                        ['levels_dir', '方向编码层数'],
                        ['hidden_dim', '隐藏层宽度'],
                      ] as const
                    ).map(([key, label]) => (
                      <label key={key}>
                        {label}
                        <input
                          type="number"
                          aria-label={label}
                          min={key === 'hidden_dim' ? 1 : 0}
                          value={config.nerf[key]}
                          onChange={(e) =>
                            setConfig({
                              ...config,
                              nerf: { ...config.nerf, [key]: e.target.valueAsNumber },
                            })
                          }
                        />
                      </label>
                    ))}
                  </div>
                </details>
              )}
              {method === '3dgs' && (
                <details open>
                  <summary>3DGS 高斯参数</summary>
                  <div className="rendering-fields">
                    {(
                      [
                        [
                          'optimizer_state',
                          '增密时的 Adam 状态',
                          'preserve',
                          [
                            ['preserve', '保留已有高斯动量'],
                            ['reset', '整体重置（历史消融基线）'],
                          ],
                        ],
                        [
                          'lr_mode',
                          '高斯学习率策略',
                          'uniform',
                          [
                            ['uniform', '统一学习率'],
                            ['grouped', '参数分组＋位置衰减'],
                          ],
                        ],
                        [
                          'density_strategy',
                          '密度控制策略',
                          'legacy',
                          [
                            ['legacy', '原有中心梯度策略'],
                            ['adaptive', '屏幕梯度累计＋克隆／分裂'],
                          ],
                        ],
                        [
                          'loss',
                          '图像训练损失',
                          'mse',
                          [
                            ['mse', 'MSE'],
                            ['l1_ssim', 'L1＋SSIM'],
                          ],
                        ],
                      ] as const
                    ).map(([key, label, fallback, choices]) => (
                      <label key={key}>
                        {label}
                        <select
                          aria-label={label}
                          value={config.gaussian[key] ?? fallback}
                          onChange={(e) =>
                            setConfig({
                              ...config,
                              gaussian: { ...config.gaussian, [key]: e.target.value },
                            })
                          }
                        >
                          {choices.map(([value, text]) => (
                            <option key={value} value={value}>
                              {text}
                            </option>
                          ))}
                        </select>
                      </label>
                    ))}
                    <label>
                      球谐阶数
                      <select
                        aria-label="球谐阶数"
                        value={config.gaussian.sh_degree ?? 3}
                        onChange={(e) =>
                          setConfig({
                            ...config,
                            gaussian: { ...config.gaussian, sh_degree: Number(e.target.value) },
                          })
                        }
                      >
                        {[0, 1, 2, 3].map((degree) => (
                          <option key={degree} value={degree}>
                            {degree} 阶 · {(degree + 1) ** 2} 个系数 / 通道
                          </option>
                        ))}
                      </select>
                    </label>
                    <label>
                      球谐升阶间隔
                      <input
                        type="number"
                        min={0}
                        max={1000000}
                        step={1}
                        aria-label="球谐升阶间隔"
                        value={config.gaussian.sh_degree_interval ?? 0}
                        onChange={(e) =>
                          setConfig({
                            ...config,
                            gaussian: {
                              ...config.gaussian,
                              sh_degree_interval: e.target.valueAsNumber,
                            },
                          })
                        }
                      />
                    </label>
                    <label>
                      高斯光栅后端
                      <select
                        aria-label="高斯光栅后端"
                        value={config.gaussian.backend ?? 'reference'}
                        onChange={(e) =>
                          setConfig({
                            ...config,
                            gaussian: {
                              ...config.gaussian,
                              backend: e.target.value as 'reference' | 'gsplat',
                            },
                          })
                        }
                      >
                        <option value="reference">reference · 数学验证</option>
                        <option value="gsplat">gsplat · CUDA 光栅加速</option>
                      </select>
                    </label>
                    {(
                      [
                        ['initial_scale', '初始高斯尺度', 0.001],
                        ['initial_opacity', '初始不透明度', 0.01],
                        ['max_gaussians', '最大高斯数', 1],
                        ['densify_every', '增密间隔', 1],
                      ] as const
                    ).map(([key, label, step]) => (
                      <label key={key}>
                        {label}
                        <input
                          type="number"
                          aria-label={label}
                          min={key === 'max_gaussians' ? 1 : 0}
                          max={
                            key === 'max_gaussians'
                              ? (capabilities?.limits?.max_gaussians ?? MAX_GAUSSIANS)
                              : undefined
                          }
                          step={step}
                          value={config.gaussian[key]}
                          onChange={(e) =>
                            setConfig({
                              ...config,
                              gaussian: { ...config.gaussian, [key]: e.target.valueAsNumber },
                            })
                          }
                        />
                      </label>
                    ))}
                  </div>
                  <details>
                    <summary>优化与密度控制高级参数</summary>
                    <div className="rendering-fields">
                      {(
                        [
                          ['position_lr_final_ratio', '位置最终学习率比例', 0.01, 0.01],
                          ['ssim_weight', 'SSIM 损失权重', 0.2, 0.05],
                          ['densify_start', '自适应增密开始步', 500, 1],
                          ['densify_stop', '自适应增密结束步', 15000, 1],
                          ['grow_grad2d', '累计屏幕梯度阈值', 0.0002, 0.0001],
                          ['split_scale', '克隆／分裂尺度阈值', 0.01, 0.001],
                          ['prune_scale', '最大三维尺度', 0.1, 0.01],
                          ['prune_screen_radius', '最大屏幕半径比例', 0.15, 0.01],
                          ['min_opacity', '剪枝透明度下限', 0.005, 0.001],
                          ['opacity_reset_every', '透明度重置间隔（0关闭）', 3000, 1],
                        ] as const
                      ).map(([key, label, fallback, step]) => (
                        <label key={key}>
                          {label}
                          <input
                            type="number"
                            aria-label={label}
                            step={step}
                            min={0}
                            value={config.gaussian[key] ?? fallback}
                            onChange={(e) =>
                              setConfig({
                                ...config,
                                gaussian: { ...config.gaussian, [key]: e.target.valueAsNumber },
                              })
                            }
                          />
                        </label>
                      ))}
                    </div>
                  </details>
                </details>
              )}
            </>
          )}
        </fieldset>
        <div className="rendering-config-actions">
          <button
            title="恢复渲染默认参数"
            disabled={disabled || !capabilities}
            onClick={() => setConfig(structuredClone(capabilities!.defaults))}
          >
            <RotateCcw size={13} />
            默认
          </button>
          <button disabled={!config} onClick={() => downloadJSON('rendering-config.json', config)}>
            <Download size={13} />
            导出配置
          </button>
          <button disabled={disabled} onClick={() => input.current?.click()}>
            <Upload size={13} />
            导入配置
          </button>
          <input
            hidden
            ref={input}
            type="file"
            accept=".json"
            onChange={async (e) => {
              const file = e.target.files?.[0];
              e.target.value = '';
              if (!file) return;
              try {
                const value = await api<RenderingConfig>(
                  '/api/rendering/config/validate',
                  json(JSON.parse(await file.text())),
                );
                const error = renderingConfigError(value);
                if (error) throw new Error(error);
                setConfig(value);
              } catch (error) {
                onError(`配置无效：${String(error)}`);
              }
            }}
          />
        </div>
        {validation && (
          <p className="rendering-error" role="alert">
            {validation}
          </p>
        )}
        <button
          className="primary full"
          data-testid="start-training"
          disabled={
            disabled || !sourceRequest || !config || !!validation || !capabilities?.torch_available
          }
          onClick={() => void start()}
        >
          {busy ? <LoaderCircle size={14} className="spin" /> : <Play size={14} />}开始训练
        </button>
        {activeRun && <p className="rendering-note">当前任务运行中，参数已锁定；可在顶部取消。</p>}
      </div>
      <div className="rendering-results">
        <div className="rendering-result-heading">
          <div>
            <strong>训练与新视角渲染</strong>
            <span>
              {trainingRun
                ? `${trainingRun.pipeline?.toUpperCase()} · ${trainingRun.name}`
                : '选择参数并启动训练，或从历史打开训练记录'}
            </span>
          </div>
        </div>
        {trainingRun?.status === 'todo' && (
          <div className="rendering-todo" data-testid="rendering-todo">
            <strong>训练未完成</strong>
            <p>请查看运行日志，检查数据与训练配置。</p>
          </div>
        )}
        {trainingRun?.error && trainingRun.status !== 'todo' && (
          <p className="rendering-error" role="alert">
            {trainingRun.error}
          </p>
        )}
        <div className="rendering-metrics">
          <div>
            <small>迭代进度</small>
            <strong>
              {metrics?.step ?? trainingRun?.completed ?? '—'} /{' '}
              {metrics?.total ?? trainingRun?.total ?? '—'}
            </strong>
          </div>
          <div>
            <small>训练 loss</small>
            <strong>{number(metrics?.loss, 6)}</strong>
          </div>
          <div>
            <small>验证 PSNR</small>
            <strong>
              {number(metrics?.psnr, 2)} <small>dB</small>
            </strong>
          </div>
          {metrics?.ssim !== undefined && (
            <div>
              <small>最终验证 SSIM</small>
              <strong>{number(metrics.ssim, 4)}</strong>
            </div>
          )}
        </div>
        {trainingRun?.pipeline === '3dgs' && trainingRun.status === 'completed' && (
          <GaussianLiveViewer
            key={trainingRun.id}
            runId={trainingRun.id}
            scene={scene}
            selectedCameraId={selectedCameraId}
            disabled={!!activeRun}
          />
        )}
        <div className="rendering-comparison" data-testid="rendering-comparison">
          <figure>
            <figcaption>
              渲染输出 {preview?.step != null && <span>step {preview.step}</span>}
            </figcaption>
            {preview?.url ? (
              <a href={preview.url} target="_blank" rel="noreferrer">
                <img src={preview.url} alt="实际渲染输出" />
              </a>
            ) : (
              <div className="rendering-empty">
                <ImageIcon size={32} />
                <p>尚无渲染图像</p>
                <small>完成训练预览后，在此显示验证视角的实际渲染图像。</small>
              </div>
            )}
          </figure>
          <figure>
            <figcaption>
              对应原图 {preview?.image_id != null && <span>#{preview.image_id}</span>}
            </figcaption>
            {preview?.reference_url ? (
              <a href={preview.reference_url} target="_blank" rel="noreferrer">
                <img src={preview.reference_url} alt="对应参考原图" />
              </a>
            ) : (
              <div className="rendering-empty">
                <ImageIcon size={32} />
                <p>此视角没有参考原图</p>
              </div>
            )}
          </figure>
        </div>
        {!!result?.previews.length && (
          <label className="rendering-preview-select">
            查看预览快照
            <select
              aria-label="渲染预览快照"
              value={preview?.url ?? ''}
              onChange={(e) =>
                setPreview(result.previews.find((item) => item.url === e.target.value) ?? null)
              }
            >
              {!result.previews.some((item) => item.url === preview?.url) && (
                <option value={preview?.url}>手动渲染结果</option>
              )}
              {result.previews.map((item) => (
                <option key={item.url} value={item.url}>
                  step {item.step ?? '—'} · 图像 #{item.image_id ?? '新视角'}
                </option>
              ))}
            </select>
          </label>
        )}
        {trainingRun && (
          <div className="rendering-downloads">
            {result?.checkpoint_url && (
              <a href={result.checkpoint_url} download>
                <Download size={13} />
                模型 checkpoint
              </a>
            )}
            {result?.gaussians_url && (
              <a href={result.gaussians_url} download>
                <Download size={13} />
                高斯参数
              </a>
            )}
          </div>
        )}
        <div className="rendering-camera-controls">
          <label>
            原始相机
            <select
              aria-label="待渲染相机"
              value={cameraId}
              onChange={(e) => setCameraId(e.target.value)}
            >
              {scene?.cameras
                .filter((camera) => camera.registered)
                .map((camera) => (
                  <option key={camera.image_id} value={camera.image_id}>
                    #{camera.image_id} · {camera.name}
                  </option>
                ))}
            </select>
          </label>
          <button
            className="outline"
            disabled={busy || !trainingRun || trainingRun.status !== 'completed' || !cameraId}
            onClick={() => void render(false)}
          >
            渲染此相机
          </button>
        </div>
        <details className="rendering-custom-camera">
          <summary>自定义新视角（源场景世界坐标，world-to-camera）</summary>
          <p className="rendering-note">
            K、R、t 沿用项目约定 Xc = R X + t，width / height
            为输出分辨率。此处修改只用于渲染，不修改源相机。
          </p>
          <textarea
            aria-label="新视角相机 JSON"
            value={customCamera}
            onChange={(e) => setCustomCamera(e.target.value)}
            spellCheck={false}
          />
          <button
            className="outline"
            disabled={busy || !trainingRun || trainingRun.status !== 'completed' || !customCamera}
            onClick={() => void render(true)}
          >
            渲染自定义新视角
          </button>
        </details>
      </div>
    </section>
  );
}
