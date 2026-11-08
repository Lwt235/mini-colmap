"""Plot existing scientific evidence; selected views include wins and failures."""
from pathlib import Path
import json

import cv2
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / 'reports/3dgs-official-ablation-20261020'
ORIGINAL = ROOT / 'outputs/ui_3dgs_20261019_230921_797f2a'
OFFICIAL = REPORT / 'native/runs/official_reference-s0-d1-t30000'


def main():
    old = json.loads((ORIGINAL / 'summary.json').read_text())
    new = json.loads((OFFICIAL / 'result.json').read_text())
    old_rows = {row['image_id']: row for row in old['per_view_metrics']}
    new_rows = {row['image_id']: row for row in new['metrics']['per_view']}
    ids = [60, 162, 140]
    fig, axes = plt.subplots(3, 3, figsize=(15, 12), constrained_layout=True)
    for row, image_id in enumerate(ids):
        paths = [ORIGINAL / f'previews/reference-{image_id}.png',
                 ORIGINAL / f'validation/image-{image_id}.png',
                 OFFICIAL / f'validation/{image_id}.png']
        titles = [f'Reference, camera {image_id}',
                  f'Original: {old_rows[image_id]["psnr"]:.2f} dB',
                  f'Graphdeco: {new_rows[image_id]["psnr"]:.2f} dB']
        for col, (path, title) in enumerate(zip(paths, titles)):
            pixels = cv2.imread(str(path))
            if pixels is None:
                raise FileNotFoundError(path)
            axes[row, col].imshow(cv2.cvtColor(pixels, cv2.COLOR_BGR2RGB))
            axes[row, col].set_title(title, fontsize=13)
            axes[row, col].axis('off')
    fig.suptitle('Native 985 x 725 | 30,000 steps | seed 0 | development views\n'
                 'Selected largest losses (60,162) and largest gain (140); see all 9 views in JSON', fontsize=15)
    fig.savefig(REPORT / 'native-comparison-seed0.png', dpi=130)
    plt.close(fig)


if __name__ == '__main__':
    main()
