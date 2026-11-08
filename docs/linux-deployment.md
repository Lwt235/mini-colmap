# Linux 部署与 SSH 访问

工作台、照片和模型运行在 Linux GPU 主机；浏览器可在本机，也可通过 SSH 隧道访问。HTTP、SSE 与 WebSocket 共用一个端口，使用生产界面无需 Node。所有命令均从项目根目录运行。

## 安装

基础环境建议 Python 3.11/3.12。安装到项目虚拟环境，不修改系统 Python：

```bash
bash scripts/setup_linux.sh
```

GPU 基线为 PyTorch 2.6.0+cu124、gsplat 1.5.3，依赖清单见 [Linux 依赖](../requirements-linux.txt) 和 [gsplat 依赖](../requirements-gsplat.txt)。需要 NVIDIA 驱动、匹配的 CUDA Toolkit、GCC/G++ 与 Ninja。CPU/UI 安装不要求这些工具。

先确认设备空闲，再显式选卡。以下示例使用物理设备编号 `1` 和 RTX A6000 的计算能力 `8.6`，请按实际显卡调整：

```bash
export CUDA_VISIBLE_DEVICES=1
export TORCH_CUDA_ARCH_LIST=8.6
export MAX_JOBS=2
# 如果脚本不能从 nvcc 推断 CUDA_HOME，再按本机 Toolkit 路径设置它
bash scripts/setup_linux.sh --gpu
```

GPU 安装脚本会执行 CUDA 自检并构建扩展，不应在设备繁忙时执行。扩展缓存位于 `.cache/torch_extensions/`，运行清单为 `.cache/gsplat-runtime.json`。不要复制 Windows 的 `.pyd` 文件到 Linux。

可用 `MINICOLMAP_PYTHON=/path/to/python` 指定创建环境的解释器。`--reuse-torch` 在首次创建环境时启用 `--system-site-packages`，适用于已备妥匹配 PyTorch 的解释器。脚本检测到 torch 版本不匹配时会安装 2.6.0+cu124。已有虚拟环境保持原有隔离方式。

语义索引还需要 torchvision、SAM 和 OpenCLIP，见 [语义指南](semantics.md)。Linux 环境不能直接使用记录 Windows 验证环境的 [依赖快照](../requirements-verified.txt) 或 [cu128 文件](../requirements-render-gpu.txt)。

## 启动

```bash
# 显式传入已确认空闲的物理卡；CPU浏览时也应检查界面选卡再开始计算
bash scripts/start_ui_linux.sh --gpu 1 --port 8765
```

服务只监听 `127.0.0.1:8765`，前台按 Ctrl+C 正常停止。若需断开 SSH 后保留服务，可在自行创建的 tmux 会话中启动，不需要修改监听地址。

启动脚本缺省沿用 `CUDA_VISIBLE_DEVICES`，未设置时使用历史默认值 `7`；可移植部署应始终显式传 `--gpu`。工作台优先使用 `.cache/workbench-gpu.json` 中已保存的显卡选择，启动后请核对右上角“显卡”。有训练、索引、查询或实时会话时禁止切换。每个计算子进程固定使用所选设备，内部 `cuda:0` 对应所选物理卡；设备不可用时任务报错。

## SSH 隧道

在浏览器所在机器执行，将 `user@gpu-host` 换成实际 SSH 地址或自行配置的别名：

```bash
ssh -N -T -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -L 127.0.0.1:18765:127.0.0.1:8765 user@gpu-host
```

Windows PowerShell 也可以使用 [隧道脚本](../scripts/connect_workbench.ps1)：

```powershell
.\scripts\connect_workbench.ps1 -SshHost user@gpu-host -LocalPort 18765 -RemotePort 8765
```

保持隧道终端打开，在本地浏览器访问 **http://127.0.0.1:18765/**。本地端口已占用时换成其他端口；服务器无需开放 8765 入站规则，也不需要单独转发 WebSocket。关闭隧道不会终止服务器上的训练。

浏览器选择的是本机照片，随后上传到服务器；自动发现的数据与训练结果则来自服务器项目目录。照片、预训练权重、检查点和原始特征不会随 Git 克隆分发，需按 [使用指南](usage.md) 和 [LERF 数据指南](lerf-data-guide.md) 准备。

## 检查与排障

```bash
.venv/bin/python -m minicolmap doctor
curl --noproxy '*' http://127.0.0.1:8765/api/bootstrap
```

Windows 检查本地转发：

```powershell
Invoke-RestMethod http://127.0.0.1:18765/api/bootstrap
```

- **连接被拒绝**：先看服务器进程和监听端口，再查 SSH 转发；不要启动第二个服务占用原端口。
- **网页正常但渲染失败**：检查所选物理卡、活动任务、CUDA/gsplat 版本及扩展清单。
- **旧结果照片缺失**：核对数据清单和结果关联路径；跨平台复制的绝对路径可能需要重新关联。
- **前端开发**：使用符合 Vite 要求的 Node 与锁文件执行 `npm ci`；详见 [前端说明](../web/README.md)。不建议为构建修改系统 GLIBC。

可先用 [300 步配置](../configs/3dgs_linux_smoke.json) 检查训练、保存和实时查看，画质验收需使用完整训练配置。已保存的历史实验与性能测量条件见 [评价说明](evaluation.md)。
