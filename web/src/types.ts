export type Config = {
  seed: number;
  features: { max_features: number; ratio: number };
  geometry: {
    threshold_px: number;
    max_trials: number;
    confidence: number;
    min_trials: number;
    min_inliers: number;
    min_pair_inliers?: number;
  };
  mapping: {
    min_pnp_points: number;
    pnp_threshold_px: number;
    min_angle_deg: number;
    max_reprojection_px: number;
    track_conflict_policy?: 'reject' | 'split';
  };
  bundle_adjustment: {
    enabled: boolean;
    max_nfev: number;
    loss: 'linear' | 'huber' | 'soft_l1';
    f_scale: number;
  };
};
export type DatasetSummary = {
  id: string;
  name: string;
  path: string;
  image_count: number;
  pose_count?: number;
};
export type Dataset = DatasetSummary & {
  images: {
    id: number;
    name: string;
    width: number;
    height: number;
    camera_id: number;
    image_url?: string;
  }[];
  cameras: { id: number; width: number; height: number; K: number[][] }[];
};
export type Snapshot = { id: string; label: string; stage: string };
export type Run = {
  gpu_index?: number | null;
  id: string;
  name: string;
  status: 'running' | 'completed' | 'failed' | 'cancelled' | 'interrupted' | 'imported' | 'todo';
  pipeline?: 'sfm' | 'nerf' | '3dgs';
  source_run_id?: string;
  source_kind?: 'sfm_result' | 'posed_dataset';
  source_dataset_id?: string;
  latest_preview?: RenderPreview;
  training_metrics?: TrainingMetrics;
  config?: RenderingConfig;
  stage: string;
  dataset_id?: string;
  registered_images?: number;
  image_count?: number;
  point_count?: number;
  elapsed_seconds?: number;
  completed?: number;
  total?: number;
  message?: string;
  error?: string;
  warnings?: string[];
  snapshots: Snapshot[];
  images_available: boolean;
  output_path?: string;
  preview_max_edge?: number;
};
export type Summary = {
  image_count: number;
  registered_images: number;
  point_count: number;
  observation_count: number;
  mean_reprojection_error_px: number | null;
  median_reprojection_error_px: number | null;
  p95_reprojection_error_px: number | null;
  positive_depth_ratio: number | null;
};
export type Camera = {
  image_id: number;
  name: string;
  camera_id: number;
  width: number;
  height: number;
  K: number[][];
  registered: boolean;
  R?: number[][];
  t?: number[];
  center?: number[];
  frustum?: number[][];
  observations: number;
  image_url?: string;
};
export type Scene = {
  revision: string;
  snapshot_id: string;
  summary: Summary;
  points: {
    ids: number[];
    positions: number[];
    colors: number[];
    errors: (number | null)[];
    track_lengths: number[];
  };
  cameras: Camera[];
  snapshots: Snapshot[];
};
export type PointDetail = {
  id: number;
  xyz: number[];
  color: number[];
  track_length: number;
  mean_error: number | null;
  observations: {
    image_id: number;
    keypoint_index: number;
    xy: number[];
    projected_xy: number[];
    error: number | null;
    image_url?: string;
  }[];
};
export type Selection = { kind: 'camera' | 'point'; id: number } | null;
export type DisplayOptions = {
  points: boolean;
  cameras: boolean;
  axes: boolean;
  pointSize: number;
  cameraScale: number;
  color: 'rgb' | 'error' | 'track';
  maxError: number;
  minTrack: number;
};
export type Artifact = { name: string; url: string };
export type TimingReport = {
  status: 'running' | 'completed' | 'failed';
  unit: 'seconds';
  total_seconds: number;
  stages_seconds: Record<string, number>;
  details: Record<string, { seconds: number; calls: number }>;
};
export type Reports = {
  timings?: TimingReport | null;
  pairs: unknown;
  initialization: unknown;
  ba: {
    accepted: boolean;
    solver_success: boolean;
    nfev: number;
    before_mean_px: number;
    candidate_mean_px: number;
    message: string;
    rejection_reason: string | null;
  } | null;
  summary: unknown;
};
export type LogEntry = { time: string; kind: string; text: string };

export type RenderingConfig = {
  method: 'nerf' | '3dgs';
  iterations: number;
  learning_rate: number;
  image_downscale: number;
  rays_per_batch: number;
  samples_per_ray: number;
  near: number;
  far: number;
  holdout_every: number;
  seed: number;
  device: 'cpu' | 'cuda';
  preview_every: number;
  background?: 'white' | 'black';
  stratified_sampling?: boolean;
  ray_sampling?: 'image' | 'all_images';
  nerf: { levels_xyz: number; levels_dir: number; hidden_dim: number };
  gaussian: {
    initial_scale: number;
    initial_opacity: number;
    max_gaussians: number;
    densify_every: number;
    optimizer_state?: 'preserve' | 'reset';
    lr_mode?: 'uniform' | 'grouped';
    position_lr_final_ratio?: number;
    loss?: 'mse' | 'l1_ssim';
    ssim_weight?: number;
    density_strategy?: 'legacy' | 'adaptive';
    densify_start?: number;
    densify_stop?: number;
    grow_grad2d?: number;
    split_scale?: number;
    prune_scale?: number;
    prune_screen_radius?: number;
    min_opacity?: number;
    opacity_reset_every?: number;
    sh_degree?: number;
    sh_degree_interval?: number;
    backend?: 'reference' | 'gsplat';
  };
};
export type TrainingMetrics = {
  loss?: number;
  psnr?: number;
  ssim?: number;
  step?: number;
  total?: number;
};
export type RenderPreview = {
  url: string;
  reference_url?: string;
  image_id?: number;
  step?: number;
};
export type RenderingResult = {
  pipeline: 'nerf' | '3dgs';
  status: Run['status'];
  source_run_id?: string;
  source_kind?: 'sfm_result' | 'posed_dataset';
  source_dataset_id?: string;
  metrics: TrainingMetrics;
  previews: RenderPreview[];
  checkpoint_url?: string;
  gaussians_url?: string;
  config?: RenderingConfig;
};
