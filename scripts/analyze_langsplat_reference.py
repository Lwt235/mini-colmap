"""Collect actual reference/control results without inventing missing stages."""
import csv
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT/'reports/langsplat-reference-20261021'


def read(path):
    return json.loads(path.read_text()) if path.exists() else None


def main():
    rows = []
    query_rows = []
    acceptance = read(REPORT/'acceptance.json')
    accepted = bool(acceptance and acceptance.get('status')=='completed')
    def add(variant,split,kind,summary,queries=None):
        if not summary:
            return
        results = summary.get('results',summary)
        if 'common' not in results or 'official' not in results:
            return
        rows.append(dict(variant=variant,split=split,kind=kind,queries=queries or summary.get('queries',''),
                         common_iou=results['common']['mean_iou'],
                         polygon_peak=results['common']['polygon_peak_rate'],
                         official_iou=results['official']['mean_iou'],
                         bbox_localization=results['official']['bbox_localization_rate']))
    paired = read(REPORT/'paired-protocol-2d/summary.json')
    if paired:
        for name,result in paired['results'].items():
            add(name,'dev','same-2d-maps',result,11)
    add('official-AE-on-old-regions','dev','AE-bundle-control',read(REPORT/'ae-old-data-control/summary.json'),11)
    for variant in ('original-mlp3','repaired-pca224'):
        add(variant,'dev','same-distilled-field',read(REPORT/'paired-protocol-3d'/variant/'summary.json'),11)
    for name,folder in [('matched173',REPORT),('full-view187',REPORT/'full-view-protocol')]:
        for path in sorted((folder/'evaluation').glob('*/summary.json')):
            summary = read(path)
            add(name,summary['split'],path.parent.name.split('-',1)[1],summary)
            for view in summary.get('per_view',[]):
                for query in view.get('per_query',[]):
                    query_rows.append(dict(variant=name,split=summary['split'],stage=path.parent.name.split('-',1)[1],
                        image_id=view['image_id'],query=query['query'],common_iou=query['common']['iou'],
                        official_iou=query['official_iou'],common_level=query['common_level'],official_level=query['official_level']))
        for kind in ('teacher','reconstructed','field'):
            files = [read(folder/'evaluation'/f'{split}-{kind}'/'summary.json') for split in ('dev','test')]
            if all(files):
                n = sum(f['queries'] for f in files)
                joined = {'results':{protocol:{metric:sum(f['results'][protocol][metric]*f['queries'] for f in files)/n
                                                for metric in ('mean_iou','bbox_localization_rate' if protocol=='official' else 'polygon_peak_rate')}
                                       for protocol in ('common','official')}}
                add(name,'all-5-annotated-views',kind,joined,n)
    if rows:
        with (REPORT/'comparison.csv').open('w') as stream:
            writer = csv.DictWriter(stream,fieldnames=list(rows[0]))
            writer.writeheader();writer.writerows(rows)
    if query_rows:
        with (REPORT/'per-query-comparison.csv').open('w') as stream:
            writer = csv.DictWriter(stream,fieldnames=list(query_rows[0]))
            writer.writeheader();writer.writerows(query_rows)
    lines = ['# LangSplat 官方语义参考与差异对照','','## 状态与范围','',
             '本轮六个三维语言场、预算快照评估和两套自定义查询均已完成，验收见acceptance.json。' if accepted else '实验尚在运行，完成状态以acceptance.json为准。',
             '数值来自实际完成的阶段；缺失结果不当作0。进度见工作台“语义学习→官方语义参考”和两个controller-status.json。',
             '共同冻结原RGB、保留用户算法与PCA224模型。仅物理GPU7；官方源d70edb86df0fcbda19dc0d9739a3e5140a5e65fc。',
             'matched173排除语言标注帧和RGB留出图；full-view187允许评测图的SAM/CLIP弱监督，后者不是未见视角测试。',
             '都不使用手工GT拟合。当前参考对齐官方语义组件，并非用同一官方RGB/环境完成论文端到端数值复现。','',
             '## 实际结果','','| 方案 | 划分 | 阶段 | 原口径IoU | 多边形峰值命中 | 官方口径IoU | bbox定位 |',
             '|---|---|---|---:|---:|---:|---:|']
    for row in rows:
        lines.append(f"| {row['variant']} | {row['split']} | {row['kind']} | {row['common_iou']:.4f} | {row['polygon_peak']:.1%} | {row['official_iou']:.4f} | {row['bbox_localization']:.1%} |")
    def get(variant,kind,split='dev'):
        return next((r for r in rows if r['variant']==variant and r['kind']==kind and r['split']==split),None)
    lines += ['', '## 已验证的差异定位','']
    old = get('original-mlp3','same-distilled-field')
    if old:
        lines.append(f"固定旧三维场和全部相关性图，只换评测后处理，IoU从{old['common_iou']:.2%}变为{old['official_iou']:.2%}；因此原7.12%不能直接与论文表格比较。")
    old_ae = get('original-mlp3','same-2d-maps')
    ae_control = get('official-AE-on-old-regions','AE-bundle-control')
    if old_ae and ae_control:
        lines.append(f"固定旧训练区域，整套官方AE替换后，原口径二维IoU从{old_ae['common_iou']:.2%}提高到{ae_control['common_iou']:.2%}。这支持压缩器整套配置有影响，不能单独归因于某一层或损失。")
    old_teacher = get('raw','same-2d-maps')
    teacher,codec = get('matched173','teacher'),get('matched173','reconstructed')
    if old_teacher and teacher:
        lines.append(f"官方多层SAM/裁剪/特征流程的未压缩二维IoU为{teacher['common_iou']:.2%}，旧单层流程为{old_teacher['common_iou']:.2%}；这是预处理和多层整套对照，不是只改层数的消融。")
    if teacher and codec:
        lines.append(f"新流程自身从512维教师到官方3维压缩器，原口径IoU由{teacher['common_iou']:.2%}降至{codec['common_iou']:.2%}；压缩损失仍然存在。")
    field = get('matched173','field')
    if field and codec:
        lines.append(f"相同留出协议下，官方3维三维场dev IoU为{field['common_iou']:.2%}（官方口径{field['official_iou']:.2%}）；对应二维解码目标为{codec['common_iou']:.2%}，两者差异反映三维拟合、跨视角一致性和固定几何等共同影响。")
        worst = sorted((q for q in query_rows if q['variant']=='matched173' and q['split']=='dev' and q['stage']=='field'),key=lambda q:q['official_iou'])[:3]
        for q in worst:
            targets = [next((r for r in query_rows if r['variant']=='matched173' and r['split']=='dev' and
                       r['stage']==stage and r['image_id']==q['image_id'] and r['query']==q['query']),None)
                       for stage in ('teacher','reconstructed')]
            if all(targets):
                lines.append(f"困难查询 image_id={q['image_id']}、{q['query']}：官方代码口径二维教师 / 二维解码 / 三维场 IoU = "+
                             ' / '.join(f"{r['official_iou']:.2%}" for r in [*targets,q])+'；这不是单因素归因，逐查询完整数值见per-query-comparison.csv。')
    for variant in ('matched173','full-view187'):
        budgets = [get(variant,kind) for kind in ('field-i3000','field-i7000','field')]
        if all(budgets):
            lines.append(f"{variant}同轨迹3000/7000/30000步，原口径IoU分别为"+' / '.join(f"{r['common_iou']:.2%}" for r in budgets)+'；官方口径分别为'+' / '.join(f"{r['official_iou']:.2%}" for r in budgets)+'。最后模型仍固定采用30000步，未按test选择。')
    all_match = get('matched173','field','all-5-annotated-views')
    all_full = get('full-view187','field','all-5-annotated-views')
    if all_match and all_full:
        lines.append(f"同5张标注帧、官方代码口径：留出协议{all_match['official_iou']:.2%}，全视角弱监督协议{all_full['official_iou']:.2%}；协议同时更改AE与语言场可见图像集合，不能归因于单一模块。")
    lines += ['', '论文Table 2的waldo_kitchen语义分割IoU为44.5%，3D-OVS的90%级结果来自不同场景和协议；本实验保留当前RGB与环境，数值接近也不等于完成论文复现。',
              '论文与发布代码本身也有差异：正文AE写L1+cos，代码用MSE+0.001*cos(dim=0)；附录LERF均值滤波20，固定代码为30。本轮严格按固定代码运行。',
              '来源：[LangSplat论文v2](https://arxiv.org/html/2312.16084v2)、[固定官方源](https://github.com/minghanqin/LangSplat/tree/d70edb86df0fcbda19dc0d9739a3e5140a5e65fc)。']
    lines += ['', '## 如何解释','','同图协议对照固定网络和相关性图，只更换评测后处理/定位/多边形舍入口径。',
              'AE-on-old-regions固定原29357个有效训练区域及dev区域图，整套替换官方网络、单位化、损失和100轮训练；',
              '这不是单独BN、单独权重或单独归一化的因果消融，CPU随机训练与GPU也不是逐步同轨迹。',
              '3000/7000/30000步使用同一次训练的快照；两个视角协议同时更改AE与语言场可见图像集合。',
              '原口径多层按最大原始响应选层；官方按平滑响应选层并归一化分割，定位按bbox。不得混合汇总。',
              'seed0单次结果，dev很小；不能把小差值当统计显著改进或跨场景结论。',
              '主模型固定官方30000轮方案；中间快照只用于预算分析，不使用test选步数。','',
              '## 代码和验证','','官方训练函数、GaussianModel/Adam、AE类和语义CUDA核未编辑。',
              '工程适配包含：相机/RGB世界坐标转换、目标图预加载、等价SAM后处理/NMS加速、GPU驻留epoch采样、日志和指标绘图关闭。',
              'SAM真实批次全部字段精确对照、NMS/多数滤波/单层兼容/无GT查询掩码对照已通过。',
              '投影与opacity诊断在geometry-probe.json；它检查覆盖，不证明几何GT准确性。',
              '源/子模块/库哈希见source-manifest.json，各模型完成标记记录RGB/AE绑定及冻结校验。','',
              '## 操作','','文档：docs/semantics.md；远程访问配置见docs/linux-deployment.md。参考结果单独展示。',
              '文件：data/langsplat_reference/、data/langsplat_reference_full_views/；当前报告内evaluation/及full-view-protocol/evaluation/。',
              'query支持真实相机ID和任意文本，例：',
              '先确认设备空闲，并设置GPU为选定物理编号；从项目根目录执行：',
              '```bash','CUDA_VISIBLE_DEVICES="$GPU" OMP_NUM_THREADS=4 .venv/bin/python scripts/langsplat_reference.py query --image-id 99 --text "spice jar"',
              '```',('本轮已通过完整验收，可以直接查询。' if accepted else '完整训练结束后再运行。')+'没有GT的查询只保存相关性图与官方后处理掩码。']
    (REPORT/'README.md').write_text('\n'.join(lines)+'\n')
    (REPORT/'comparison.json').write_text(json.dumps(rows,indent=2))
    if Path('/tmp/langsplat-build.log').exists():
        shutil.copyfile('/tmp/langsplat-build.log',REPORT/'native-build.log')
    print(json.dumps({'actual_result_rows':len(rows),'report':str(REPORT/'README.md')}))


if __name__=='__main__':
    main()
