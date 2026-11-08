import type { DatasetSummary, RenderingConfig, Run } from './types';
import type { PerspectiveCamera } from 'three';

export const MAX_GAUSSIANS = 5_000_000;

export const isRenderingRun = (run: Run | null | undefined) =>
  run?.pipeline === 'nerf' || run?.pipeline === '3dgs';

export const renderingSources = (runs: Run[], method: RenderingConfig['method'] = 'nerf') =>
  runs.filter(
    (run) =>
      !isRenderingRun(run) &&
      ['completed', 'imported'].includes(run.status) &&
      (run.registered_images ?? 0) >= 2 &&
      (method === 'nerf' || (run.point_count ?? 0) > 0),
  );

export function renderingSourceRequest(
  method: RenderingConfig['method'],
  kind: 'result' | 'dataset',
  id: string,
  photoDatasetId: string,
  runs: Run[],
  datasets: DatasetSummary[],
): { source_run_id: string; photo_dataset_id?: string } | { dataset_id: string } {
  if (kind === 'dataset') {
    if (method !== 'nerf') throw new Error('3DGS 需要先构建或加载点云，不能直接使用照片数据集。');
    const dataset = datasets.find((item) => item.id === id);
    if (!dataset || (dataset.pose_count ?? 0) < 2)
      throw new Error('照片缺少至少两台真实相机位姿。请先估计或导入位姿；NeRF 不要求点云。');
    return { dataset_id: dataset.id };
  }
  const source = renderingSources(runs, method).find((item) => item.id === id);
  if (!source)
    throw new Error(
      method === 'nerf'
        ? '请选择至少含两台真实相机位姿的模型，或导入带位姿的照片数据集。'
        : '请选择含点云与至少两台相机位姿的模型，或先构建点云。',
    );
  if (!source.images_available) {
    const photos = datasets.find((item) => item.id === photoDatasetId);
    if (!photos || photos.image_count < 2)
      throw new Error('此模型缺少原图。请关联与模型图像 ID、文件名及内参一致的照片数据集。');
    return { source_run_id: source.id, photo_dataset_id: photos.id };
  }
  return { source_run_id: source.id };
}

/** Early feedback only; the server remains authoritative for all configuration rules. */
export function renderingConfigError(config: RenderingConfig): string | null {
  const modes = {
    optimizer_state: ['preserve', 'reset'],
    lr_mode: ['uniform', 'grouped'],
    density_strategy: ['legacy', 'adaptive'],
    loss: ['mse', 'l1_ssim'],
  };
  for (const key of Object.keys(modes) as (keyof typeof modes)[]) {
    const value = config.gaussian[key];
    if (value !== undefined && !modes[key].includes(value)) return `高斯优化参数 ${key} 无效。`;
  }
  for (const key of ['ssim_weight', 'min_opacity', 'position_lr_final_ratio'] as const) {
    const value = config.gaussian[key];
    if (
      value !== undefined &&
      (!Number.isFinite(value) ||
        value < 0 ||
        value > 1 ||
        (key === 'position_lr_final_ratio' && value === 0))
    )
      return `高斯优化参数 ${key} 超出范围。`;
  }
  for (const key of ['densify_start', 'densify_stop', 'opacity_reset_every'] as const) {
    const value = config.gaussian[key];
    if (value !== undefined && (!Number.isInteger(value) || value < 0 || value > 1000000))
      return `高斯优化参数 ${key} 必须是非负整数。`;
  }
  if ((config.gaussian.densify_stop ?? 15000) <= (config.gaussian.densify_start ?? 500))
    return '自适应增密结束步必须大于开始步。';
  for (const key of ['grow_grad2d', 'split_scale', 'prune_scale', 'prune_screen_radius'] as const) {
    const value = config.gaussian[key];
    if (value !== undefined && (!Number.isFinite(value) || value <= 0))
      return `高斯优化参数 ${key} 必须为正数。`;
  }
  if (
    !Number.isInteger(config.gaussian.max_gaussians) ||
    config.gaussian.max_gaussians < 1 ||
    config.gaussian.max_gaussians > MAX_GAUSSIANS
  )
    return `最大高斯数必须是 1 至 ${MAX_GAUSSIANS} 的整数。`;
  const shDegree = config.gaussian.sh_degree ?? 3;
  const shInterval = config.gaussian.sh_degree_interval ?? 0;
  if (!Number.isInteger(shDegree) || shDegree < 0 || shDegree > 3)
    return '球谐阶数必须是 0 至 3 的整数。';
  if (!Number.isInteger(shInterval) || shInterval < 0 || shInterval > 1000000)
    return '球谐升阶间隔必须是 0 至 1000000 的整数；0 表示直接启用全部阶数。';
  if (
    config.gaussian.backend !== undefined &&
    !['reference', 'gsplat'].includes(config.gaussian.backend)
  )
    return '高斯光栅后端需要选择 reference 或 gsplat。';
  if (config.method === '3dgs' && config.gaussian.backend === 'gsplat' && config.device !== 'cuda')
    return 'gsplat 光栅后端需要 CUDA 设备。';
  if (config.background !== undefined && !['white', 'black'].includes(config.background))
    return '背景需要选择黑色或白色。';
  if (config.stratified_sampling !== undefined && typeof config.stratified_sampling !== 'boolean')
    return '随机深度采样必须为布尔值。';
  if (config.ray_sampling !== undefined && !['image', 'all_images'].includes(config.ray_sampling))
    return '光线批次需要选择单张照片或混合训练照片。';
  const integers = [
    config.iterations,
    config.image_downscale,
    config.rays_per_batch,
    config.samples_per_ray,
    config.holdout_every,
    config.preview_every,
    config.nerf.hidden_dim,
    config.gaussian.max_gaussians,
  ];
  if (integers.some((v) => !Number.isInteger(v) || v <= 0))
    return '迭代数、降采样倍数、批大小、采样数和网络宽度等必须为正整数。';
  if (
    [
      config.seed,
      config.nerf.levels_xyz,
      config.nerf.levels_dir,
      config.gaussian.densify_every,
    ].some((v) => !Number.isInteger(v) || v < 0)
  )
    return '种子、编码层数和增密间隔必须为非负整数。';
  if (!Number.isFinite(config.learning_rate) || config.learning_rate <= 0)
    return '学习率必须为正数。';
  if (
    !Number.isFinite(config.near) ||
    !Number.isFinite(config.far) ||
    config.near < 0 ||
    config.far <= config.near
  )
    return '采样范围需要满足 0 ≤ near < far（归一化场景单位）。';
  if (!Number.isFinite(config.gaussian.initial_scale) || config.gaussian.initial_scale <= 0)
    return '初始高斯尺度必须为正数。';
  if (
    !Number.isFinite(config.gaussian.initial_opacity) ||
    config.gaussian.initial_opacity <= 0 ||
    config.gaussian.initial_opacity >= 1
  )
    return '初始不透明度必须严格位于 0 与 1 之间。';
  return null;
}

export type RenderCamera = {
  K: number[][];
  R: number[][];
  t: number[];
  width: number;
  height: number;
};
/** Capture a free Three camera as an OpenCV world-to-camera pose without moving it. */
export function captureRenderCamera(camera: PerspectiveCamera, maxEdge = 256): RenderCamera {
  if (
    !Number.isInteger(maxEdge) ||
    maxEdge <= 0 ||
    !Number.isFinite(camera.aspect) ||
    camera.aspect <= 0
  )
    throw new Error('输出大小与相机宽高比无效。');
  camera.updateProjectionMatrix();
  camera.updateMatrixWorld(true);
  const width = camera.aspect >= 1 ? maxEdge : Math.max(1, Math.round(maxEdge * camera.aspect));
  const height = camera.aspect >= 1 ? Math.max(1, Math.round(maxEdge / camera.aspect)) : maxEdge;
  const e = camera.matrixWorldInverse.elements,
    p = camera.projectionMatrix.elements;
  // CV coordinates are D * Three camera coordinates, D = diag(1, -1, -1).
  return {
    R: [
      [e[0], e[4], e[8]],
      [-e[1], -e[5], -e[9]],
      [-e[2], -e[6], -e[10]],
    ],
    t: [e[12], -e[13], -e[14]],
    K: [
      [(width * p[0]) / 2, 0, (width * (1 - p[8]) - 1) / 2],
      [0, (height * p[5]) / 2, (height * (1 + p[9]) - 1) / 2],
      [0, 0, 1],
    ],
    width,
    height,
  };
}
export function parseRenderCamera(text: string): RenderCamera {
  const camera = JSON.parse(text) as RenderCamera;
  const matrix = (value: unknown) =>
    Array.isArray(value) &&
    value.length === 3 &&
    value.every(
      (row) =>
        Array.isArray(row) &&
        row.length === 3 &&
        row.every((v) => typeof v === 'number' && Number.isFinite(v)),
    );
  if (
    !matrix(camera.K) ||
    !matrix(camera.R) ||
    !Array.isArray(camera.t) ||
    camera.t.length !== 3 ||
    camera.t.some((v) => typeof v !== 'number' || !Number.isFinite(v))
  )
    throw new Error('K、R 需要为有限的 3×3 数组，t 需要为长度 3 的有限数组。');
  if (![camera.width, camera.height].every((v) => Number.isInteger(v) && v > 0))
    throw new Error('width、height 需要为正整数。');
  return camera;
}
