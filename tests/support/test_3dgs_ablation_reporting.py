"""Guard experimental selection and metric scope against misleading reports."""
import importlib.util
import json
import math
from pathlib import Path

import pytest


def load_script(name):
    path = Path(__file__).resolve().parents[2] / 'scripts' / (name + '.py')
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def record(scene, variant, seed, dev, test):
    return {'scene': scene, 'variant': variant, 'seed': seed,
            'evaluation': {'dev': {'psnr_from_mean_mse': dev},
                           'test': {'psnr_from_mean_mse': test, 'ssim': .8}},
            'summary': {'point_count': 100, 'elapsed_seconds': 1,
                        'peak_cuda_allocated_mb': 10},
            'render_benchmark': {'local_fps': 60, 'local_latency_p95_ms': 20}}


def test_configuration_selection_never_uses_test_psnr(tmp_path):
    reporter = load_script('summarize_3dgs_ablation')
    rows = [record(scene, variant, seed, dev, test)
            for scene in ('temple47', 'dino321') for seed in (0, 1)
            for variant, dev, test in (('baseline', 20, 20), ('lr', 23, 10), ('loss', 21, 40))]
    (tmp_path / 'results.json').write_text(json.dumps(rows))
    (tmp_path / 'plan.json').write_text(json.dumps({'jobs': list(range(len(rows)))}))
    result, _ = reporter.aggregate(tmp_path)
    assert result['dev_ranking'][0]['variant'] == 'lr'
    assert not result['provisional']
    original = result['dev_ranking']
    for row in rows:
        row['evaluation']['test']['psnr_from_mean_mse'] *= -5
    (tmp_path / 'results.json').write_text(json.dumps(rows))
    changed, _ = reporter.aggregate(tmp_path)
    assert changed['dev_ranking'] == original


def test_paired_gain_weights_input_sets_equally_and_marks_incomplete(tmp_path):
    reporter = load_script('summarize_3dgs_ablation')
    # Set A has more repeats, but must not dominate the per-input-set mean.
    rows = [record('a', variant, seed, dev, 20) for seed in (0, 1)
            for variant, dev in (('baseline', 20), ('lr', 22))]
    rows += [record('b', variant, 0, dev, 20)
             for variant, dev in (('baseline', 10), ('lr', 18))]
    (tmp_path / 'results.json').write_text(json.dumps(rows))
    (tmp_path / 'plan.json').write_text(json.dumps({'jobs': list(range(8))}))
    result, _ = reporter.aggregate(tmp_path)
    assert result['dev_ranking'][0]['mean_paired_dev_gain_db'] == pytest.approx(5)
    assert result['provisional']


def test_psnr_uses_mean_mse_and_excludes_other_views():
    runner = load_script('run_3dgs_ablation')
    rows = [{'image_id': index, 'mse': mse, 'psnr': -10 * math.log10(mse), 'ssim': .8}
            for index, mse in ((1, .01), (2, .0001), (3, .5))]
    score = runner.split_score(rows, {1, 2})
    assert score['views'] == 2
    assert score['psnr_from_mean_mse'] == pytest.approx(-10 * math.log10(.00505))
    assert score['mean_view_psnr'] == pytest.approx(30)
