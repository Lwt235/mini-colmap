"""Offline CPU LPIPS and all-view comparisons for the matched teatime tests."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

os.environ['CUDA_VISIBLE_DEVICES'] = '-1'
os.environ.setdefault('OMP_NUM_THREADS', '4')
os.environ.setdefault('OPENBLAS_NUM_THREADS', '4')

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / '.cache/gaussian-upstream-audit'


def read(path):
    return json.loads(path.read_text())


def write(path, value):
    from minicolmap.ui.worker import atomic_json
    atomic_json(path, value)


def perceptual(results, report):
    if not results:
        return
    import cv2
    import numpy as np
    import torch
    torch.set_num_threads(4)
    sys.path.insert(0, str(CACHE))
    checkpoints = CACHE / 'eval-hub/checkpoints'
    checkpoints.mkdir(parents=True, exist_ok=True)
    for name in ('vgg16-397923af.pth', 'vgg.pth'):
        target = checkpoints / name
        if not target.exists():
            os.link(CACHE / 'eval-weights' / name, target)
    torch.hub.set_dir(str(CACHE / 'eval-hub'))
    from lpipsPyTorch.modules.lpips import LPIPS
    criterion = LPIPS('vgg').eval()
    with torch.no_grad():
        for name, result in results.items():
            destination = report / 'runs' / name / 'lpips.json'
            if destination.exists():
                old = read(destination)
                if old['checkpoint_sha256'] != result['checkpoint_sha256']:
                    raise RuntimeError('LPIPS cache belongs to another checkpoint')
                continue
            rows = []
            for row in result['metrics']['per_view']:
                images = []
                for suffix in ('', '-reference'):
                    path = report / 'runs' / name / 'validation' / f'{row["image_id"]}{suffix}.png'
                    image = cv2.imread(str(path))
                    if image is None:
                        raise FileNotFoundError(path)
                    images.append(torch.from_numpy(np.ascontiguousarray(image[:, :, ::-1])).float().permute(2, 0, 1)[None] / 255)
                rows.append({'image_id': row['image_id'], 'lpips_vgg_standard':
                             float(criterion(images[0] * 2 - 1, images[1] * 2 - 1))})
            write(destination, {'checkpoint_sha256': result['checkpoint_sha256'],
                  'lpips_vgg_standard': float(np.mean([r['lpips_vgg_standard'] for r in rows])),
                  'per_view': rows, 'scope': 'Clipped rounded RGB PNG; VGG LPIPS [-1,1]; CPU, cached weights only'})


def comparison(results, report, ids, filename, crop=False):
    if not results:
        return
    import cv2
    import numpy as np
    width, height = 320, 270
    names = list(results)
    canvas = np.full((height * len(ids) + 40, width * (len(names) + 1), 3), 246, dtype=np.uint8)
    for column, title in enumerate(['Reference'] + names):
        cv2.putText(canvas, title, (column * width + 8, 26), cv2.FONT_HERSHEY_SIMPLEX, .6, (20, 20, 20), 1, cv2.LINE_AA)
    for row, image_id in enumerate(ids):
        for column, name in enumerate([None] + names):
            source = names[0] if name is None else name
            suffix = '-reference' if name is None else ''
            image = cv2.imread(str(report / 'runs' / source / 'validation' / f'{image_id}{suffix}.png'))
            if image is None:
                raise RuntimeError('Missing comparison image')
            if crop:
                h, w = image.shape[:2]
                image = image[int(h * .33):int(h * .83), int(w * .28):int(w * .72)]
            h, w = image.shape[:2]
            scale = min((width - 8) / w, (height - 26) / h)
            image = cv2.resize(image, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA)
            y, x = row * height + 62, column * width + 4
            canvas[y:y + image.shape[0], x:x + image.shape[1]] = image
            label = f'View {image_id}'
            if name:
                metric = next(r for r in results[name]['metrics']['per_view'] if r['image_id'] == image_id)
                label += f' | {metric["psnr"]:.2f} dB'
            cv2.putText(canvas, label, (x, row * height + 56), cv2.FONT_HERSHEY_SIMPLEX, .45, (30, 30, 30), 1, cv2.LINE_AA)
    if not cv2.imwrite(str(report / filename), canvas):
        raise RuntimeError('Failed to write comparison image')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    report = args.report.resolve()
    plan = read(report / 'plan.json')
    execution = read(report / 'execution.json') if (report / 'execution.json').exists() else {'status': 'prepared'}
    results = {}
    for name in plan['variants']:
        path = report / 'runs' / name / 'result.json'
        if path.exists():
            value = read(path)
            if value['signature'] != plan['signature']:
                raise RuntimeError('Result signature mismatch')
            results[name] = value
    perceptual(results, report)
    lines = ['**Teatime 官方训练与单因素实验**', '',
             f'状态：`{execution["status"]}`；完整训练结果 {len(results)}/{len(plan["variants"])} 组。', '',
             '统一原尺寸 988×730、169/8 原划分、白背景、30000 步、seed0；所有权重由同一个 gsplat 渲染入口评测。'
             'PSNR/SSIM 使用截断浮点图像；LPIPS 使用保存的 RGB PNG、VGG、[-1,1] 输入。'
             '这些视图已经用于开发；单种子差异是探索性观察，不代表跨种子稳定收益。', '',
             '| 实验 | 完成步数 | PSNR：平均MSE | PSNR：逐图均值 | SSIM | LPIPS↓ | 高斯数 |',
             '| --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    for name in plan['variants']:
        if name not in results:
            lines.append(f'| {name} | 未完成 | — | — | — | — | — |')
            continue
        metric = results[name]['metrics']
        lpips = read(report / 'runs' / name / 'lpips.json')['lpips_vgg_standard']
        lines.append(f'| {name} | 30000 | {metric["psnr"]:.4f} | {metric["mean_view_psnr"]:.4f} | '
                     f'{metric["ssim_valid"]:.4f} | {lpips:.4f} | {metric["gaussians"]:,} |')
    if 'baseline' in results:
        lines.extend(['', '相对本轮新训练 baseline 的单次差值：', ''])
        baseline = results['baseline']['metrics']
        for name, result in results.items():
            if name != 'baseline':
                metric = result['metrics']
                lines.append(f'- {name}：PSNR {metric["psnr"] - baseline["psnr"]:+.4f} dB，'
                             f'SSIM {metric["ssim_valid"] - baseline["ssim_valid"]:+.5f}。')
    if results:
        comparison(results, report, plan['validation_ids'], 'comparison-all-views.png')
        comparison(results, report, [60, 80, 161], 'comparison-fixed-crops.png', crop=True)
        lines.extend(['', '全部留出视角：', '', '![全部视角](comparison-all-views.png)', '',
                      '预先指定的弱视角 60/80/161，在相同归一化区域裁剪：', '',
                      '![固定裁剪](comparison-fixed-crops.png)'])
    lines.extend(['', 'official_entry 使用未修改的固定版本 `train.py.training`，仅适配 Scene/相机 I/O 和评测监控。'
                  '其 checkpoint 配置是项目渲染描述，精确训练策略另存 `training-policy.json`。'
                  'baseline/sh_rest_lr/statistics 使用同一个受控实验循环和融合传输路径；'
                  '正式比较前必须通过本场景原尺寸的前向/梯度一致性核验。', '',
                  '参数/输入/源码冻结清单见 [plan.json](plan.json)，执行状态见 [execution.json](execution.json)。'
                  '现有工作台模型不替换；不与尚未执行的模糊图过滤 A/B 混合。', '',
                  '在项目根目录启动或继续缺少的完整训练：', '', '```bash',
                  f'.venv/bin/python scripts/run_teatime_official_tests.py --run --report {report}', '```', '',
                  '每个 worker 启动前按 GPU 7→0 再检查占用；全部忙则退出，不建立后台等待队列。'])
    (report / 'README.md').write_text('\n'.join(lines) + '\n')
    print(json.dumps({'completed_results': list(results), 'status': execution['status'], 'report': str(report / 'README.md')}))


if __name__ == '__main__':
    main()
