"""Aggregate paired ablations without treating repeated captures as new objects."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import statistics

import cv2
import numpy as np


VARIANTS = ('baseline', 'adam', 'lr', 'density', 'loss', 'combined')


def average(values):
    return statistics.mean(values)


def aggregate(report):
    records = json.loads((report / 'results.json').read_text(encoding='utf-8'))
    groups = {}
    lookup = {(row['scene'], row['variant'], row['seed']): row for row in records}
    for row in records:
        groups.setdefault((row['scene'], row['variant']), []).append(row)
    rows = []
    for (scene, variant), values in sorted(groups.items(), key=lambda item: (item[0][0], VARIANTS.index(item[0][1]))):
        test = [row['evaluation']['test']['psnr_from_mean_mse'] for row in values]
        deltas = [row['evaluation']['dev']['psnr_from_mean_mse'] -
                  lookup[(scene, 'baseline', row['seed'])]['evaluation']['dev']['psnr_from_mean_mse']
                  for row in values if (scene, 'baseline', row['seed']) in lookup]
        rows.append({'scene': scene, 'variant': variant, 'seeds': [row['seed'] for row in values],
            'dev_psnr': average([row['evaluation']['dev']['psnr_from_mean_mse'] for row in values]),
            'paired_dev_gain_db': average(deltas) if deltas else None,
            'test_psnr': average(test), 'test_psnr_sample_std': statistics.stdev(test) if len(test) > 1 else None,
            'test_ssim': average([row['evaluation']['test']['ssim'] for row in values]),
            'gaussians': average([row['summary']['point_count'] for row in values]),
            'training_seconds': average([row['summary']['elapsed_seconds'] for row in values]),
            'peak_cuda_allocated_mb': max(row['summary']['peak_cuda_allocated_mb'] for row in values),
            'local_fps': average([row['render_benchmark']['local_fps'] for row in values]),
            'local_p95_ms': average([row['render_benchmark']['local_latency_p95_ms'] for row in values])})
    # Equal weight per input set, using ONLY dev to rank configurations.
    ranking = []
    scenes = sorted({row['scene'] for row in rows})
    for variant in VARIANTS[1:]:
        selected = [row for row in rows if row['variant'] == variant]
        if len(selected) == len(scenes):
            ranking.append({'variant': variant, 'mean_paired_dev_gain_db': average([row['paired_dev_gain_db'] for row in selected])})
    ranking.sort(key=lambda row: row['mean_paired_dev_gain_db'], reverse=True)
    summary = {'completed_jobs': len(records), 'input_sets': scenes,
               'scope': 'Within-input-set paired seeds. TempleRing/Temple share object identity.',
               'rows': rows, 'dev_ranking': ranking,
               'selection_metric': 'Mean paired dev PSNR gain, equal weight per input set. Test is not used for selection.'}
    plan = json.loads((report / 'plan.json').read_text(encoding='utf-8'))
    summary['provisional'] = len(records) != len(plan['jobs'])
    (report / 'aggregate.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if rows:
        with (report / 'aggregate.csv').open('w', newline='', encoding='utf-8-sig') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    return summary, records


def comparisons(report, records, selected):
    paths = []
    for scene in sorted({row['scene'] for row in records}):
        entries = {row['variant']: row for row in records if row['scene'] == scene and row['seed'] == 0}
        if any(key not in entries for key in ('baseline', selected)):
            continue
        image_id = entries['baseline']['evaluation']['test_ids'][0]
        baseline = Path(entries['baseline']['output'])
        candidates = [('reference', baseline / f'previews/reference-{image_id}.png')]
        for variant in dict.fromkeys(('baseline', 'adam', selected, 'combined')):
            if variant in entries:
                candidates.append((variant, Path(entries[variant]['output']) / f'validation/image-{image_id}.png'))
        tiles = []
        for label, path in candidates:
            pixels = cv2.imread(str(path))
            if pixels is None:
                raise ValueError('Missing actual validation image: ' + str(path))
            header = np.full((30, pixels.shape[1], 3), 245, np.uint8)
            cv2.putText(header, label, (7, 20), cv2.FONT_HERSHEY_SIMPLEX, .45, (25, 25, 25), 1, cv2.LINE_AA)
            tiles.append(np.concatenate((header, pixels), axis=0))
        target = report / f'comparison-{scene}.png'
        ok, encoded = cv2.imencode('.png', np.concatenate(tiles, axis=1))
        if not ok:
            raise OSError('Could not encode comparison')
        target.write_bytes(encoded.tobytes())
        paths.append({'scene': scene, 'image_id': image_id, 'path': str(target),
                      'scope': 'First predeclared test view; actual 8-bit display exports, no enhancement'})
    return paths


def chart(report, summary):
    scenes = summary['input_sets']
    width, height = 1080, 350
    pieces = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
              '<rect width="100%" height="100%" fill="#111827"/>',
              '<g font-family="Arial,sans-serif" fill="#e5e7eb">',
              '<text x="28" y="30" font-size="18">Paired development PSNR gain vs baseline (dB)</text>']
    maximum = max(1., max(abs(row['paired_dev_gain_db'] or 0) for row in summary['rows']) * 1.2)
    for index, scene in enumerate(scenes):
        left, top, center = 35 + index * 350, 70, 190
        pieces.append(f'<text x="{left}" y="{top}" font-size="16">{scene}</text>')
        pieces.append(f'<path d="M {left} {center} H {left+315}" stroke="#6b7280"/>')
        for j, variant in enumerate(VARIANTS[1:]):
            row = next((row for row in summary['rows'] if row['scene'] == scene and row['variant'] == variant), None)
            if row is None:
                continue
            gain = row['paired_dev_gain_db'] or 0
            magnitude = abs(gain) / maximum * 95
            x = left + j * 62
            y = center - magnitude if gain >= 0 else center
            color = '#38bdf8' if gain >= 0 else '#fb923c'
            pieces.extend([f'<rect x="{x}" y="{y:.2f}" width="44" height="{magnitude:.2f}" fill="{color}"/>',
                           f'<text x="{x}" y="{center+118}" font-size="11">{variant}</text>',
                           f'<text x="{x}" y="{y-7 if gain>=0 else y+magnitude+16:.2f}" font-size="12">{gain:+.2f}</text>'])
    pieces.append('</g></svg>')
    (report / 'dev-gain.svg').write_text('\n'.join(pieces), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, default=Path('reports/3dgs-ablation-20261019'))
    args = parser.parse_args()
    summary, records = aggregate(args.report)
    if summary['dev_ranking']:
        winner = summary['dev_ranking'][0]['variant']
        images = comparisons(args.report, records, winner)
        (args.report / 'comparison_manifest.json').write_text(json.dumps(images, indent=2) + '\n', encoding='utf-8')
        chart(args.report, summary)
    print(json.dumps({'completed': summary['completed_jobs'], 'dev_ranking': summary['dev_ranking']}))


if __name__ == '__main__':
    main()
