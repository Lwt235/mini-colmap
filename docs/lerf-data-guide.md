# LERF-OVS 数据指南

LERF-OVS 用于多物体场景的 RGB 3DGS 与语言检索。照片、COLMAP 模型与标注需从 [LangSplat 仓库](https://github.com/minghanqin/LangSplat) 提供的 [扩展数据包](https://drive.google.com/file/d/1QF1Po5p5DwTjFHu6tnTeYs_G0egMVmHt/view) 单独获取，使用时遵循原数据的条款。

## 下载与准备

在 Linux 项目根目录执行；准备脚本的工作范围为 CPU 数据读取与转换：

```bash
.venv/bin/python -m pip install gdown==5.2.0
mkdir -p .cache/datasets
.venv/bin/python -m gdown \
  'https://drive.google.com/uc?id=1QF1Po5p5DwTjFHu6tnTeYs_G0egMVmHt' \
  -O .cache/datasets/lerf_ovs.zip --no-cookies
.venv/bin/python scripts/prepare_lerf.py --archive .cache/datasets/lerf_ovs.zip
```

脚本拒绝覆盖非空的解压目录、数据目录与模型目录；完成后不要在原目录重复准备。原压缩包与解压内容保存在 `.cache/datasets/`，输入照片复制到 `data/lerf_ovs/<场景>/`，官方模型转为 `outputs/lerf_<场景>_official_colmap/model.json`。

历史观测的压缩包为 **745,585,683 字节**，SHA256 为 `0cb250fce178e4e132faf56c9de00e058094ae36cf8d2b6deac0558b5e59374d`。字节数与哈希的来源为本地下载记录。新下载应核对来源与内容；脚本在 `.cache/datasets/lerf-preparation.json` 保存实际结果，每场景的 `provenance.json` 保存逐图 SHA256 和转换说明。

已准备版本的计数如下，图片数按官方注册帧计：

| 场景 | 照片/位姿 | 官方稀疏点 | 标注帧 |
| --- | ---: | ---: | ---: |
| figurines | 299 | 50,517 | 4 |
| ramen | 131 | 29,746 | 7 |
| teatime | 177 | 25,503 | 6 |
| waldo_kitchen | 187 | 17,475 | 5 |
| 合计 | 794 | 123,241 | 22 |

标注原件在解压包的 `label/<场景>/` 下，工作台的 `semantic_annotations.json` 保存图片 ID、路径和类别索引。人工多边形用于语义评测；SAM/CLIP 特征由独立的语义提取阶段生成。

## 选择输入

| 生成的清单 | 内容 | 用途 |
| --- | --- | --- |
| `dataset_12.json` | 前 12 张照片及内参，无位姿 | 小规模自行 SfM；仅覆盖局部视角 |
| `dataset_all.json` | 全部已注册照片及内参，无位姿 | 自行 SfM，或配合导入的官方稀疏模型训练 3DGS |
| `dataset_posed_all.json` | 同一批照片、内参、官方世界到相机位姿 | 直接训练 NeRF；不含 3DGS 初始化点云 |

导入的模型是**官方数据包中的 COLMAP 估计几何**，可直接用于训练 3DGS。比较自己的 SfM 时，单独保存自行恢复的模型及对应数据清单；使用官方位姿的结果按官方几何实验记录。

## 训练与查询

1. 在工作台打开导入的官方历史，检查照片、相机和点云能否对应。
2. 显式选择空闲物理 GPU，先导入 [冒烟配置](../configs/3dgs_linux_smoke.json)：300 步、下采样 4、最多 20,000 高斯。它仅检查数据与训练链路。
3. 链路正常后改用 [LERF 配置](../configs/3dgs_lerf.json)：下采样 2、10,000 步、最多 100,000 高斯。按场景分别保存配置，并检查该预算与参数在目标场景的效果。
4. 训练完成、释放计算任务后，开启实时渲染。RGB 模型足够稳定后再按 [语义指南](semantics.md) 提取特征、训练语言场与查询；语言索引由语义流程另行生成。

CLI 等价用法（`1` 仅为物理 GPU 编号示例，需先确认空闲）：

```bash
CUDA_VISIBLE_DEVICES=1 .venv/bin/python -m minicolmap train \
  --method 3dgs \
  --model outputs/lerf_figurines_official_colmap/model.json \
  --dataset data/lerf_ovs/figurines/dataset_all.json \
  --config configs/3dgs_linux_smoke.json \
  --output outputs/lerf_figurines_rgb_smoke_001
```

## 几何、初始化与评价约定

- 使用包内配套的去畸变 `images/` 和 `sparse/0/`，不要混入 `distorted/sparse/0/` 或 `input/`。导入器支持本数据包使用的受限相机/模型格式；其他 COLMAP 格式需先核对支持范围。
- 原始图像字节、ID、三维坐标和 R/t 保持不变。COLMAP 使用半整数像素中心，本项目/OpenCV 使用整数中心；转换时 K 主点及二维观测统一 **减 0.5 px**。官方 LangSplat 输入不能重复转换。
- 每点每图只保留一个观测；若原轨迹重复，固定官方几何后保留重投影误差较小者，详情见生成的 `import_audit.json`；原包保持原样。
- `scene_normalization=camera_extent` 使用训练相机中心和范围，避免远处离群点支配优化尺度。`initial_scale_mode=knn` 使用最多三个近邻的 RMS 距离，并以 `initial_scale` 为上限。旧配置的 `sparse_points/fixed` 仍保留历史行为。
- 如果出现“密度控制将移除全部高斯”，先查 `density_failure.json` 中透明度/尺度/屏幕半径的拒绝数及初始化尺度，再依据拒绝原因修正初始化或密度控制参数。
- 官方几何可能用过 RGB 留出照片，因此评价范围为**固定几何下的外观留出**。RGB PSNR 衡量外观重建；语言检索效果需在单独固定的 train/dev/test、阈值和后处理协议下评测。
