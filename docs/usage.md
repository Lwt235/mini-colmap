# 安装与使用

## 1. 基础环境

需要 Python ≥3.10；可参考 Windows Python 3.12 或 Linux Python 3.11 环境。CPU 可运行 SfM、工作台和小规模 PyTorch 参考渲染；GPU 训练、连续 3DGS 浏览和语义索引需要额外依赖。Linux CUDA 环境及跨机器访问见 [Linux 部署与 SSH 隧道](linux-deployment.md)。

在项目根目录创建并激活虚拟环境：

```bash
# Linux
python3 -m venv .venv
source .venv/bin/activate
```

```powershell
# Windows PowerShell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

以下命令中的 `python` 均指项目虚拟环境；也可直接使用 Linux 的 `.venv/bin/python` 或 Windows 的 `.venv\Scripts\python.exe`，无需激活脚本。

```bash
python -m pip install -e ".[dev,ui]"
python -m minicolmap doctor
```

`doctor` 检查基础依赖的导入状态，重建与渲染质量通过实际运行检查。SfM CLI 使用基础依赖；小规模参考训练需安装 `python -m pip install -e ".[render]"`。外部照片、预训练权重、训练输出和大批原始实验记录在本地单独管理。

## 2. 准备照片

### TempleRing：可下载的小型入口

```bash
python scripts/prepare_middlebury.py
```

[准备脚本](../scripts/prepare_middlebury.py) 下载 Middlebury TempleRing，校验固定 SHA256、尺寸与标定，生成 `data/middlebury_temple_ring/`。目录已有内容时拒绝覆盖。默认清单是连续拍摄弧段的 8 图；`dataset_all.json` 是完整 47 图，另有 5/20 图和文件序前 8 图清单。

SfM 输入只包含照片和官方 K；参考 R/t 单独保存，不作为估计位姿输入。照片已由发布方去畸变，不要再次处理或把倒置照片自行转正。

### 自己的数据

复制 [输入示例](../examples/dataset.example.json)，将照片放在清单目录内。填写实际尺寸及**去畸变后**对应的 K，不沿用示例焦距；图像路径相对清单所在目录，不能越界。需要静态场景、充分重叠与视差。缩放、裁剪或旋转照片后必须同步修改相机参数，字段见 [配置与数据格式](configuration.md)。

工作台也可上传含清单的目录，或选择照片并填写共享 K。请在重建前通过标定或可靠来源取得内参。

### 完整 Dino / Temple 与 LERF

从 [Middlebury 数据页](https://vision.middlebury.edu/mview/data/) 获取完整 Dino 363 图或 Temple 312 图数据包，按下表准备：

| 数据 | 本地原件布局 | 准备命令 | 常用清单 |
| --- | --- | --- | --- |
| Dino 363 图 | `data/dino/images/dino0001.png`…`dino0363.png`；`reference/` 下放 `dino_par.txt`、`dino_ang.txt`、`README.txt` | `python scripts/prepare_dino.py` | 默认 8 图、20 图、首采集带 49 图、完整 363 图 |
| Temple 312 图 | `data/temple/images/`；`reference/` 下放 `temple_par.txt`、`temple_ang.txt`、`README.txt` | `python scripts/prepare_temple.py` | 默认 8 图、连续 19 图、首采集带 27 图、完整 312 图 |

[完整 Dino 脚本](../scripts/prepare_dino.py) 与 [完整 Temple 脚本](../scripts/prepare_temple.py) 保留原像素和标定原件，记录来源与指纹。Dino 派生位姿将近似旋转投影到合法旋转并保留相机中心。`dataset_all.json` 供 SfM；`dataset_posed_all.json` 等显式带官方位姿的清单可直接训练 NeRF，但没有稀疏点，不能单独初始化当前 3DGS。

建议从连续小子集开始。当前穷举 `N(N-1)/2` 图像对，完整集代价明显增加，也可能只部分注册。Dino 可参考 [SfM 配置](../configs/dino_sfm.json)。LERF/3D-OVS 数据、官方 COLMAP 几何与受限导入说明见 [LERF 数据指南](lerf-data-guide.md)。

## 3. CLI：重建、检查与训练

```bash
python -m minicolmap run --dataset data/middlebury_temple_ring/dataset_all.json --output outputs/temple47 --config configs/temple_ring_no_ba.json
python -m minicolmap inspect outputs/temple47/model.json
python -m minicolmap export outputs/temple47/model.json --output outputs/temple47_export
```

首次检查可将第一条的输入改成默认 8 图清单，并另取输出目录。重建/训练使用新的输出目录，不覆盖已有结果；导出目录必须为空。SfM 可输出 JSON、PLY 和 COLMAP TXT，但不提供稠密表面。

安装 PyTorch 后可先用低预算 CPU 配置检查训练和渲染流程，画质验收使用完整训练配置：

```bash
python -m minicolmap train --method 3dgs --model outputs/temple47/model.json --dataset data/middlebury_temple_ring/dataset_all.json --output outputs/temple_gs_smoke --config configs/3dgs_smoke.json
python -m minicolmap render --run outputs/temple_gs_smoke --image-id 4
```

`--image-id` 使用模型中已注册图像的 ID。示例中的 `4` 请按 `inspect` 列出的可用相机替换。

完成下一节的 GPU 环境与显卡选择后，可使用 CUDA 配置：

```bash
# NeRF：复用 SfM 位姿；也可不传 --model，改用含 K/R/t 的照片清单
python -m minicolmap train --method nerf --model outputs/temple47/model.json --dataset data/middlebury_temple_ring/dataset_all.json --output outputs/temple_nerf --config configs/nerf_temple_quality.json

# 3DGS：需要已注册相机、有效稀疏点和对应原图
python -m minicolmap train --method 3dgs --model outputs/temple47/model.json --dataset data/middlebury_temple_ring/dataset_all.json --output outputs/temple_gs --config configs/3dgs_gsplat.json
```

配置模板供新任务调整使用，复现实验时请核对对应的历史配置。[TempleRing NeRF 配置](../configs/nerf_temple_quality.json) 的 near/far 需按当前场景设置；[3DGS CUDA 配置](../configs/3dgs_gsplat.json) 设置高斯容量上限，实际点数由初始化与密度控制决定。参数见 [配置说明](configuration.md)，协议与历史测量见 [评测说明](evaluation.md) 和 [实验摘要](../reports/README.md)。

`train --checkpoint <已有3DGS检查点>` 提供检查点微调入口：来源与配置匹配后载入高斯参数，并新建 Adam 状态。该入口适用于 3DGS。

## 4. GPU 与光栅后端

### 选择物理卡

启动 GPU 计算前检查 `nvidia-smi`，选择有可用资源的物理卡，并为已有任务保留资源。下面的单卡主机示例使用物理编号 `0`，请按实际设备替换：

```bash
# Linux；仅影响此终端启动的进程
export CUDA_VISIBLE_DEVICES=0
```

```powershell
# Windows PowerShell
$env:CUDA_VISIBLE_DEVICES = "0"
```

只暴露一张卡后，进程内的 `cuda:0` 指这张物理卡。终端变量控制该终端启动的进程，**工作台计算子进程的设备由页头保存的显卡选择决定**。代码默认物理编号为 7，首次使用请显式选择本机可用卡。所选卡不可用时任务报错，活动任务或实时会话期间设备选择锁定。

### 安装与检查

Linux 按 [部署指南](linux-deployment.md) 选择兼容的 PyTorch、torchvision、CUDA 和 gsplat。Windows 可参考已固定的 Python 3.12 / PyTorch 2.7.1+cu128 组合：

```powershell
python -m pip install -r requirements-render-gpu.txt
python -m pip install -e ".[render]"
python scripts/check_render_gpu.py --output .cache/render-gpu-check.json
```

[CUDA 自检](../scripts/check_render_gpu.py) 通过矩阵计算、反向传播和参数更新检查计算环境；图像质量需通过训练后的渲染结果验收。依赖升级应在空闲环境中进行。

NeRF 与 PyTorch 高斯参考后端不依赖 gsplat。3DGS CUDA 后端和连续浏览还需 [gsplat 依赖](../requirements-gsplat.txt) 及匹配的原生扩展；Windows 本地编译需 MSVC C++ 工具和兼容 CUDA Toolkit：

```powershell
python -m pip install -r requirements-gsplat.txt
python scripts/setup_gsplat_cuda.py
& .\scripts\with_gsplat_env.ps1 -PythonArgs @('scripts/build_gsplat_extension.py')
python scripts/check_gsplat_backend.py
```

扩展与校验信息保存于项目缓存；更换 Python/PyTorch/CUDA/gsplat 后重新构建和检查。设备与后端独立配置，使用 gsplat 时需同时选择 `device=cuda` 和 `gaussian.backend=gsplat`。CUDA 检查会在所选卡上执行计算。

## 5. 工作台与演示

```bash
python -m minicolmap ui
# 自定义端口，不自动打开浏览器
python -m minicolmap ui --port 8766 --no-browser
```

默认地址为 `http://127.0.0.1:8765`，只监听本机；远程访问用 SSH 隧道，不直接向公网暴露。可用 `--project <项目目录>` 指定包含数据和输出的根目录。生产前端资源随项目提供，日常使用无需 Node/CDN。

首页为场景浏览与语义演示；右上角 **重建与训练** 或 `#workspace` 进入工作区。厨房示例需要另行准备照片、RGB/语义权重及已生成选区；首次使用可先从自己的重建流程开始。

### 重建与训练

1. 选择或导入照片，在参数页检查内参、特征、RANSAC、初始化、建图与 BA 配置；支持 JSON 导入/导出。
2. 开始 SfM，查看真实进度、耗时、日志与阶段快照。点选相机/点可检查 K/R/t、轨迹、关键点和重投影。
3. 左键旋转、右键平移、滚轮缩放；可对齐至估计相机、切换快照或按误差着色。显示筛选不改导出模型。
4. 进入 **NeRF / 3DGS**：NeRF 可选含位姿的照片集，3DGS 必须关联点云和原图。检查设备、图像降采样、迭代、采样/高斯预算及后端。
5. 训练时查看 loss、验证 PSNR 和实际渲染预览，完成后按已有相机或自定义相机渲染。3DGS 可开启 CUDA 连续浏览；NeRF 目前是按需渲染，没有相同的实时会话。

普通 3DGS“渲染此相机”使用 CPU 参考路径，连续实时浏览使用 GPU/gsplat，性能应按各自入口测量。独立导入模型可检查几何，照片预览需关联对应原图。

### 语义场景

完成 3DGS 后点击 **用于语义检索**，创建独立场景并生成索引，详见 [新场景流程](new-scene-workflow.md)。在资源齐备的厨房演示中，可输入英文物体名查找，使用高亮、单独查看、恢复和导出；搜索会先关闭实时连接以释放计算资源。证据不足的查询可能被拒绝，结果以本次查询状态与选区为准。

当前文本编码面向英文；中文示例按钮使用对应英文词。ZIP 导出可见高斯表面的选区数据，坐标沿用未标定米制的 SfM 世界单位。方法与协议见 [语义流程](semantics.md)，厨房案例的逐提示验证结果见 [选区记录](../reports/benchmarks/selection.json)；新场景候选的跨视角发布验证尚待完成。

### 任务、GPU 与清理

- 工作台同时运行一个重建/训练任务；语义任务和实时浏览还受项目内共享 GPU 预留约束，启动前需另行检查外部程序的资源占用。
- 页面关闭或切换到演示后，后台任务继续运行，刷新可重新关联。**取消**结束任务进程，并保留已有诊断与快照；3DGS 可使用已保存检查点新建微调任务。
- 页头显卡选择持久化，影响后续训练、语义与实时任务；活动任务/实时会话期间锁定，不自动回退。
- **历史 → 本地任务记录** 的垃圾桶先预览路径与大小，再确认删除。会清理该任务输出、索引、日志和其独立语义场景；保留原始照片及共享数据集。活动进程、实时会话、示例配置依赖或其他任务引用会阻止删除。
- 外部手动删掉输出后，历史刷新只清除失效索引，不主动清理仍存在的失败结果。清理预览后文件改变需刷新；若提示“部分文件待清理”，检查提示中的清理回执与隔离路径，不反复强删共享目录。

## 6. 常见问题与前端开发

| 现象 | 检查方式 |
| --- | --- |
| 初始化失败 / 部分注册 | 核对标定内参，检查连续小子集、真实内点、视差及匹配图 |
| BA 候选接受但未收敛 | 分别检查候选有效性、求解预算和求解器收敛状态 |
| 黑图 / 轮廓模糊 | 检查位姿、归一化、near/far、背景、采样与 RGB 几何，再增加预算 |
| 显存不足 | 减少射线/采样、高斯预算或分辨率；大规模高斯使用 gsplat |
| 高斯数达不到上限 | 上限约束最大容量；实际增长需启用增密并满足梯度等条件 |
| GPU 不存在 / 扩展不兼容 | 核对页头物理编号、终端环境、真实 CUDA 自检与原生库版本 |
| 语义索引失效 | 核对场景、RGB、编码器与语言场的配套关系，按新配置创建独立 workspace |

修改前端时，在 Python 服务之外的终端执行：

```bash
cd web
npm ci
npm run dev
# 检查与生成生产资源
npm test
npm run build
```

Vite 提供开发界面并将 `/api` 代理到本机 8765 的 Python 计算后端；生产界面使用同源接口。[前端构建配置](../web/vite.config.ts) 将产物写入 Python UI 静态目录。修改前端后应重新构建并更新生产资源。
