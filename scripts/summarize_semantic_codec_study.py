"""Verify the completed finite AE study and write numerical conclusions."""
import json
from pathlib import Path
from minicolmap.semantics.project import sha256,write_json

ROOT=Path(__file__).resolve().parents[1]
REPORT=ROOT/'reports/semantic-codec-study-20261021'
WORK=ROOT/'data/semantic_codec_study'


def read(path):
    return json.loads(path.read_text())


def main():
    completed=read(REPORT/'completed.json')
    assert completed['status']=='completed'
    manifest=read(REPORT/'manifest.json')
    selection=read(REPORT/'selection.json')
    dev={s['name']:read(REPORT/s['name']/'dev/summary.json') for s in manifest['variants']}
    for spec in manifest['variants']:
        metadata=read(WORK/spec['name']/'completed.json')
        assert metadata['epochs']==100 and metadata['spec']==spec
        assert metadata['checkpoint_sha256']==sha256(WORK/spec['name']/'best.pth')
    assert sha256(REPORT/'runner-source.py')==manifest['script_sha256']
    assert sha256(ROOT/'.cache/langsplat-official/autoencoder/model.py')==manifest['source_ae_sha256']
    for path,value in {**manifest['preservation_hashes'],**manifest['feature_hashes']}.items():
        assert sha256(Path(path))==value, path
    reference=read(ROOT/'reports/langsplat-reference-20261021/acceptance.json')
    for path,value in reference['original_artifacts_preserved'].items():
        assert sha256(ROOT/path)==value, path
    reproduction=read(REPORT/'baseline-reproduction.json')
    assert reproduction['parameter_tensors_exact']
    assert dev['stock3']['results']==read(REPORT/'existing_reference/dev/summary.json')['results']
    metric=lambda name:dev[name]['results']['official']['mean_iou']
    best=max(manifest['variants'],key=lambda s:metric(s['name']))['name']
    native=max((s for s in manifest['variants'] if s['dim']==3 and s['latent_norm'] and s['output_norm']),
               key=lambda s:metric(s['name']))['name']
    assert best==selection['best_overall'] and native==selection['best_native3']
    tests={name:read(REPORT/name/'test/summary.json') for name in dict.fromkeys(['stock3',native,best])}
    lines=['# 压缩器诊断结论','','## 已完成的验证','',
        '8组模型各100轮，全部物理GPU7；固定173张训练图、39184个四层训练区域、批次顺序和原RGB。',
        'stock3重训逐张量复现已有官方AE，开发集指标相同。源程序、区域/标签文件哈希和原模型均已核验。',
        '本轮为二维SAM区域特征压缩重建后的文本查询评测，尚未重训任何三维语言场。','',
        '## 单因素变化（开发集）','',
        '| 变化 | 相对组 | 官方IoU变化 | 原IoU变化 | 区域余弦变化 |','|---|---|---:|---:|---:|']
    for spec in manifest['variants']:
        parent=spec['parent']
        if parent is None:continue
        child,base=dev[spec['name']],dev[parent]
        delta=lambda p:(child['results'][p]['mean_iou']-base['results'][p]['mean_iou'])*100
        cosine=child['reconstruction']['mean_cosine']-base['reconstruction']['mean_cosine']
        lines.append(f"| {spec['name']} | {parent} | {delta('official'):+.2f}百分点 | {delta('common'):+.2f}百分点 | {cosine:+.4f} |")
    lines+=['','## 锁定候选及测试','',
        f'预先声明按开发集官方IoU排序。总体候选为 **{best}**，保持官方3通道、单位潜码及单位输出约束的候选为 **{native}**。',
        '测试集只在本轮选择锁定后评测；它沿用历史划分，先前已在官方参考研究中报告，不声称全新盲测集。','',
        '| 方案 | split | 原IoU | 官方IoU | 区域余弦 |','|---|---|---:|---:|---:|']
    for name,s in tests.items():
        for split,row in [('dev',dev[name]),('test',s)]:
            lines.append(f"| {name} | {split} | {row['results']['common']['mean_iou']:.2%} | {row['results']['official']['mean_iou']:.2%} | {row['reconstruction']['mean_cosine']:.4f} |")
    lines+=['','## dark cup的变化（开发集image_id=124）','',
        '| 方案 | 原IoU | 官方IoU |','|---|---:|---:|']
    for name,row in dev.items():
        q=next(q for v in row['per_view'] if v['image_id']==124 for q in v['per_query'] if q['query']=='dark cup')
        lines.append(f"| {name} | {q['common']['iou']:.2%} | {q['official_iou']:.2%} |")
    lines+=['','## 怎样解释','',
        '单位化预测与目标满足MSE = 2/512 ×逐区域余弦损失。逐区域余弦权重从0.001改成1基本是在缩放损失，Adam近似抵消该缩放；不能把数值轨迹差异解释成额外监督。',
        '实际输入是FP16提取后拓宽为FP32，范数约0.99954..1.00045，所以上述等价为近似。预检使用精确单位向量验证了恒等式。',
        '取消编码归一化让3维码拥有可用的径向信息，但会改变三维表示约束；它不是无代价的原生3通道替换。',
        '不同容量初始化的参数形状和抽样不同。单种子与小开发集不能给出统计显著性；本轮报告条件化效果，不保证所有对象或新场景都提高。',
        '官方与原口径的阈值、选层和相对归一化不同，某个对象在两种口径中可能一升一降，需同时查看，不能挑有利数字。','',
        '## 现在可执行的使用步骤','',
        '工作台刷新“语义学习→压缩器单因素诊断”，查看表格和dark cup二维重建图。',
        '总体候选的二维压缩查询：','',
        '先确认设备空闲，并设置GPU为选定物理编号；从项目根目录执行：',
        '```bash',f'CUDA_VISIBLE_DEVICES="$GPU" OMP_NUM_THREADS=4 .venv/bin/python scripts/semantic_codec_study.py query --variant {best} --image-id 124 --text "dark cup"','```','',
        '命令打印实际结果目录。原生表示候选可将variant替换为'+native+'。',
        '本轮dark cup重点问题应优先继续验证no_latent_norm：它恢复了该对象的官方IoU，而row_cos001在该对象的官方IoU仍然很低；后者仅是保持原约束的较小改动对照。',
        'no_latent_norm仍是3通道，原生核的数据宽度可保留，但渲染前的每高斯特征单位化需要显式取消。单位向量alpha加权和长度不超过1，无法直接拟合平均长度约3的原始潜码；相关三维变体本轮尚未实现或验证。',
        '新编码器改变了潜空间坐标，已有三维语言场不能直接换用新解码器。下一阶段需先确定表示约束，再以新编码器重新编码训练图并重训对应语言场。',
        '原生CUDA核固定3通道，8/16维候选需适配渲染器；所有原三维模型及查询命令本轮保持原绑定。',
        '该阶段没有完成额外独立CLI查询烟测；已保存的GPU评估、热图和掩码与独立CLI验证分开记录，详见query-smoke-status.json。']
    (REPORT/'findings.md').write_text('\n'.join(lines)+'\n')
    readme=REPORT/'README.md'
    text=readme.read_text()
    if 'findings.md' not in text:
        readme.write_text(text+'\n量化结论、dark cup逐组结果及使用步骤见同目录findings.md。\n')
    write_json(REPORT/'acceptance.json',dict(status='completed',models=8,epochs_each=100,gpu=7,
        baseline_tensor_reproduction=True,inputs_and_original_artifacts_unchanged=True,
        selection=selection,scope='2D compression diagnosis only; existing 3D models not replaced'))
    print(json.dumps({'best_overall':best,'best_native3':native,'report':str(REPORT/'findings.md')},ensure_ascii=False))


if __name__=='__main__':main()
