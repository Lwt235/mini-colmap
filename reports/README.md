# 紧凑实验记录

此目录公开可核对的结果与评价口径；照片、模型权重、逐帧日志和机器环境快照保存在本地。下列数据均**从已有历史实验提取**。

| 文件 | 内容 |
| --- | --- |
| [3dgs-screening.csv](benchmarks/3dgs-screening.csv) | 三组输入 × 六种策略、每组种子 0/1 的 18 行聚合结果（36 项训练） |
| [3dgs-fixed-topology.csv](benchmarks/3dgs-fixed-topology.csv) | 三组输入 × 两种损失的 6 行聚合结果（12 项训练） |
| [3dgs-full-resolution.csv](benchmarks/3dgs-full-resolution.csv) | Temple312 原图、10,000 步、种子 0 的基线与组合策略（2 项训练） |
| [semantics.json](benchmarks/semantics.json) | 完整 Dev/Test、统一阈值/官方式后处理双口径；编码器参数、173 个语言训练视角和源码/模型指纹 |
| [live-rendering.json](benchmarks/live-rendering.json) | Linux A6000 百万高斯实时链路、数值一致性、聚合时延和受控弱网浏览器对照 |
| [selection.json](benchmarks/selection.json) | 文本到三维选区的固定验收策略、通过/拒绝案例与 Dev 多边形检查 |
| [provenance.json](benchmarks/provenance.json) | 上述文件及本地原始来源的字节数、SHA256 和提取规则 |
| [既有基准元数据](../docs/assets/benchmarks.json) | SfM、NeRF、早期 3DGS 与 Windows 浏览器证据、素材来源与配置 |

三个 CSV 为原聚合表的逐字节副本，包含全部变体及负结果。JSON 的精简项为冗长逐帧数组、进程编号、绝对路径和历史测试计数；评价协议、负结果及影响解释的限制均予保留。原有小型素材与这些证据一起公开，方便交叉检查；源码中的报告输出路径仍可用于新的本地实验。

## 评价范围与测量定义

- 3DGS 筛选覆盖两种物体身份，原图确认使用一个种子。各变体共享训练步数与容量上限，实际高斯数、训练耗时和显存随策略变化，比较时一并报告。见 [消融设计](../docs/3dgs-optimization-ablation.md)。
- 语义 Dev 被开发过程反复查看。统一阈值下 Dev mIoU 下降，官方式 Test bbox 定位率也下降；结果同时包含改善项与退步项。固定 RGB/SfM 可能用到评价照片。见 [语义协议](../docs/semantics.md)。
- WebSocket FPS 统计接收并解码吞吐，浏览器速率统计新图像提交，显示器刷新率由显示设备端测量。80 ms RTT / 8 Mbps 对应受控网络模拟。
- 三维选区验收衡量语言掩膜的多视角自洽性。杯子单视角 IoU 较高，多视角验证失败，验收结果为拒绝。

## 原始记录与复现

完整原始目录及日志保存在原机器的 `reports/` 下，通过忽略规则在版本控制之外管理。公开仓库提供紧凑结果与来源指纹，指纹用于核对文件身份。

重新实验需要另外准备数据、权重、空闲 GPU 与运行配置。入口见 [实验评价](../docs/evaluation.md)、[消融脚本说明](../docs/3dgs-optimization-ablation.md) 和 [语义指南](../docs/semantics.md)。新结果写入新报告目录，历史来源保持原样；由实验脚本重新生成相应聚合表，再按本文件及来源清单的提取规则整理公开证据。
