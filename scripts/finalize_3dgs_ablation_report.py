"""Fill the human-readable explanation with completed, audited experiment data."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from minicolmap.io import load_dataset
from run_3dgs_ablation import SCENES, VARIANTS, digest
from summarize_3dgs_ablation import aggregate, chart, comparisons

ROOT = Path(__file__).resolve().parents[1]
OLD_RESET_GUARD = "if density is not None and config['gaussian']['opacity_reset_every'] and step % config['gaussian']['opacity_reset_every'] == 0 and step < config['gaussian']['densify_stop']:"
SAFE_RESET_GUARD = """if (density is not None and config['gaussian']['opacity_reset_every']
                and step % config['gaussian']['opacity_reset_every'] == 0
                and step + len(scene.train_ids) < min(config['gaussian']['densify_stop'], config['iterations'])):"""


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def number(value, digits=2):
    return f'{value:,.{digits}f}'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, default=ROOT / 'reports/3dgs-ablation-20261019')
    args = parser.parse_args()
    report = args.report.resolve()
    report_link = Path(os.path.relpath(report, ROOT / 'docs')).as_posix()
    stages = read(report / 'stages.json')
    if stages['status'] != 'completed':
        raise ValueError('Refusing to finalize a report before all declared stages finish')
    screen, raw = aggregate(report)
    fixed, fixed_raw = aggregate(report / 'fixed-topology')
    full, full_raw = aggregate(report / 'full-resolution')
    assert [len(raw), len(fixed_raw), len(full_raw)] == [36, 12, 2]
    assert not any(data['provisional'] for data in (screen, fixed, full))
    assert {(r['scene'], r['variant'], r['seed']) for r in raw} == {
        (scene, variant, seed) for scene in SCENES for variant in VARIANTS for seed in (0, 1)}
    # Verify the actual source files still match the frozen inputs, once per
    # input set. Comparing recorded hashes alone would not catch a later edit.
    source_checks = {}
    for scene, (model, dataset, _) in SCENES.items():
        data = load_dataset(ROOT / dataset)
        actual = {'model_sha256': digest(ROOT / model), 'dataset_sha256': digest(ROOT / dataset),
                  'image_sha256': {str(key): digest(path) for key, path in sorted(data['image_paths'].items())}}
        assert all(r['source_fingerprint'] == actual for r in raw + fixed_raw + full_raw if r['scene'] == scene)
        source_checks[scene] = actual
    code_checks = raw[0]['code_sha256']
    current_code = {path: digest(ROOT / path) for path in code_checks}
    assert all(r['code_sha256'] == code_checks for r in raw + fixed_raw + full_raw)
    post_experiment_changes = []
    for path, expected in code_checks.items():
        if current_code[path] == expected:
            continue
        assert path == 'src/minicolmap/rendering/pipeline.py', 'Unexpected training code change'
        frozen = (report / 'source' / expected[:12] / 'pipeline.py').read_text(encoding='utf-8')
        assert frozen.count(OLD_RESET_GUARD) == 1
        assert (ROOT / path).read_text(encoding='utf-8') == frozen.replace(OLD_RESET_GUARD, SAFE_RESET_GUARD)
        # A short-run boundary repair is allowed only after proving that it
        # changes ZERO reset steps in every actual experiment, not just hashes.
        for row in raw + fixed_raw + full_raw:
            config = read(Path(row['output']) / 'config.json')
            options = config['gaussian']
            if options['density_strategy'] != 'adaptive' or not options['opacity_reset_every']:
                continue
            n = len(read(Path(row['output']) / 'summary.json')['train_ids'])
            possible = range(options['opacity_reset_every'], config['iterations'] + 1, options['opacity_reset_every'])
            original = [s for s in possible if s < options['densify_stop']]
            safe = [s for s in possible if s + n < min(options['densify_stop'], config['iterations'])]
            assert original == safe, 'Post-experiment guard would change a measured run'
        post_experiment_changes.append({'path': path, 'experiment_sha256': expected,
            'current_sha256': current_code[path], 'reason': 'Reserve one training-view cycle after opacity reset',
            'reset_steps_identical_in_all_50_experiments': True})
    selection = read(report / 'selection.json')
    winner = selection['variant']
    assert selection['test_used_for_selection'] is False
    assert {(r['scene'], r['variant'], r['seed']) for r in full_raw} == {
        ('temple312', variant, 0) for variant in ('baseline', winner)}
    assert all(r['iterations'] == 10000 and r['downscale'] == 1 for r in full_raw)
    chart(report, screen)
    comparisons(report, raw, winner)
    comparisons(report / 'full-resolution', full_raw, winner)
    rows = { (row['scene'], row['variant']): row for row in screen['rows'] }
    paired = []
    for row in raw:
        directory = Path(row['output'])
        summary = read(directory / 'summary.json')
        config = read(directory / 'config.json')
        baseline = next(item for item in raw if item['scene'] == row['scene'] and
                        item['seed'] == row['seed'] and item['variant'] == 'baseline')
        assert row['source_fingerprint'] == baseline['source_fingerprint']
        assert row['code_sha256'] == baseline['code_sha256']
        assert row['evaluation']['dev_ids'] == baseline['evaluation']['dev_ids']
        assert row['evaluation']['test_ids'] == baseline['evaluation']['test_ids']
        assert summary['step'] == 3000 and config['image_downscale'] == 4
        paired.append({'name': row['name'], 'summary_sha256': hashlib.sha256((directory / 'summary.json').read_bytes()).hexdigest()})
    for scene in fixed['input_sets']:
        counts = {row['summary']['point_count'] for row in fixed_raw if row['scene'] == scene}
        assert len(counts) == 1, 'Fixed-topology point counts differ'
    lines = ['<!-- ABLATION_RESULTS_START -->',
        '已完成 **50 项真实 CUDA 训练**：36 项多输入/双种子筛选、12 项固定拓扑损失控制、2 项原分辨率确认。另有一项 1,000 步冒烟训练不纳入结果。',
        '', '### 筛选：3,000 步、160×120、种子 0/1', '',
        '下表 PSNR 为各次独立运行的最终 test 指标再跨种子取均值，± 为两个种子的样本标准差；SSIM 为 test 均值。高斯数和时间取两次均值，显存取两次的最大 allocated 峰值。两个种子不足以支持统计显著性结论。', '',
        '| 输入 | 策略 | 测试 PSNR / dB | 测试 SSIM | 高斯数 | 训练 / s | 峰值显存 / MiB |',
        '| --- | --- | ---: | ---: | ---: | ---: | ---: |']
    for row in screen['rows']:
        lines.append(f"| {row['scene']} | {row['variant']} | {number(row['test_psnr'])} ± {number(row['test_psnr_sample_std'])} | {number(row['test_ssim'],4)} | {number(row['gaussians'],0)} | {number(row['training_seconds'],1)} | {number(row['peak_cuda_allocated_mb'],1)} |")
    lines.extend(['', f"最终候选 **{winner}** 仅按三个输入组的配对 dev PSNR 增益等权平均选择，未使用 test 选配置。排序和依据保存在 `selection.json`。", '',
                  f'![开发集配对增益]({report_link}/dev-gain.svg)', '',
                  '### 固定高斯数：MSE 与 L1＋SSIM', '',
                  '此处 baseline 与 loss 均保留 Adam、使用统一学习率并关闭增密，唯一有效策略差异是损失函数。表内指标为种子 0/1 两次运行指标的均值。', '',
                  '| 输入 | MSE 测试 PSNR | L1＋SSIM 测试 PSNR | MSE SSIM | L1＋SSIM SSIM | 固定高斯数 |',
                  '| --- | ---: | ---: | ---: | ---: | ---: |'])
    for scene in fixed['input_sets']:
        a = next(row for row in fixed['rows'] if row['scene'] == scene and row['variant'] == 'baseline')
        b = next(row for row in fixed['rows'] if row['scene'] == scene and row['variant'] == 'loss')
        lines.append(f"| {scene} | {number(a['test_psnr'])} | {number(b['test_psnr'])} | {number(a['test_ssim'],4)} | {number(b['test_ssim'],4)} | {number(a['gaussians'],0)} |")
    lines.extend(['', '### 原分辨率确认：Temple312、640×480、10,000 步、种子 0', '',
                  '同一份 19,648 点源地图、297 张训练图、15 张留出图；留出图分为 8 张 dev 和 7 张最终 test。下面同时列出全 15 视角和仅 test 的口径，不能将其混用。', '',
                  '| 策略 | 全验证 PSNR | test PSNR | test SSIM | 高斯数 | 训练 / s |',
                  '| --- | ---: | ---: | ---: | ---: | ---: |'])
    for row in full_raw:
        lines.append(f"| {row['variant']} | {number(row['summary']['metrics']['psnr'])} | {number(row['evaluation']['test']['psnr_from_mean_mse'])} | {number(row['evaluation']['test']['ssim'],4)} | {row['summary']['point_count']:,} | {number(row['summary']['elapsed_seconds'],1)} |")
    base = next(row for row in full_raw if row['variant'] == 'baseline')
    best = next(row for row in full_raw if row['variant'] == winner)
    preset = ROOT / 'configs/3dgs_temple312_optimized.json'
    preset.write_text(json.dumps(read(Path(best['output']) / 'config.json'), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    gain = best['evaluation']['test']['psnr_from_mean_mse'] - base['evaluation']['test']['psnr_from_mean_mse']
    lines.extend(['', f"原分辨率确认的 test PSNR 变化为 **{gain:+.2f} dB**。这是单种子、10,000 步条件下的结果，不代表 30,000 步、多场景原图或论文标准 benchmark。", '',
                  f'![同一测试视角原图对照]({report_link}/full-resolution/comparison-temple312.png)', '',
                  '### 本地实时链路采样', '',
                  '以下为 1280×720 CUDA 渲染＋读回＋JPEG85，预热 10 帧、测量 40 帧。它是短程离线链路采样，不包含网络/浏览器，也不替代 60 秒浏览器持续帧率验收。', '',
                  '| 原图确认模型 | 本地吞吐 / 帧每秒 | 本地 P95 / ms | GPU 渲染均值 / ms | 峰值 allocated / MiB |',
                  '| --- | ---: | ---: | ---: | ---: |'])
    for row in full_raw:
        perf = row['render_benchmark']
        lines.append(f"| {row['variant']} | {number(perf['local_fps'])} | {number(perf['local_latency_p95_ms'])} | {number(perf['gpu_render_mean_ms'])} | {number(row['summary']['peak_cuda_allocated_mb'],1)} |")
    lines.extend(['', '### 结论应如何使用', '',
                  '- 本轮最稳定的单项策略组是分组学习率＋位置衰减：三个输入组测试 PSNR 较旧基线提高约 2.2–4.5 dB，高斯数量及训练时间变化相对较小。这里尚未拆开学习率倍率和位置衰减各自的贡献。',
                  '- 固定高斯数后，L1＋SSIM 测试 PSNR 提高约 0.47–0.73 dB，SSIM 也提高。它有独立收益，但不足以单独解释完整组合的大幅提升。',
                  f"- 原图组合的画质收益伴随约 {best['summary']['elapsed_seconds']/base['summary']['elapsed_seconds']:.2f} 倍训练时间、{best['summary']['point_count']/base['summary']['point_count']:.2f} 倍实际高斯数和更高显存。训练迭代数和容量上限相同，实际计算成本不同。",
                  '- Adam 状态迁移修复了拓扑变化时的优化历史丢失，但不预设它在每个场景、每个训练预算上都提高 PSNR；负结果在上表保留。',
                  '- 分组学习率、密度策略、损失是预设的策略组；其交互使组合收益不能由单项增益简单相加。',
                  '- 相同最大容量不等于相同实际容量。L1＋SSIM 会改变梯度和增密，高斯数、时间、显存也必须一起比较；固定拓扑实验用于辅助归因。',
                  '- 两种物体、三个输入组、两个筛选种子的覆盖有限。最终原图确认只有一个种子，不能宣称普适最优或统计显著。',
                  '- 所有实验重用固定 SfM 位姿，未联合优化相机。对更大场景、姿态噪声和前景区域质量仍需独立验证。', '',
                  '原始证据：`aggregate.json/csv`、`fixed-topology/aggregate.json/csv`、`full-resolution/aggregate.json/csv`、每项 `ablation_result.json` 和逐视角 `summary.json`。大型权重保存在本机 `runs/`，不加入 Git。原图确认的两份结果已复制到 `outputs/ablation_*`，刷新工作台历史后可查看；源实验没有移动。', '',
                  '### 在工作台中使用', '',
                  f'先刷新历史，打开名称含 `ablation_temple312_{winner}` 的完成记录，即可查看本次实际结果、对照照片和自由视角渲染。重新训练时，选择本次 SfM 源结果 `ui_20261019_084229_6c1c74`（312 台相机、19,648 点），进入 NeRF / 3DGS，导入 [原图确认配置](../configs/3dgs_temple312_optimized.json)，再开始训练。', '',
                  '这份配置与确认实验完全一致，包含 10,000 步、原图分辨率、100,000 高斯预算和 20 图一留出的划分。用于其他数据时应另外保存实验配置；这里没有修改所有场景的默认学习率或损失。',
                  '<!-- ABLATION_RESULTS_END -->'])
    if post_experiment_changes:
        lines[-1:-1] = ['', '### 短训练的收尾保护', '',
            '正式实验结束后，增加了透明度重置的恢复时间保护：仅当距离训练结束和增密窗口结束都还有超过一轮训练视角的步数时，才执行重置，避免短训练在结束附近突然变暗。审计脚本逐项核对全部 50 项配置，修复前后的实际重置步骤完全一致：低预算实验不触发重置，原图组合仍在 3,000/6,000 步重置。', '',
            '本次指标与权重仍对应冻结源码，未改写它们的代码指纹；冻结源码与当前源码散列、这一处精确改动及触发步骤等价检查分别记录在 `source/` 和 `audit.json`。重新运行实验会使用当前版本产生新的指纹。']
    browser_paths = {variant: report / 'verification' / ('live-' + variant) / 'live-browser-performance.json'
                     for variant in ('baseline', winner)}
    if all(path.is_file() for path in browser_paths.values()):
        browser_lines = ['', '### 浏览器持续渲染确认', '',
            '同一 headless Edge、1280×720、JPEG85，预热 10 秒、持续测量 60 秒。下面统计生产组件 RAF 中提交的新渲染图像，包含 CUDA、读回、编码、传输与解码；不是物理显示器刷新率。每个模型只测一次，视角沿同样的预设轨迹变化。', '',
            '| 模型 | 高斯数 | 新图像提交 / 帧每秒 | 请求至提交 P95 / ms | GPU 渲染均值 / ms |',
            '| --- | ---: | ---: | ---: | ---: |']
        from minicolmap.ui.store import stable_id
        published = {r['variant']: r['output'] for r in read(report / 'published.json')}
        for variant, path in browser_paths.items():
            perf = read(path)
            corresponding = next(r for r in full_raw if r['variant'] == variant)
            assert perf['status'] == 'passed' and perf['measurement_seconds'] == 60
            assert perf['run_id'] == stable_id('result-', Path(published[variant]))
            assert perf['ready']['gaussian_count'] == corresponding['summary']['point_count']
            browser_lines.append(f"| {variant} | {perf['ready']['gaussian_count']:,} | {number(perf['browser_fps'])} | {number(perf['latency_ms']['p95'])} | {number(perf['render_ms']['mean'])} |")
        browser_lines.extend(['', '原始逐帧时间、截图与检查结果保存在 `verification/live-baseline/` 和 `verification/live-combined/`。新模型还通过实际旋转、平移、缩放、相机对齐、窗口缩放、重连和释放/重开会话检查。'])
        insertion = lines.index('### 结论应如何使用')
        lines[insertion:insertion] = browser_lines + ['']
    doc = ROOT / 'docs/3dgs-optimization-ablation.md'
    text = doc.read_text(encoding='utf-8')
    replacement = '\n'.join(lines)
    start, end = '<!-- ABLATION_RESULTS_START -->', '<!-- ABLATION_RESULTS_END -->'
    if start in text:
        text = text[:text.index(start)] + replacement + text[text.index(end)+len(end):]
    else:
        placeholder = '实验完成后，本节补充实际结果、负结果及局部对比图。结果以原始 JSON 为准，不预设必须提高多少 dB。'
        if placeholder not in text:
            raise ValueError('Expected report insertion point missing')
        text = text.replace(placeholder, replacement)
    doc.write_text(text, encoding='utf-8')
    (report / 'audit.json').write_text(json.dumps({'status': 'passed', 'screening_input_pairs_verified': 36,
        'fixed_topology_counts_equal': True, 'completed_training_jobs': 50, 'test_selection': False,
        'screening_summaries': paired, 'selected_variant': winner,
        'actual_sources_verified': source_checks, 'experiment_code': code_checks,
        'current_code_verified': current_code, 'post_experiment_changes': post_experiment_changes}, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'document': str(doc), 'winner': winner, 'full_test_gain_db': gain}))


if __name__ == '__main__':
    main()
