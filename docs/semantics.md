# 三维语义特征与文本检索

语义流程在已完成的 RGB 3DGS 上学习语言特征：SAM 自动产生区域，OpenCLIP 编码区域/文本，低维特征经过投影融合或可微蒸馏绑定到高斯。训练固定 RGB、几何、不透明度和高斯顺序，仅学习语言特征。输出包括文本相关性和可见高斯候选选区。

- 用自己的照片从工作台生成索引：见 [新场景流程](new-scene-workflow.md)。
- 用 CLI 比较原始二维特征、压缩误差与三维语言场：按下文配置并逐阶段运行。
- 数据来源、结果与限制：见 [LERF 数据指南](lerf-data-guide.md)、[评测说明](evaluation.md) 和 [实验摘要](../reports/README.md)。

## 模型与环境

以下命令在 **Linux 项目根目录**执行，假设已有 `.venv`、兼容的 PyTorch/torchvision、CUDA 和 gsplat。语义提取/训练/查询依赖 CUDA。基础安装与远程浏览器访问见 [Linux 部署](linux-deployment.md)。当前工作台新场景进程使用 Linux 风格虚拟环境路径；Windows 本机语义任务尚未验证。

```bash
.venv/bin/python -m pip install -r requirements-semantic.txt
.venv/bin/python scripts/setup_semantic_models.py
# 已有权重时只校验，不重新下载
.venv/bin/python scripts/setup_semantic_models.py --verify-only
```

[依赖清单](../requirements-semantic.txt) 固定 OpenCLIP、SAM 源码等版本；[准备脚本](../scripts/setup_semantic_models.py) 需要 `curl` 和同一虚拟环境中的 `hf`，下载约 3.2 GB 权重并校验大小/SHA256，生成本地模型清单。权重不在 Git 中，运行时从本地加载；不需要为每次查询重新下载。

| 模型 | 用途 | 固定来源 |
| --- | --- | --- |
| SAM ViT-H | 自动区域掩码 | [Segment Anything](https://github.com/facebookresearch/segment-anything)，源码 `dca509fe793f601edb92606367a655c15ac00fdf`；Apache-2.0 |
| OpenCLIP ViT-B-16，LAION-2B | 512 维单位区域/文本特征 | [权重仓库](https://huggingface.co/laion/CLIP-ViT-B-16-laion2B-s34B-b88K)，revision `7288da5a0d6f0b51c4a2b27c624837a9236d0112`；MIT |

GPU 计算前检查主机占用并显式选择一张存在的物理卡，例如单卡主机的 `export CUDA_VISIBLE_DEVICES=0`。语义 CLI 采用手动选卡，只接受一个十进制物理编号；列表、UUID 和自动回退均不支持。进程内 `cuda:0` 对应所选物理卡。未显式设置时会读取工作台保存的选择，代码默认值为 7，使用前需确认该编号在主机上存在。

CLI 与工作台共享项目内 GPU 预留；主机上其他程序的占用需另外检查。先关闭实时浏览并等待已有任务结束，依赖调整应在环境空闲时进行；进程清理限于自己启动的任务。

## 配置与数据身份

[基础配置](../configs/semantic_waldo.json) 使用历史 Waldo RGB 路径。将其复制为项目 `configs/` 下的新配置，并替换为目标场景的路径与划分；配置直接放在该目录，因为 CLI 从配置的上两级解析项目根。

以无标注场景的 `configs/semantic_my_scene.json` 为例，保留模板其他字段并设置：

| 字段 | 设置要求 |
| --- | --- |
| `schema_version`、`name` | `2`、自定义场景名 |
| `dataset` | 项目内的照片清单，例如 `data/my_scene/dataset.json` |
| `rgb_run` | 自己完成的 3DGS 输出目录，例如 `outputs/my_scene_gs`；须含合法 SH RGB 检查点及来源模型 |
| `workspace` | 新的派生结果目录，例如 `data/semantic_my_scene`，不与旧实验混用 |
| `annotations` | 项目内 JSON 路径；无标注时文件内容为 `{"frames": []}` |
| `dev_image_ids`、`smoke_image_ids` | `[]`；以及至少一个实际的语义训练 image_id（不含 RGB 验证 ID） |
| `autoencoder` | 复现通用新场景时设置 `codec="pca"`、`latent_dim=32`，不加 MLP 的 `conditioning`/`residual_weight` |
| `extract` | 通用新场景默认 `max_edge=640`、`points_per_side=24`、`crop_n_layers=0` |
| `distill` | 通用新场景默认 `max_edge=512`、`steps=3000`；其余参数可保留模板值 |

`prepare` 从来源模型中的已注册照片创建映射，排除语义标注帧与 RGB 检查点验证帧形成语言训练集。schema v1 用于语义准确率与留出评测，必须提供实际标注及合法 Dev/Smoke ID；schema v2 用于无标注索引，要求标注和 Dev 为空。工作台新场景还要求至少两张 RGB 训练照片。

语言场与 RGB SHA256、高斯顺序、编码器和训练划分绑定；准备阶段也记录照片、清单、来源模型与归一化信息。配置或来源变化时应使用新 workspace，并重新生成匹配的产物。查询使用清单中的真实 image_id；**文件名里的帧数字与该 ID 的对应关系需单独核对**。

## CLI 阶段

以下以已经编辑好的新配置为例；先检查状态，再准备项目：

```bash
CFG=configs/semantic_my_scene.json
.venv/bin/python -m minicolmap semantic doctor --config "$CFG" --verify
.venv/bin/python -m minicolmap semantic status --config "$CFG"
.venv/bin/python -m minicolmap semantic prepare --config "$CFG"
```

`doctor/status` 报告依赖、来源和缓存的可用状态；语义质量通过查询检查或有标注评测另行验证。`--verify` 额外计算本地权重哈希。

```bash
.venv/bin/python -m minicolmap semantic extract --config "$CFG" --split train
.venv/bin/python -m minicolmap semantic regions --config "$CFG" --split train
.venv/bin/python -m minicolmap semantic autoencoder --config "$CFG"
.venv/bin/python -m minicolmap semantic encode --config "$CFG" --split train

# 可选基线；省略时蒸馏从零特征开始，存在时用融合结果初始化
# .venv/bin/python -m minicolmap semantic fuse --config "$CFG"
.venv/bin/python -m minicolmap semantic distill --config "$CFG"

# 1 仅为示例，替换成 project.json 中真实的已注册 image_id
IMAGE_ID=1
.venv/bin/python -m minicolmap semantic query --config "$CFG" --mode distilled --image-id "$IMAGE_ID" --text "red mug"
```

`extract/regions/encode` 支持 `--split`、`--image-id`、`--limit`；显式 ID 优先于 split。可先用 smoke 检查提取与二维查询，但压缩器、融合与蒸馏始终读取完整 train 列表，不会因 `--limit` 自动变为小样本训练。训练型阶段拒绝覆盖已有检查点，不提供优化器状态续训；工作台只复用身份一致且已完成的中间结果。

### 实现与查询模式

核心算法在 [algorithms](../src/minicolmap/semantics/algorithms.py)，阶段调度在 [pipeline](../src/minicolmap/semantics/pipeline.py)，编码器在 [compression](../src/minicolmap/semantics/compression.py)：

| 阶段 | 当前实现 |
| --- | --- |
| 区域组织 | 按质量/面积过滤，重叠时小区域优先；像素图保留原始 SAM 索引，背景为 `-1` |
| MLP 压缩 | 默认 `512→256→128→64→latent` 与反向解码，latent 不强制单位化；损失为 MSE＋平均余弦距离。也支持显式中心化条件 |
| PCA 压缩 | 仅在训练区域特征上中心化并做协方差特征分解；默认新场景保留 32 维，阶段名仍为 `autoencoder`，但不执行 MLP 梯度训练 |
| 特征渲染 | 对合成特征除以 alpha，空覆盖为零，不对低维向量强制单位化 |
| 语言蒸馏 | 冻结 RGB，随机采样训练照片；alpha 达到门限的有效像素上计算 L1，分母为有效像素及通道数 |
| 文本相关性 | 归一化特征/文本，计算正词相对每个负词的二类分数，取最不利负词；零特征返回零 |
| 观测融合 | 中心投影、5% 期望深度门限与加权累积的近似可见性基线 |

查询的 `--mode` 默认为 `2d`，比较时务必显式指定：

| 模式 | 输入与含义 |
| --- | --- |
| `2d` | 原始 512 维区域特征；需先对目标帧 extract/regions |
| `reconstructed` | 区域特征经编码/解码后的二维结果；另需训练好的压缩器 |
| `fusion` | 投影融合得到的三维特征；另需 fuse 检查点 |
| `distilled` | 可微优化得到的三维特征；另需 distill 检查点 |

CLI 查询输出目标相机的热图、阈值掩码、叠加图与数值；工作台新场景在此基础上执行锚点选择、可见性、连通性及大小限制，形成可见高斯候选选区，具体见 [新场景流程](new-scene-workflow.md)。默认负词为 `object/things/stuff/texture`、temperature 为 10、原始阈值为 0.5；分数为未作概率校准的文本相关性。

## 有标注评测

只有配置了真实标注与固定划分的 schema v1 才能使用以下命令。先为 Dev/Test 生成 extract/regions 才能比较二维模式；它们的区域不进入压缩器或语言场训练。

```bash
# 此处 CFG 应指向自己的有标注评测配置
.venv/bin/python -m minicolmap semantic extract --config "$CFG" --split dev
.venv/bin/python -m minicolmap semantic regions --config "$CFG" --split dev
.venv/bin/python -m minicolmap semantic validate-autoencoder --config "$CFG" --split dev
.venv/bin/python -m minicolmap semantic evaluate --config "$CFG" --split dev --mode 2d
.venv/bin/python -m minicolmap semantic evaluate --config "$CFG" --split dev --mode reconstructed
.venv/bin/python -m minicolmap semantic evaluate --config "$CFG" --split dev --mode distilled
# 固定方案后才做最终 Test；2d/reconstructed 需先准备 test 区域
.venv/bin/python -m minicolmap semantic evaluate --config "$CFG" --split test --mode distilled
```

`validate-autoencoder` 报告区域特征重建误差。基础 CLI 的分割与定位评测按原始多边形网格计算固定阈值 IoU、最高响应点落入多边形的定位率，再对 image/query 对平均。

历史 Waldo matched 协议有 173 张语言训练、2 张 Dev、3 张 Test，另留出未标注的 RGB 验证帧。固定 RGB/SfM 曾使用部分语义评测照片，因此评测范围为**固定 RGB 下的语言视图留出**。Dev 是反复使用的开发集。当前评测采用单场景、单 seed，展示案例经过筛选。

## 与 LangSplat 参考路径的区别

项目提供单层流程及调用 [LangSplat 上游](https://github.com/minghanqin/LangSplat) 组件的独立参考；两条路径分别定义损失、特征和指标。

| 路径 | 区域 / 压缩 | 三维训练与用途 |
| --- | --- | --- |
| 基础 CLI | 单层 SAM；模板默认 MLP 3 维，可改 PCA 或其他维度 | 项目 gsplat 适配；alpha 后除法和有效像素 L1；比较二维、压缩、融合、蒸馏各阶段 |
| 工作台新场景 | 单层 SAM；PCA32 | 3,000 步冻结 RGB 蒸馏；生成无标签候选，精度尚未验证 |
| 官方组件参考 | 官方四层预处理；带 BatchNorm、编码/解码单位化的 3 维 AE | 独立原生语义光栅；s/m/l 三个语言场各 30,000 步，原 alpha 合成、不加 alpha 后除法，掩码置零后整图平均损失 |
| Waldo raw3 | 复用官方多层特征；选择非单位化 3 维 codec | 同时去掉渲染前每高斯语言特征单位化；在官方配方上组合了 codec 与渲染改动，独立于基础 CLI/PCA32 |

官方参考固定 LangSplat commit `d70edb86df0fcbda19dc0d9739a3e5140a5e65fc`，语义光栅子模块 `329f8e8c64a56f3f018397faa4a4996cbf024de0`，多层 SAM 子模块 `e5dbe4b5616e24f02f15ce5a439a5edf228b3a75`。AE 采用发布代码的 MSE＋`0.001×cosine_loss(dim=0)`；基础 CLI 的损失见上表。参考复用当前项目冻结 RGB 与相机适配，实验范围为固定 RGB 上的语义组件训练与评测。

参考入口为 [langsplat_reference](../scripts/langsplat_reference.py)，raw3 入口为 [langsplat_raw3](../scripts/langsplat_raw3.py)。它们使用**固定 Waldo 实验路径**，配置独立于上面的通用 `CFG`。需要另行准备匹配 RGB、数据、上游检出及独立编译的原生库；缓存源码、编译库和已训练参考模型作为 Git 仓库之外的本地产物保存。参考环境隔离于项目缓存，与工作台 gsplat 分开管理。重新实验前应设置新的工作/报告路径并记录来源，历史模型保留在各自路径下。

在对应资源和协议已准备完成后，阶段入口如下；训练 s/m/l 时分别取 level 1/2/3：

```bash
# 先显式设置已检查可用的 CUDA_VISIBLE_DEVICES
.venv/bin/python scripts/langsplat_reference.py preprocess
.venv/bin/python scripts/langsplat_reference.py autoencoder
.venv/bin/python scripts/langsplat_reference.py encode
.venv/bin/python scripts/langsplat_reference.py train --level 1
.venv/bin/python scripts/langsplat_reference.py train --level 2
.venv/bin/python scripts/langsplat_reference.py train --level 3
.venv/bin/python scripts/langsplat_reference.py evaluate --kind field --split dev
.venv/bin/python scripts/langsplat_reference.py query --image-id 99 --text "spice jar"
```

`--protocol full-view` 使用全部 187 张图的语言特征，包括评测图，属于语言全视图训练协议。matched 使用 173 张语言训练图，评测范围为固定 RGB 下的语言视图留出。两个协议有独立压缩器和三层模型。

官方发布代码评测还包含 30 像素均值滤波、跨层选择、每图响应归一化、`mask_thresh=0.4` 和多数平滑，定位使用 bbox。这里的 0.4 作用于归一化后的响应；基础 CLI 使用原始相关性阈值与多边形口径。比较 IoU/定位率及归因网络影响时，需先统一阈值、后处理与标注口径。

raw3 的 Dev 反复用于方案选择；当前结果限于单场景、单 seed，部分指标仍未改善。几何一致性、原生渲染恢复与语义正确性分别验证；负面案例与来源摘要统一保留在 [实验记录入口](../reports/README.md)。
