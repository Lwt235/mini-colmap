# 参数与数据格式

SfM 与新视图训练使用独立 JSON 配置。CLI 和界面复用同一校验函数，允许只提供部分覆盖项，拒绝未知字段、错误类型和非法数值。启动时保存展开后的完整配置，修改面板不会改变已经启动的任务。

## SfM 参数

默认值以 `src/minicolmap/config.py` 为准。`mapper` 的输入为完整的 `PipelineConfig`，其中包含 `mapping` 分组。

| 字段 | 默认值 | 含义 / 约束 |
| --- | --- | --- |
| `seed` | 0 | 非负整数随机种子 |
| `features.max_features` | 4000 | 每图 SIFT 特征预算，整数 ≥2 |
| `features.ratio` | 0.75 | 描述子 ratio 门槛，0–1 开区间 |
| `geometry.threshold_px` | 1.5 | Sampson 门槛，px；比较平方距离时使用其平方 |
| `geometry.max_trials` | 2000 | RANSAC 硬迭代上限，正整数 |
| `geometry.confidence` | 0.99 | 自适应 RANSAC 目标成功概率，0–1 开区间 |
| `geometry.min_trials` | 20 | 最小采样预算，正整数；硬上限优先 |
| `geometry.min_inliers` | 30 | 初始化最低有效点数，整数 ≥8；未单独设置图像对门槛时也用于图像对准入 |
| `geometry.min_pair_inliers` | 跟随 `min_inliers` | 可选的图像对准入门槛，整数 ≥8；允许弱连接进入轨迹图而不降低初始化要求 |
| `mapping.min_pnp_points` | 15 | 唯一 2D–3D 输入对应及保留内点门槛，整数 ≥6 |
| `mapping.pnp_threshold_px` | 3.0 | PnP-RANSAC 门槛，正数 px |
| `mapping.min_angle_deg` | 1.5 | 最低三角化视差角，0–180 开区间，度 |
| `mapping.max_reprojection_px` | 3.0 | 新点及新增观测的重投影误差门槛，正数 px |
| `mapping.track_conflict_policy` | reject | 可选的 `reject`（整组丢弃）或 `split`（保留一致子轨迹），旧配置保持原行为 |
| `bundle_adjustment.enabled` | false | 是否执行最终全局 BA；两视图初始化精化单独执行 |
| `bundle_adjustment.max_nfev` | 50 | 求解器函数评估预算，正整数 |
| `bundle_adjustment.loss` | huber | `linear`、`huber` 或 `soft_l1` |
| `bundle_adjustment.f_scale` | 1.0 | 鲁棒损失尺度，正数，px |

`max_nfev` 以求解器函数评估次数为预算单位，数值差分还可能产生额外的底层残差调用。初始化剪枝是当前 mapper 的固定内置策略。

```json
{
  "geometry": {"confidence": 0.995, "min_trials": 30, "max_trials": 2000},
  "bundle_adjustment": {"enabled": true, "max_nfev": 100}
}
```

模板：`configs/default.json`、`configs/temple_ring_no_ba.json`、`configs/temple_ring_ba.json`。

`configs/dino_sfm.json` 将图像对门槛设为 20、初始化仍设为 30，并启用 `split`；PnP 仍至少 15 点、最大重投影误差仍为 3 px。拆分策略优先处理有观察三角支持的边，再按图像对支持数和固定 ID 顺序处理，阻止同图不同特征合并。保留的匹配继续接受后续几何检查。

## NeRF / 3DGS 参数

默认值以 `src/minicolmap/rendering/config.py` 为准。完整配置同时保存 `nerf` 和 `gaussian` 分组，实际方法由 `method` 决定。

### 通用参数

| 字段 | 默认值 | 含义 / 有效值 |
| --- | --- | --- |
| `method` | nerf | `nerf` 或 `3dgs` |
| `iterations` | 1000 | 训练步数，整数 1–1,000,000 |
| `learning_rate` | 0.001 | Adam 统一学习率，0–1，排除 0 |
| `image_downscale` | 8 | 图像缩小倍数，整数 1–128；1 保持原尺寸，同步变换 K |
| `rays_per_batch` | 256 | NeRF 每步射线数，整数 1–65,536 |
| `samples_per_ray` | 32 | NeRF 每射线采样数，整数 2–1,024 |
| `near` / `far` | 0.1 / 4.0 | NeRF 归一化采样距离，0≤near<far≤1,000,000 |
| `holdout_every` | 4 | 对排序后的有效图像每隔 N 张取验证视图，整数 ≥2 |
| `seed` | 0 | 随机种子，整数 0–2,147,483,647 |
| `device` | cpu | `cpu` 或 `cuda` |
| `preview_every` | 100 | 验证预览与中间权重保存间隔，整数 1–1,000,000 |
| `background` | white | `white` 或 `black`，匹配照片背景 |
| `stratified_sampling` | false | NeRF 训练时是否随机分层采样，布尔值 |
| `ray_sampling` | image | NeRF 的 `image` 单图或 `all_images` 跨图射线采样 |

`holdout_every` 上限为 1,000,000；少量图像也会保留至少一个验证视图。预览期间的 PSNR 可仅对应首个验证视图，最终汇总的 `psnr_scope` 标识其范围。

### NeRF 网络

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `nerf.levels_xyz` | 6 | 位置编码频带数，整数 0–16 |
| `nerf.levels_dir` | 4 | 方向编码频带数，整数 0–16 |
| `nerf.hidden_dim` | 64 | 主干隐藏维数，整数 4–1,024 |

`configs/nerf_smoke.json` 为 CPU 小规模检查；`nerf_gpu.json` 为 GPU 起点；`nerf_temple_quality.json` 为 TempleRing 特定场景配置；`nerf_deep_diagnostic.json` 用于对照诊断，不作为画质推荐配置。

### 高斯参数

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `gaussian.initial_scale` | 0.01 | 初始尺度，归一化世界单位，0–10，排除 0 |
| `gaussian.initial_opacity` | 0.1 | 初始透明度参数，0–1 开区间 |
| `gaussian.max_gaussians` | 512 | 初始化与增密容量，整数 1–5,000,000 |
| `gaussian.densify_every` | 0 | 密度控制间隔，整数 0–1,000,000；0 关闭 |
| `gaussian.sh_degree` | 3 | 最大实球谐阶数，整数 0–3 |
| `gaussian.sh_degree_interval` | 0 | 球谐升阶间隔，整数 0–1,000,000；0 立即启用最大阶数 |
| `gaussian.backend` | reference | `reference` 或 `gsplat`；后者要求 CUDA |
| `gaussian.optimizer_state` | preserve | 拓扑变化时迁移 Adam；`reset` 为历史整体重置控制组 |
| `gaussian.lr_mode` | uniform | `uniform` 或 `grouped`（五类参数分组、位置衰减） |
| `gaussian.position_lr_final_ratio` | 0.01 | 分组策略的位置最终/初始学习率比例，0–1，排除 0 |
| `gaussian.loss` / `ssim_weight` | mse / 0.2 | `mse` 或 `l1_ssim`；后者 SSIM 权重为 0–1 |
| `gaussian.density_strategy` | legacy | 原中心梯度或 `adaptive` 累计可见屏幕梯度策略 |
| `gaussian.densify_start` / `densify_stop` | 500 / 15000 | 自适应增密窗口，整数；结束严格大于开始 |
| `gaussian.grow_grad2d` | 0.0002 | 累计归一化屏幕梯度阈值，正数 |
| `gaussian.split_scale` | 0.01 | 小高斯克隆/大高斯分裂界限，归一化尺度，正数 |
| `gaussian.prune_scale` | 0.1 | 自适应最大三维尺度，正数 |
| `gaussian.prune_screen_radius` | 0.15 | 自适应最大屏幕半径/图像长边，正数 |
| `gaussian.min_opacity` | 0.005 | 密度剪枝透明度下限，0–1 |
| `gaussian.opacity_reset_every` | 3000 | 自适应透明度重置间隔，整数 0–1,000,000；0 关闭；距训练/增密窗口结束不超过一轮训练视角时跳过 |

各优化开关独立，历史配置缺失字段由后端补默认；训练器的改动与消融结果见 [3DGS 优化说明](3dgs-optimization-ablation.md)。SH DC 与高阶系数当前仍共用一组学习率。

`sh_degree_interval>0` 时，活跃阶数从 0 开始，按间隔升阶，直至配置的最大阶数。参考后端的中间量随高斯数和像素数增长；百万规模应使用 gsplat，并按设备显存设置预算。容量校验检查配置数值范围，实际高斯数由初始化与增密/剪枝共同决定。

模板：`3dgs_smoke.json` 为 CPU 检查，`3dgs_gpu.json` 为 CUDA reference 起点，`3dgs_gsplat.json` 选择加速后端。示例覆盖：

```json
{
  "method": "3dgs",
  "device": "cuda",
  "iterations": 5000,
  "image_downscale": 4,
  "background": "black",
  "gaussian": {"backend": "gsplat", "max_gaussians": 100000, "densify_every": 100}
}
```

## 输入清单

### 照片与内参

```json
{
  "cameras": [
    {"id": 1, "width": 640, "height": 480, "K": [[800, 0, 320], [0, 800, 240], [0, 0, 1]]}
  ],
  "images": [
    {"id": 1, "name": "view01.jpg", "camera_id": 1, "path": "images/view01.jpg"},
    {"id": 2, "name": "view02.jpg", "camera_id": 1, "path": "images/view02.jpg"}
  ]
}
```

此处 K 演示数据结构，使用时请填写实际内参。ID 唯一且引用有效，相机尺寸与照片一致；可定义多台已知内参相机。输入须为已去畸变照片，并提供对应的相机内参。

### 可直接训练 NeRF 的位姿

在同一清单中增加：

```json
{
  "pose_convention": "world_to_camera",
  "poses": [
    {"image_id": 1, "R": [[1, 0, 0], [0, 1, 0], [0, 0, 1]], "t": [0, 0, 0]},
    {"image_id": 2, "R": [[1, 0, 0], [0, 1, 0], [0, 0, 1]], "t": [-0.1, 0, 0]}
  ]
}
```

该片段演示位姿格式，使用时须与前面的照片/内参对象合并。位姿遵循 `Xc=R@Xw+t`，每条位姿严格包含 `image_id/R/t`，R 必须为合法旋转。训练至少需要两张具有有效位姿的照片；3DGS 还需提供带有效稀疏点的模型。

### 模型与原图关联

`model.json` 保存相机、图像、已注册位姿、三维点、颜色及观测，读写由 `minicolmap.io` 管理。训练按图像身份和内参校验模型与照片清单的对应关系。通用模型读取使用本项目 JSON 格式，COLMAP TXT 用于导出；已去畸变数据的受限二进制导入见 [LERF 数据指南](lerf-data-guide.md)。

## 输出与历史

| 产物 | 用途 |
| --- | --- |
| `model.json`、`points.ply`、`sparse/0/*.txt` | SfM 地图、点云、COLMAP 文本模型 |
| `summary.json`、`config.json`、`run_status.json` | 结果汇总、完整配置和状态 |
| `timings.json`、`pairs.json`、`initialization.json`、`ba.json` | 阶段耗时、匹配/初始化与 BA 诊断；按运行阶段产生 |
| `matching_graph.json` | 准入图像对的连通分量、初始地图可达数量、无法连接的图像 ID 与轨迹策略 |
| `ui/snapshots/`、事件与工作进程日志 | 工作台真实中间场景及诊断 |
| `checkpoint.pt` | NeRF 网络或高斯张量、配置、步数、帧、归一化与训练/验证划分 |
| `gaussians.npz` | 3DGS 世界坐标中心、协方差、尺度、四元数、SH 与透明度 |
| `previews/`、训练指标 | 实际渲染图、对照原图及优化记录 |
| `.cache/mini-colmap-ui/runs/*.json` | 界面历史索引；模型保存在任务输出目录 |

输出目录默认被 Git 忽略；仓库中的展示截图与紧凑实验摘要另存于 `docs/assets/`。清理时应同时处理索引与结果，下一次扫描会重新发现仍在 `outputs/` 中的模型。
