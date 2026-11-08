"""Summarize completed read-only diagnostics; no fitting or threshold selection."""
import json
from pathlib import Path
from minicolmap.semantics.project import write_json
ROOT=Path(__file__).resolve().parents[1];REPORT=ROOT/'reports/semantic-stage-diagnostics-20261022'


def main():
    data=json.loads((REPORT/'results.json').read_text());visibility=json.loads((REPORT/'visibility.json').read_text())
    focus={(r['image_id'],r['query']):r for r in data['focus']}
    findings=[
        dict(query='dark cup',title='杯子：三维阶段明显丢失目标覆盖，同时部分训练视角有压缩退化',
            detail='相机 124 的二维原始 / 压缩重建 / 三维 IoU 为 86.3% / 86.6% / 43.7%，三维覆盖率仅 49.1%。相机 118 的原始→重建重投影 IoU 由 75.6% 降至 29.3%，说明部分上游目标也不稳定。不能把原因全部归给同一个模块。',
            next_step='先检查目标区域的多视角监督冲突、可见性与三维拟合残差；保留不可靠视角的诊断，不直接增加训练步数。'),
        dict(query='knife',title='刀具：标注视角较好，出画视角产生背景激活',
            detail=f'相机 62 的三阶段 IoU 为 68.7% / 82.1% / 76.2%。相机 63 中，源标注可用表面的 {visibility[0]["in_frame_fraction"]:.2%} 投影在画内，但二维查询仍选出 319,171 个像素；这种视角不能当作普通目标正视角。',
            next_step='优先增加基于几何的目标入画检查，并检验缺失目标时的伪激活抑制；先做隔离实验，不降低当前选区发布门槛。'),
        dict(query='sink',title='水槽：作为工作正常的对照',
            detail='相机 124 的三阶段 IoU 为 68.8% / 86.3% / 88.3%。当前三维流程可以保持该目标质量，问题具有物体与视角依赖性。',
            next_step='保留为回归对照，后续改善杯子和刀具时同时检查水槽是否退步。')]
    write_json(REPORT/'findings.json',findings)
    lines=['# 语义错误逐层归因（2026-10-22）','','## 范围与方法','',
        '固定当前 raw3 三维语言场、同一编解码器、相同文本/负词、原图网格及后处理。只使用 GPU 7，无训练、无阈值搜索，无 Test 人工标签。',
        '两张已重复使用的 Dev 图像提供 11 个查询；另选 5 个已知问题/对照训练视角，合计 7 张图。不是独立盲测。',
        '原始二维 = 同一套 SAM 区域上的 512 维 CLIP 特征；压缩重建 = 同一区域 512→3→512；三维 = 当前 raw3 原生渲染后解码。',
        '主要使用固定上游流程：30 像素均值平滑、无 GT 的最大平滑响应选层、逐图缩放、阈值 0.4、7 像素多数滤波。原固定阈值口径另列，不混合。',
        '几何与三维学习的影响在 field 阶段合并，不能仅靠阶段差值区分二者。','',
        '## 全部 11 个 Dev 查询','', '| 阶段 | 上游后处理 mIoU | 原固定阈值 mIoU |','|---|---:|---:|']
    names=dict(teacher='原始二维',reconstructed='压缩重建',field='三维渲染',region_pooled='区域平均对照')
    for s,v in data['dev_summary'].items():lines.append(f'| {names[s]} | {v["official"]:.2%} | {v["common"]:.2%} |')
    lines+=['','当前三维指标与已有 raw3 Dev 结果逐值一致，排除了加载错误模型或切换了评测口径这一解释。',
        '总体仍有压缩损失，但不能把全部目标的失败都归给 3 维瓶颈；困难目标需要逐视角诊断。','',
        '## 关键目标逐层对照','', '| 目标 / 相机 | 原始二维 IoU | 压缩重建 IoU | 三维 IoU | 三维精度 | 三维覆盖率 |','|---|---:|---:|---:|---:|---:|']
    for i,q in [(124,'dark cup'),(62,'knife'),(124,'sink')]:
        stages=focus[i,q]['stages'];a,b,c=[stages[s] for s in ('teacher','reconstructed','field')]
        lines.append(f'| {q} / {i} | {a["iou"]:.2%} | {b["iou"]:.2%} | {c["iou"]:.2%} | {c["precision"]:.2%} | {c["coverage"]:.2%} |')
    lines+=['','绿=预测，红边=Dev 人工标注。未标注视角只作视觉检查与几何一致性对照，不报告准确率。','']
    for finding in findings:
        lines += [f'### {finding["title"]}', '',finding['detail'],'',finding['next_step'],'']
    lines+=['## 两个已执行的小对照','','### 固定教师选择的特征层级','',
        '不使用 GT 选层；每张图先用未压缩教师的平滑响应选一个层级，三阶段均使用这个层级。',
        '刀具相机 62 的三维 IoU 从自动选 L2 的 76.25% 提高到教师选 L3 的 84.71%，说明选层会影响结果；杯子在 L3 仍为 43.68%，因此不能靠固定某一层统一解决。',
        '该对照依赖该照片的二维教师信息，只用于诊断，未替换线上策略。','',
        '### SAM 区域内平均三维码再解码','',
        '保持模型、码维度、文本、阈值不变，用已知二维 SAM 区域把三维渲染码替换为区域均值，然后解码。此操作引入二维区域先验，不是纯粹的三维训练消融。',
        'Dev 上游流程 mIoU 从 42.99% 降至 29.69%；刀具从 76.25% 降至 11.91%，杯子从 43.68% 降至 39.73%。不作为正式修复。',
        '杯子跨视角一致性虽有提升，但目标正确性未同步提升；几何一致可以对应持续选错区域。','',
        '## SAM 几何上限（只作诊断）','',
        '使用 Dev GT 计算每层不相交 SAM 区域任意并集的最优 IoU，再取最高层。刀具可到 95.00%，杯子可到 93.19%。这说明标注视角的 SAM 划分具备较高覆盖能力，不能仅归咎于 SAM 无法描出边界。',
        '这是读取 GT 的上限，不是可部署结果，不用于选模型、选层、阈值、拟合或自动定位。','',
        '## 视角与可见性','',
        f'使用新鲜冻结 RGB 原生期望深度，将相机 62 的人工刀具区域有效表面重投影：63 有 {visibility[0]["projected_in_frame"]}/{visibility[0]["source_gt_samples"]} 个样本入画；94 有 {visibility[1]["projected_in_frame"]}/{visibility[1]["source_gt_samples"]} 个样本入画。',
        '入画不等于可见，未测目标视角遮挡；目标视角也没有新增人工标注。期望深度自身可能混合表面。',
        '逐图归一化会在缺失目标时仍把相对最高响应拉高；相机 63 的大片背景激活与目标基本出画同时出现，但尚未单独消融证明归一化是唯一原因。','',
        '## 后续可执行顺序','',
        '1. 先在刀具固定视角上做几何入画/遮挡筛选对照，并测试明确缺失目标时是否仍返回大片掩码；不调低现有选区门槛。',
        '2. 对杯子固定 SAM 层级、目标像素及相机，检查训练目标冲突与前景码残差，再决定是否需要区域加权或更换目标特征。',
        '3. 如需训练，先复制到独立实验目录做小预算单因素对照；同样检查水槽和全部 11 个 Dev 查询，禁止覆盖原检查点。',
        '4. 正式选择只使用 Dev；Test 人工标签继续封存。当前 Dev 已重复用于开发，结论不等价于泛化性能。','',
        '## 文件与复现','',
        '- protocol.json：源模型、编码器、学习代码哈希与固定协议。',
        '- results.json：全部阶段、逐查询指标、邻近视角一致性和固定层级控制。',
        '- visibility.json / visibility-source.json：新鲜原生深度的入画检查。',
        '- *-comparison.jpg：逐层并排可视化；*-maps.npz：私有原始相关性图。',
        '- 原 RGB、编码器、三个语义层检查点及学习者代码哈希均未改变。',
        '首次执行：`CUDA_VISIBLE_DEVICES=7 OMP_NUM_THREADS=4 .venv/bin/python scripts/diagnose_semantic_stages.py`；完成后不覆盖旧结果。',
        '可单独复核可见性：`CUDA_VISIBLE_DEVICES=7 OMP_NUM_THREADS=4 .venv/bin/python scripts/check_semantic_target_visibility.py`；整理报告：`.venv/bin/python scripts/report_semantic_stage_diagnostics.py`。']
    (REPORT/'README.md').write_text('\n'.join(lines)+'\n')


if __name__=='__main__':main()
