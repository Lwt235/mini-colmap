"""Report measured results, paired replications, and negative controls."""
from __future__ import annotations

import csv
import json
import os
from pathlib import Path
import statistics

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / 'reports/3dgs-official-ablation-20261020'
MISSING = '待完成'


def read(path):
    return json.loads(path.read_text())


def runs(directory):
    return {row['variant']: (row, path.parent)
            for path in (REPORT / directory).glob('runs/*/result.json')
            if (row := read(path))['status'] == 'complete'}


def parent_for(name):
    return {'official_rasterizer': 'rasterizer_prerequisites',
            'official_reference': 'official_policy_gsplat', 'alpha99_ste': 'alpha99',
            'opacity_reset_rewrap': 'topology_before_adam'}.get(name, 'baseline')


def lpips(directory):
    path = directory / 'lpips.json'
    return read(path)['lpips_vgg_standard'] if path.exists() else None


def fmt(value, digits=3):
    return f'{value:.{digits}f}' if value is not None else MISSING


def avg(values):
    return statistics.mean(values) if values else None


def sd(values):
    return statistics.stdev(values) if len(values) > 1 else None


def paired(groups, name, metric='psnr'):
    parent = parent_for(name)
    return [group[name][0]['metrics'][metric] - group[parent][0]['metrics'][metric]
            for group in groups.values() if name in group and parent in group]


def main():
    global MISSING
    manifest_file = REPORT / 'matrix/manifest-d4-t30000-s0.json'
    if not manifest_file.exists():
        raise RuntimeError('Matrix not started')
    manifest = read(manifest_file)
    policy_path = REPORT / 'verification-policy.json'
    seed0_only = policy_path.exists() and read(policy_path).get('mode') == 'seed0_only'
    MISSING = '未测' if seed0_only else '待完成'
    groups = {0: runs('matrix')}
    if not seed0_only:
        groups.update({1: runs('confirm-seed1'), 2: runs('confirm-seed2')})
    target_count = len(manifest['policies'])*len(groups)
    verification_note = ('按用户最新要求，仅完成seed0，取消seed1/2、重复基线与追加原尺寸训练，'
                         '以释放GPU 7进行Waldo语义训练。全部单次效果只作探索性观察，不填写不存在的标准差。'
                         if seed0_only else '用户已确认全部30组使用seed0/1/2，另加两次同seed0基线。'
                         '三种子完成前，单次结果均为暂定观察。')
    matrix = groups[0]
    failures = []
    for seed, group in groups.items():
        stage = 'matrix' if seed == 0 else f'confirm-seed{seed}'
        for name in manifest['policies']:
            directory = REPORT / f'{stage}/runs/{name}-s{seed}-d4-t30000'
            if name not in group and (directory / 'failure.json').exists():
                log = (directory / 'worker.log').read_text(errors='replace')
                failures.append({'variant': name, 'seed': seed,
                                 'kind': 'CUDA OOM' if 'out of memory' in log.lower() else '需调查的运行错误',
                                 'log': str(directory / 'worker.log')})
    counts = '；'.join(f'seed{seed}：{len(group)}/{len(manifest["policies"])}' for seed, group in groups.items())
    lines = ['# 官方实现差异的实验结果', '', f'完整矩阵成功数：{counts}；运行失败{len(failures)}项。', '',
             '**单次分差不代表稳定收益。** 本场景的近邻尺度下界没有作用于任何初始点，五类初始参数逐位相同，'
             '但该负对照与同seed基线仍相差约0.47dB。这个差值不能归因于尺度下界；'
             '单个差值也不能当作噪声标准差。'+verification_note+
             '见`knn-floor-activation.json`、`verification-policy.json`。', '',
             '固定官方提交54c035f7834b564019656c3e3fcc3646292f727d。统一Waldo Kitchen、178训练/9开发视图、'
             '白背景、30000步；缩图和原尺寸分别比较。方法、源码、输入哈希及全部覆盖见README与各冻结清单。', '',
             ]
    if len(matrix) == len(manifest['policies']):
        best_name, (best, _) = max(matrix.items(), key=lambda item: item[1][0]['metrics']['clipped_psnr'])
        base_psnr = matrix['baseline'][0]['metrics']['clipped_psnr']
        perceptual = [(name, value) for name, (_, directory) in matrix.items()
                      if (value := lpips(directory)) is not None]
        lines.extend(['## 本轮观察与后续使用', '',
                      f'统一截断后的最高PSNR为`{best_name}`：{best["metrics"]["clipped_psnr"]:.4f}dB，'
                      f'比基线{base_psnr:.4f}dB高{best["metrics"]["clipped_psnr"]-base_psnr:+.4f}dB。'
                      '这是已包含组合策略的单次排名，不能当成稳定提升；也未追加原尺寸候选确认。'])
        if perceptual:
            name, value = min(perceptual, key=lambda item: item[1])
            lines.append(f'已评估LPIPS的{len(perceptual)}组中，最低为`{name}`：{value:.4f}；'
                         '不同指标的最佳方案可能不同，应同时查看SSIM和逐图结果。')
        if seed0_only:
            lines.append('继续用冻结原RGB模型进行Waldo语义对照。'
                         '训练顺序和命令见[语义指南](../../docs/semantics.md)；'
                         '本轮不根据单次缩图排名替换语义输入权重。')
        lines.append('')
    lines.extend(['## 等价初始化负对照' if seed0_only else '## 同种子重复与负对照', ''])
    controls = [('baseline', matrix.get('baseline')), ('knn_floor（数学上未激活）', matrix.get('knn_floor'))]
    if not seed0_only:
        controls.extend((f'baseline repeat{i}', runs(f'control-repeat{i}').get('baseline')) for i in (1, 2))
    control_values = []
    lines.extend(['| seed0重训 | PSNR | SSIM | 高斯数 |', '| --- | ---: | ---: | ---: |'])
    for label, item in controls:
        if item:
            row = item[0]
            control_values.append(row['metrics']['psnr'])
            lines.append(f'| {label} | {row["metrics"]["psnr"]:.4f} | '
                         f'{row["metrics"]["ssim_valid"]:.4f} | {row["metrics"]["gaussians"]:,} |')
        else:
            lines.append(f'| {label} | 待完成 | — | — |')
    if len(control_values) > 1:
        lines.extend(['', f'当前n={len(control_values)}，PSNR均值{avg(control_values):.4f}，'
                      f'样本标准差{sd(control_values):.4f}，范围{min(control_values):.4f}–{max(control_values):.4f}dB。'
                      '这是相同seed、等价初始化与训练策略的实现波动描述；未完成的重复不纳入统计。'])
    lines.extend(['', '## 原尺寸官方对照', ''])
    original_file = REPORT / 'reference-original.json'
    original = read(original_file)['summary'] if original_file.exists() else None
    official = runs('native').get('official_reference')
    if original and official:
        row, directory = official
        old_path = REPORT / 'reference-original-lpips.json'
        old_lpips = read(old_path)['lpips_vgg_standard'] if old_path.exists() else None
        delta = row['metrics']['psnr'] - original['metrics']['psnr']
        lines.extend(['| 方案 | PSNR | SSIM | LPIPS↓ | 高斯数 |', '| --- | ---: | ---: | ---: | ---: |',
                      f'| 冻结原模型 | {original["metrics"]["psnr"]:.3f} | {original["metrics"]["ssim"]:.4f} | '
                      f'{fmt(old_lpips,4)} | {original["point_count"]:,} |',
                      f'| 官方核心与共同数据适配 | {row["metrics"]["psnr"]:.3f} | {row["metrics"]["ssim_valid"]:.4f} | '
                      f'{fmt(lpips(directory),4)} | {row["metrics"]["gaussians"]:,} |', '',
                      f'单次seed0对照相差{delta:+.3f}dB，尚不能作稳定优劣或单项贡献结论。'
                      '官方核心使用固定提交的GaussianModel、光栅器和优化方法，配共同的数据/循环适配；'
                      '不冒称未修改的全流程CLI基准。'+
                      ('原计划追加的实际训练入口短程核验已取消。' if seed0_only else '另安排实际train.py.training短程检查。'), '',
                      '原模型复用冻结权重质量指标；旧训练耗时不与新实验器相比。', '',
                      '![原尺寸逐图对照](native-comparison-seed0.png)', ''])
    lines.extend(['## 全部策略seed0配对结果' if seed0_only else '## 全部策略三种子配对结果', '',
                  '每个种子分别用该种子的指定父对照计算差值，再汇总。后端、饱和梯度和透明度参数重置使用表中父对照；'
                  '组合项不能解释成一个单项的收益。'+
                  ('仅保留n=1，SD标为未测，不再等待额外种子。' if seed0_only else
                   'n<3表示尚未完成。±为样本标准差，不是95%置信区间。只有三个训练重复，且比较多项，结果应结合重复基线、原尺寸确认和图像检查解读。')+
                  '另列所有方案统一截断[0,1]后的浮点PSNR差值，防止把输出截断口径误当作训练收益。', '',
                  '| 策略 | 父对照 | n | PSNR均值±SD | ΔdB均值±SD | 统一截断ΔdB±SD | 各种子ΔdB | ΔSSIM均值 | LPIPS均值↓[n] |',
                  '| --- | --- | ---: | --- | --- | --- | --- | ---: | ---: |'])
    flat = []
    for name in manifest['policies']:
        items = [group[name] for group in groups.values() if name in group]
        if not items:
            continue
        psnrs = [row['metrics']['psnr'] for row, _ in items]
        deltas = paired(groups, name)
        clipped = [row['metrics']['clipped_psnr'] for row, _ in items]
        clipped_deltas = paired(groups, name, 'clipped_psnr')
        ssim_deltas = paired(groups, name, 'ssim_valid')
        perceptual = [value for _, directory in items if (value := lpips(directory)) is not None]
        row0 = matrix.get(name, (None, None))[0]
        train_minutes = [row['train_seconds']/60 for row, _ in items]
        points = [row['metrics']['gaussians'] for row, _ in items]
        render_times = [row['render_gpu_ms'] for row, _ in items]
        train_deltas = [(group[name][0]['train_seconds']-group[parent_for(name)][0]['train_seconds'])/60
                        for group in groups.values() if name in group and parent_for(name) in group]
        record = {'variant': name, 'parent': parent_for(name), 'n': len(items), 'paired_n': len(deltas),
                  'psnr_mean': avg(psnrs), 'psnr_sample_sd': sd(psnrs),
                  'delta_db_mean': avg(deltas), 'delta_db_sample_sd': sd(deltas),
                  'clipped_psnr_mean': avg(clipped), 'clipped_psnr_sample_sd': sd(clipped),
                  'clipped_delta_db_mean': avg(clipped_deltas), 'clipped_delta_db_sample_sd': sd(clipped_deltas),
                  'ssim_delta_mean': avg(ssim_deltas), 'lpips_mean': avg(perceptual), 'lpips_n': len(perceptual),
                  'gaussians_mean': avg(points), 'gaussians_sample_sd': sd(points),
                  'train_minutes_mean': avg(train_minutes), 'train_minutes_sample_sd': sd(train_minutes),
                  'train_minutes_paired_delta_mean': avg(train_deltas),
                  'train_minutes_paired_delta_sample_sd': sd(train_deltas),
                  'process_peak_gib_mean': avg([row['peak_allocated_mb']/1024 for row, _ in items]),
                  'reserved_peak_gib_mean': avg([row['peak_reserved_mb']/1024 for row, _ in items]),
                  'render_gpu_ms_mean': avg(render_times), 'render_gpu_ms_sample_sd': sd(render_times),
                  'render_gpu_p95_ms_mean': avg([row['render_gpu_p95_ms'] for row, _ in items]),
                  'seed0_psnr': row0['metrics']['psnr'] if row0 else None}
        for seed, group in groups.items():
            parent = parent_for(name)
            record[f'seed{seed}_delta_db'] = (group[name][0]['metrics']['psnr']-group[parent][0]['metrics']['psnr']
                                             if name in group and parent in group else None)
        flat.append(record)
        lines.append(f'| {name} | {record["parent"]} | {len(deltas)} | {fmt(avg(psnrs))} ± {fmt(sd(psnrs))} | '
                     f'{fmt(avg(deltas))} ± {fmt(sd(deltas))} | '
                     f'{fmt(avg(clipped_deltas))} ± {fmt(sd(clipped_deltas))} | '
                     f'{", ".join(fmt(record[f"seed{seed}_delta_db"]) for seed in groups)} | '
                     f'{fmt(avg(ssim_deltas),4)} | {fmt(avg(perceptual),4)} [{len(perceptual)}] |')
    lines.extend(['', '## seed0单次完整训练明细', '',
                  '下表为原始观察，不能独立当成因果效果。LPIPS使用同尺寸8位PNG与VGG标准[-1,1]输入。'
                  '训练时间排除准备、评估及最终序列化；PyTorch分配峰值含进程内评估/导出，'
                  '不是驱动总占用；另在CSV记录缓存保留峰值和渲染P95。'
                  '延迟包括实验器参数适配，官方模型路径有额外SH拼接；'+
                  ('独立后端计时已取消，不能据此判断光栅器本身的速度。' if seed0_only else
                   '后端前向速度优先看固定权重交叉渲染。')+
                  '微小耗时差不作速度结论。', '',
                  '| 策略 | PSNR | SSIM | LPIPS↓ | 高斯数 | 分钟 | 分配峰值GiB | 渲染ms |',
                  '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |'])
    for name in manifest['policies']:
        if name in matrix:
            row, directory = matrix[name]
            lines.append(f'| {name} | {row["metrics"]["psnr"]:.4f} | {row["metrics"]["ssim_valid"]:.4f} | '
                         f'{fmt(lpips(directory),4)} | {row["metrics"]["gaussians"]:,} | '
                         f'{row["train_seconds"]/60:.2f} | {row["peak_allocated_mb"]/1024:.2f} | {row["render_gpu_ms"]:.2f} |')
    if failures:
        lines.extend(['', '未成功项：', ''])
        lines.extend(f'- {item["variant"]} seed{item["seed"]}：{item["kind"]}；`{item["log"]}`。' for item in failures)
    lines.extend(['', '## 资源与时间', '',
                  'n是实际完成训练数。以下为实验器测量值；标准差描述跨种子变化，包含硬件和运行波动。'
                  '官方模型的SH适配开销如前所述，不能据此对直接光栅器下速度结论。', '',
                  '| 策略 | n | 训练分钟均值±SD | 相对父对照分钟Δ±SD | 高斯数均值 | 分配峰值GiB均值 | CUDA渲染ms均值±SD |',
                  '| --- | ---: | --- | --- | ---: | ---: | --- |'])
    for row in flat:
        lines.append(f'| {row["variant"]} | {row["n"]} | '
                     f'{fmt(row["train_minutes_mean"],2)} ± {fmt(row["train_minutes_sample_sd"],2)} | '
                     f'{fmt(row["train_minutes_paired_delta_mean"],2)} ± {fmt(row["train_minutes_paired_delta_sample_sd"],2)} | '
                     f'{row["gaussians_mean"]:,.0f} | {row["process_peak_gib_mean"]:.2f} | '
                     f'{fmt(row["render_gpu_ms_mean"],2)} ± {fmt(row["render_gpu_ms_sample_sd"],2)} |')
    lines.extend(['', '## 原尺寸同路径确认', ''])
    native_controls = runs('fast-native')
    native_factors = runs('native-factors')
    if native_controls or native_factors:
        base = native_controls.get('baseline')
        lines.extend(['| 策略 | PSNR | 相对同路径基线ΔdB | SSIM | LPIPS↓ |', '| --- | ---: | ---: | ---: | ---: |'])
        for name, (row, directory) in {**native_controls, **native_factors}.items():
            delta = row['metrics']['psnr']-base[0]['metrics']['psnr'] if base else None
            lines.append(f'| {name} | {row["metrics"]["psnr"]:.4f} | {fmt(delta)} | '
                         f'{row["metrics"]["ssim_valid"]:.4f} | {fmt(lpips(directory),4)} |')
        lines.extend(['', '原尺寸仅seed0确认，不能替代原尺寸多种子实验。候选在开发集缩图结果上挑选，详见confirmation-plan.json。'])
    else:
        lines.append('按用户要求取消追加原尺寸训练。' if seed0_only else '尚未完成，不填写不存在的原尺寸同路径结果。')
    lines.extend(['', '## 额外配对核验', '',
                  '以下损失实现比较固定same padding，官方整体比较是组合效应。', '',
                  '| 方案/父对照 | n | ΔdB均值±SD | 统一截断ΔdB均值±SD |', '| --- | ---: | --- | --- |'])
    for name, parent in [('ssim_fused', 'ssim_same'), ('official_reference', 'baseline')]:
        deltas, clipped = [], []
        for group in groups.values():
            if name in group and parent in group:
                deltas.append(group[name][0]['metrics']['psnr']-group[parent][0]['metrics']['psnr'])
                clipped.append(group[name][0]['metrics']['clipped_psnr']-group[parent][0]['metrics']['clipped_psnr'])
        if deltas:
            lines.append(f'| {name}/{parent} | {len(deltas)} | {fmt(avg(deltas))} ± {fmt(sd(deltas))} | '
                         f'{fmt(avg(clipped))} ± {fmt(sd(clipped))} |')
    lines.extend(['', '## 两因素交互', '',
                  '联合−单项A−单项B＋基线，按同种子计算。下列为PSNR尺度描述；非线性PSNR与MSE的交互不同，'
                  '不能把二阶差分当作可直接相加的普适机制贡献。', '',
                  '| 联合项 | n | 二阶差分均值±SD（dB） | 各种子二阶差分 |', '| --- | ---: | --- | --- |'])
    for joint, factor_a, factor_b in [('window_capacity', 'window15000', 'no_capacity'),
                                      ('statistics_grow_prune', 'statistics', 'grow_then_prune'),
                                      ('epsilon_sh', 'adam_epsilon', 'sh_rest_lr')]:
        interactions = []
        for seed, group in groups.items():
            if all(name in group for name in ('baseline', factor_a, factor_b, joint)):
                value = (group[joint][0]['metrics']['psnr']-group[factor_a][0]['metrics']['psnr']-
                         group[factor_b][0]['metrics']['psnr']+group['baseline'][0]['metrics']['psnr'])
                interactions.append((seed, value))
        if interactions:
            values = [value for _, value in interactions]
            lines.append(f'| {joint} | {len(values)} | {fmt(avg(values))} ± {fmt(sd(values))} | '
                         f'{", ".join(f"seed{seed} {value:+.3f}" for seed,value in interactions)} |')
    cross_file = REPORT / 'cross-render/results.json'
    if cross_file.exists():
        lines.extend(['', '## 固定权重交叉渲染', '',
                      '同样9个原尺寸相机，统一RGB截断与世界单位近裁剪.2。隔离前向差异，不替代带梯度的冷启动训练对照。', '',
                      '| 权重/后端 | PSNR | SSIM | CUDA渲染ms |', '| --- | ---: | ---: | ---: | ---: |'])
        for name, row in read(cross_file)['results'].items():
            lines.append(f'| {name} | {row["psnr"]:.4f} | {row["ssim_valid"]:.4f} | {row["render_gpu_ms"]:.3f} |')
    verification = REPORT / 'final-input-verification.json'
    geometry_file = REPORT / 'source-geometry-audit.json'
    if geometry_file.exists():
        geometry = read(geometry_file)
        lines.extend(['', '## 输入身份与稀疏几何自洽性', '',
                      f'图像ID/名称、相机和位姿与已导入的带位姿数据集一致：{all(geometry.get("identity_checks", {}).values())}。'
                      f'{geometry["observations"]:,}次稀疏观测重投影误差：均值{geometry["mean_error_px"]:.3f}、'
                      f'中位数{geometry["median_error_px"]:.3f}、95分位{geometry["p95_error_px"]:.3f}原尺寸像素；'
                      f'负深度观测{geometry["behind_camera"]}。'
                      '这检查坐标/输入自洽性，不能证明相机与真实几何完全准确，也不能把稀疏误差直接换算成PSNR损失。'
                      '详见source-geometry-audit.json；CPU只读，可用audit_3dgs_source_geometry.py复现。'])
    if verification.exists():
        lines.extend(['', f'最终原输入与冻结权重哈希检查：`{json.dumps(read(verification))}`。'])
    lines.extend(['', '## 使用边界', '',
                  '- 全部照片参与共同SfM建模；9张外观留出用于开发，未声明新场景或严格全流程未见视图泛化。',
                  '- 缩图与原尺寸独立比较，不能把分辨率变化当成算法改善。相机相关，9张照片不是9个独立训练重复。',
                  '- 渲染拟合指标不等于真实几何精度；没有Waldo几何真值，也未测语义mIoU。',
                  '- CPU审计已证等价的颜色、相机、SH、协方差保留原实现；未给等价项编造修复收益。',
                  '- 原工作台RGB模型、语义练习和特征绑定均未替换。大检查点在独立runs目录并被Git忽略。',
                  '- 各训练实验均冷启动；重置后的中间验证临时下降不代表最终失败，最终统一30000步。', ''])
    (REPORT / 'FINDINGS.md').write_text('\n'.join(lines))
    if flat:
        with (REPORT / 'factor-effects.csv').open('w') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(flat[0]))
            writer.writeheader()
            writer.writerows(flat)
        os.environ.setdefault('MPLCONFIGDIR', str(ROOT / '.cache/matplotlib'))
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        for clipped in (False, True):
            field = 'clipped_delta_db' if clipped else 'delta_db'
            rows = sorted((row for row in flat if row['variant'] != 'baseline' and row[field+'_mean'] is not None),
                          key=lambda row: row[field+'_mean'])
            if not rows:
                continue
            fig, ax = plt.subplots(figsize=(10, max(4, len(rows)*.30)))
            ax.barh([f'{row["variant"]}'+(' [inactive control]' if row['variant'] == 'knn_floor' else '')+
                     f' (n={row["paired_n"]})' for row in rows],
                    [row[field+'_mean'] for row in rows],
                    xerr=[row[field+'_sample_sd'] or 0 for row in rows], capsize=2,
                    color=['#888888' if row['variant'] == 'knn_floor' else
                           '#3274A1' if row[field+'_mean'] >= 0 else '#C45A54' for row in rows])
            ax.axvline(0, color='#555555', linewidth=.8)
            ax.set_xlabel(('Common clip[0,1]; ' if clipped else 'Raw outputs; ')+
                          'PSNR delta vs specified parent (dB); downscale=4, 30k')
            ax.set_title(f'Development data; {sum(map(len,groups.values()))}/{target_count} completed; '+
                         ('seed0 only, exploratory' if seed0_only else 'provisional when n<3'))
            fig.tight_layout()
            fig.savefig(REPORT / ('factor-effects-clipped.png' if clipped else 'factor-effects.png'), dpi=150)
            plt.close(fig)
    print(f'Wrote {REPORT / "FINDINGS.md"}; {counts}; {len(failures)} failures.')


if __name__ == '__main__':
    main()
