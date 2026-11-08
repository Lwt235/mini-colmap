"""Explicit educational metrics, not a silent clone of official evaluation."""
from pathlib import Path
import json
import time

import cv2
import numpy as np

from .project import write_json


def annotation_masks(path):
    record = json.loads(Path(path).read_text())
    size = (record['info']['height'], record['info']['width'])
    masks = {}
    for item in record['objects']:
        polygon = np.asarray(item['segmentation'], dtype=np.float64)
        if polygon.ndim != 2 or polygon.shape[1] != 2 or len(polygon) < 3 or not np.isfinite(polygon).all():
            raise ValueError('Expected one finite Nx2 polygon per annotation object')
        mask = masks.setdefault(item['category'], np.zeros(size, np.uint8))
        cv2.fillPoly(mask, [np.rint(polygon).astype(np.int32)], 1)
    return masks


def binary_metrics(prediction, target, scores):
    prediction, target = np.asarray(prediction, bool), np.asarray(target, bool)
    if prediction.shape != target.shape or scores.shape != target.shape or not np.isfinite(scores).all():
        raise ValueError('Prediction/GT/scores must share one finite pixel grid')
    intersection = np.logical_and(prediction, target).sum()
    union = np.logical_or(prediction, target).sum()
    peak = np.unravel_index(np.argmax(scores), scores.shape)
    return {'iou': float(intersection / union) if union else 1.,
            'peak_inside_gt': bool(target[peak]) if scores.max() > 0 else False,
            'prediction_pixels': int(prediction.sum()), 'gt_pixels': int(target.sum())}


def evaluate(config, prepared, split, mode):
    from .encoders import ClipEncoder
    from .pipeline import prediction_map, choose_frames, load_autoencoder, load_features
    from .rendering import FrozenGaussians
    if split not in ('dev', 'test'):
        raise ValueError('Semantic evaluation requires the fixed dev/test split')
    clip = ClipEncoder(config)
    renderer = FrozenGaussians(config, prepared) if mode in ('fusion', 'distilled') else None
    decoder = load_autoencoder(config, prepared) if mode != '2d' else None
    latents = load_features(config, prepared, mode) if mode in ('fusion', 'distilled') else None
    annotations = {item['image_id']: item for item in prepared['annotations']}
    rows = []
    for frame in choose_frames(prepared, split):
        masks = annotation_masks(annotations[frame['image_id']]['annotation'])
        for text, gt in sorted(masks.items()):
            scores, valid, _ = prediction_map(config, prepared, frame, text, mode,
                clip=clip, renderer=renderer, decoder=decoder, latents=latents)
            # Compare ALL methods on the unchanged original annotation grid.
            # Different internal render resolutions must not change metric pixels.
            scores = cv2.resize(scores, (gt.shape[1], gt.shape[0]), interpolation=cv2.INTER_LINEAR)
            valid = cv2.resize(valid.astype(np.uint8), (gt.shape[1], gt.shape[0]), interpolation=cv2.INTER_NEAREST_EXACT) > 0
            scores[~valid] = 0
            gt = gt > 0
            rows.append({'image_id': frame['image_id'], 'query': text,
                         **binary_metrics((scores > config['query']['threshold']) & valid, gt, scores)})
    result = {'split': split, 'mode': mode, 'threshold': config['query']['threshold'], 'per_query': rows,
              'mean_iou': float(np.mean([r['iou'] for r in rows])),
              'peak_inside_gt_rate': float(np.mean([r['peak_inside_gt'] for r in rows])),
              'protocol': 'Fixed threshold; single SAM level; all methods evaluated on original GT grid; peak in polygon. Not official paper metrics.',
              'scope': prepared['evaluation_scope']}
    path = Path(config['workspace']) / 'evaluation' / f'{split}-{mode}-{time.time_ns()}.json'
    write_json(path, result)
    return result
