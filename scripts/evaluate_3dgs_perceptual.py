"""Offline VGG LPIPS on identical saved 8-bit validation PNG protocols (CPU).

Report both conventional [-1,1] inputs and Graphdeco metrics.py's [0,1]
range. No Internet access or GPU reservation is needed by this evaluator.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / '.cache/gaussian-upstream-audit'
CRITERION = None


def evaluate(report):
    global CRITERION
    import cv2
    import numpy as np
    import torch
    torch.set_num_threads(4)
    sys.path.insert(0, str(CACHE))
    hub = CACHE / 'eval-hub'
    checkpoints = hub / 'checkpoints'
    checkpoints.mkdir(parents=True, exist_ok=True)
    for name in ('vgg16-397923af.pth', 'vgg.pth'):
        target = checkpoints / name
        if not target.exists():
            os.link(CACHE / 'eval-weights' / name, target)
    torch.hub.set_dir(str(hub))
    from lpipsPyTorch.modules.lpips import LPIPS
    if CRITERION is None:
        CRITERION = LPIPS('vgg').eval()
    criterion = CRITERION
    results = []
    paths = sorted(report.glob('runs/*/result.json'))
    original_record = report / 'reference-original.json'
    if original_record.exists():
        paths.append(original_record)
    for path in paths:
        is_original = path == original_record
        destination = report / 'reference-original-lpips.json' if is_original else path.parent / 'lpips.json'
        if destination.exists():
            continue
        summary = json.loads(path.read_text())
        original_directory = Path(summary['provenance']) if is_original else None
        if is_original:
            summary = {'metrics': {'per_view': summary['summary']['per_view_metrics']}}
        per_view = []
        with torch.no_grad():
            for row in summary['metrics']['per_view']:
                image_id = row['image_id']
                images = []
                for suffix in ('', '-reference'):
                    if is_original:
                        png = (original_directory / f'validation/image-{image_id}.png' if not suffix else
                               original_directory / f'previews/reference-{image_id}.png')
                    else:
                        png = path.parent / f'validation/{image_id}{suffix}.png'
                    bgr = cv2.imread(str(png))
                    if bgr is None:
                        raise FileNotFoundError(png)
                    rgb = np.ascontiguousarray(bgr[:, :, ::-1])
                    images.append(torch.from_numpy(rgb).float().permute(2, 0, 1)[None] / 255)
                conventional = float(criterion(images[0]*2-1, images[1]*2-1).item())
                upstream_range = float(criterion(*images).item())
                per_view.append({'image_id': image_id, 'lpips_vgg_standard': conventional,
                                 'lpips_vgg_upstream_range': upstream_range})
        result = {'input': 'saved clipped/rounded 8-bit RGB PNG; same resolution and views per group',
                  'standard_range': '[-1,1]', 'upstream_range': '[0,1]',
                  'lpips_vgg_standard': float(np.mean([row['lpips_vgg_standard'] for row in per_view])),
                  'lpips_vgg_upstream_range': float(np.mean([row['lpips_vgg_upstream_range'] for row in per_view])),
                  'weights': json.loads((CACHE / 'eval-weights/manifest.json').read_text()),
                  'per_view': per_view}
        destination.write_text(json.dumps(result, indent=2))
        results.append((path.parent.name, result['lpips_vgg_standard']))
        print(json.dumps({'run': path.parent.name, 'lpips_vgg_standard': result['lpips_vgg_standard']}), flush=True)
    if list(report.glob('runs/*/result.json')):
        from summarize_official_3dgs import summarize
        summarize(report)
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('--watch', action='store_true')
    args = parser.parse_args()
    if not args.watch:
        evaluate(args.report)
    else:
        manifest_path = next(args.report.glob('manifest-d*-t*-s*.json'))
        manifest = json.loads(manifest_path.read_text())
        expected = [args.report / 'runs' / f'{name}-s{manifest["seed"]}-d{manifest["downscale"]}-t{manifest["steps"]}'
                    for name in manifest['policies']]
        while True:
            pending = [path for path in args.report.glob('runs/*/result.json')
                       if not (path.parent / 'lpips.json').exists()]
            if pending:
                evaluate(args.report)
            if all((path / 'result.json').exists() or (path / 'failure.json').exists() for path in expected):
                break
            time.sleep(10)
