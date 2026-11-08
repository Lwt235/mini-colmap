"""Summarize actual completed runs; missing/failed jobs remain explicit."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


def summarize(report):
    groups = {}
    for path in sorted(report.glob('runs/*/result.json')):
        result = json.loads(path.read_text())
        groups.setdefault((result['downscale'], result['steps'], result['seed']), {})[result['variant']] = result
    lines = ['# 实际完成的官方对照实验', '',
             'PSNR由9图平均MSE计算；SSIM统一为valid窗口。不同分辨率和训练预算不相减。',
             '所有留出图已经用于开发；单种子差异不代表跨种子稳定性。', '']
    flat = []
    for (downscale, steps, seed), runs in sorted(groups.items()):
        lines.extend([f'## downscale={downscale}, steps={steps}, seed={seed}', '',
                      '| 实验 | 比较对象 | PSNR | ΔdB | SSIM | LPIPS↓ | 高斯数 | 训练分钟 | 显存GiB | GPU渲染ms |',
                      '| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |'])
        for variant, result in runs.items():
            parent = 'baseline'
            if variant == 'official_rasterizer':
                parent = 'rasterizer_prerequisites'
            elif variant == 'official_reference':
                parent = 'official_policy_gsplat'
            elif variant == 'alpha99_ste':
                parent = 'alpha99'
            elif variant == 'opacity_reset_rewrap':
                parent = 'topology_before_adam'
            reference = runs.get(parent)
            metrics = result['metrics']
            delta = metrics['psnr'] - reference['metrics']['psnr'] if reference else None
            row = {'variant': variant, 'downscale': downscale, 'steps': steps, 'seed': seed,
                   'parent': parent, 'psnr': metrics['psnr'], 'delta_db': delta,
                   'ssim_valid': metrics['ssim_valid'], 'gaussians': metrics['gaussians'],
                   'train_minutes': result['train_seconds']/60,
                   'peak_allocated_gib': result['peak_allocated_mb']/1024,
                   'render_gpu_ms': result['render_gpu_ms']}
            # Find by variant within this protocol, not the last scan's path.
            run_name = f'{variant}-s{seed}-d{downscale}-t{steps}'
            lpips_path = report / 'runs' / run_name / 'lpips.json'
            row['lpips_vgg_standard'] = (json.loads(lpips_path.read_text())['lpips_vgg_standard']
                                         if lpips_path.exists() else None)
            flat.append(row)
            delta_text = f'{delta:+.3f}' if delta is not None else '缺少对照'
            lpips_text = f'{row["lpips_vgg_standard"]:.4f}' if row['lpips_vgg_standard'] is not None else '待评估'
            lines.append(f'| {variant} | {parent} | {row["psnr"]:.3f} | {delta_text} | '
                         f'{row["ssim_valid"]:.4f} | {lpips_text} | {row["gaussians"]:,} | '
                         f'{row["train_minutes"]:.2f} | {row["peak_allocated_gib"]:.2f} | '
                         f'{row["render_gpu_ms"]:.2f} |')
        lines.append('')
    failures = list(report.glob('runs/*/failure.json'))
    if failures:
        lines.extend(['## 未成功项', ''])
        for path in failures:
            if not (path.parent / 'result.json').exists():
                lines.append(f'- {path.parent.name}：见 `{path.relative_to(report)}` 和 `worker.log`。')
    (report / 'RESULTS.md').write_text('\n'.join(lines) + '\n')
    if flat:
        with (report / 'results.csv').open('w') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(flat[0]))
            writer.writeheader()
            writer.writerows(flat)
    print(f'{len(flat)} completed runs; {len(failures)} failure records; {report / "RESULTS.md"}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    summarize(parser.parse_args().report)
