#!/usr/bin/env python3
"""Train-only compression controls; select using dev queries, never test labels.

Keeps the student's algorithms and the existing workspace intact. GPU work uses
the same GPU 7 reservation as the semantic CLI. Checkpoints are diagnostic until
explicitly installed into an independent, versioned semantic workspace.
"""
import argparse
import json
import os
from pathlib import Path
import time

os.environ.setdefault('CUDA_VISIBLE_DEVICES', '7')
os.environ.setdefault('OMP_NUM_THREADS', '4')

import cv2
import numpy as np
import torch

from minicolmap.semantics import algorithms
from minicolmap.semantics.compression import build_codec, initialize_conditioning, codec_loss
from minicolmap.semantics.encoders import ClipEncoder, gpu_session
from minicolmap.semantics.evaluation import annotation_masks, binary_metrics
from minicolmap.semantics.pipeline import read_regions
from minicolmap.semantics.project import load_config, project, sha256, write_json


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--config', default='configs/semantic_waldo.json')
    parser.add_argument('--report', default='reports/semantic-compression-repair-20261021')
    parser.add_argument('--steps', type=int, default=12000)
    parser.add_argument('--dimensions', type=int, nargs='+', default=[3, 16, 32])
    parser.add_argument('--centered', action='store_true')
    parser.add_argument('--pca-only', action='store_true', help='Skip gradient training after the train-only PCA controls')
    args = parser.parse_args()
    if any(not 1 <= dim < 512 for dim in args.dimensions) or args.steps < 1:
        parser.error('Use dimensions 1..511 and positive training steps')
    config = load_config(args.config)
    prepared = project(config)
    report = Path(args.report)
    if (report / 'results.json').exists():
        raise ValueError('Report already exists; select a new --report directory')
    report.mkdir(parents=True, exist_ok=True)
    source_hash = sha256(Path(config['workspace']) / 'autoencoder/checkpoint.pt')
    results = []
    with gpu_session(config):
        device = 'cuda'
        clip = ClipEncoder(config)
        cases = []
        for annotation in prepared['annotations']:
            image_id = annotation['image_id']
            if image_id not in prepared['splits']['dev']:
                continue
            labels, features = read_regions(config, image_id)
            cases.append(dict(image_id=image_id, labels=labels,
                              features=torch.from_numpy(features).to(device),
                              queries=[(text, target, clip.texts([text, *config['query']['negatives']]))
                                       for text, target in sorted(annotation_masks(annotation['annotation']).items())]))

        def evaluate(name, transform):
            rows = []
            with torch.inference_mode():
                for case in cases:
                    features = case['features']
                    predicted = transform(features)
                    labels = case['labels']
                    used = torch.from_numpy(np.unique(labels[labels >= 0])).to(device)
                    cosine = float(torch.nn.functional.cosine_similarity(predicted[used], features[used]).mean())
                    for text, gt, embeddings in case['queries']:
                        values = algorithms.text_relevancy(predicted, embeddings[0], embeddings[1:],
                                                          temperature=config['query']['temperature']).cpu().numpy()
                        valid = labels >= 0
                        heat = np.zeros(labels.shape, np.float32)
                        heat[valid] = values[labels[valid]]
                        heat = cv2.resize(heat, (gt.shape[1], gt.shape[0]), interpolation=cv2.INTER_LINEAR)
                        validity = cv2.resize(valid.astype(np.uint8), (gt.shape[1], gt.shape[0]),
                                              interpolation=cv2.INTER_NEAREST_EXACT) > 0
                        heat[~validity] = 0
                        rows.append(dict(image_id=case['image_id'], query=text, cosine=cosine,
                                         **binary_metrics((heat > config['query']['threshold']) & validity, gt > 0, heat)))
            result = dict(name=name, mean_iou=float(np.mean([r['iou'] for r in rows])),
                          peak_inside_gt_rate=float(np.mean([r['peak_inside_gt'] for r in rows])), per_query=rows)
            results.append(result)
            write_json(report / 'results.json', dict(protocol='Train regions only; fixed dev queries/threshold; no test access',
                       rgb_checkpoint_sha256=prepared['rgb_checkpoint_sha256'],
                       original_autoencoder_sha256=source_hash,
                       algorithm_sha256=sha256(algorithms.__file__), seed=config['seed'],
                       train_ids=prepared['splits']['train'], dev_ids=prepared['splits']['dev'],
                       results=results))
            print(json.dumps({k: v for k, v in result.items() if k != 'per_query'}), flush=True)
            return result

        evaluate('raw', lambda x: x)
        original = torch.load(Path(config['workspace']) / 'autoencoder/checkpoint.pt', map_location='cpu', weights_only=True)
        model = algorithms.build_autoencoder(512, original['latent_dim']).to(device)
        model.load_state_dict(original['state'])
        model.eval()
        evaluate('original-mlp3-3000', model)
        del model, clip
        train = []
        for image_id in prepared['splits']['train']:
            labels, features = read_regions(config, image_id)
            train.append(features[np.unique(labels[labels >= 0])])
        inputs = torch.from_numpy(np.concatenate(train)).to(device)
        mean = inputs.mean(0)
        # Full 512x512 covariance eigendecomposition, fitted only on train.
        centered = inputs - mean
        _, vectors = torch.linalg.eigh(centered.T @ centered / (len(inputs) - 1))
        for dim in args.dimensions:
            components = vectors[:, -dim:].flip(1).T.contiguous()
            evaluate(f'pca{dim}', lambda x: (x - mean) @ components.T @ components + mean)
            torch.save(dict(codec='pca', latent_dim=dim, mean=mean.cpu(), components=components.cpu(),
                            train_ids=prepared['splits']['train'], rgb_checkpoint_sha256=prepared['rgb_checkpoint_sha256']),
                       report / f'pca{dim}.pt')
        del centered
        for dim in ([] if args.pca_only else args.dimensions):
            torch.manual_seed(config['seed'])
            model = build_codec(512, dim, conditioning='centered' if args.centered else None).to(device)
            if args.centered:
                initialize_conditioning(model, inputs)
            optimizer = torch.optim.Adam(model.parameters(), lr=config['autoencoder']['learning_rate'])
            start = time.monotonic()
            history = []
            for step in range(1, args.steps + 1):
                model.train()
                batch = inputs[torch.randint(len(inputs), (config['autoencoder']['batch_size'],), device=device)]
                optimizer.zero_grad(set_to_none=True)
                loss = codec_loss(model, model(batch), batch, .2 if args.centered else 0.)
                if not torch.isfinite(loss):
                    raise ValueError('Nonfinite compression loss')
                loss.backward()
                optimizer.step()
                if step % 1000 == 0:
                    history.append(dict(step=step, loss=float(loss.detach())))
                    write_json(report / 'progress.json', dict(dim=dim, step=step, total=args.steps, history=history))
                    print(json.dumps(dict(dim=dim, step=step, loss=float(loss.detach()))), flush=True)
                if step in {3000, 6000, args.steps}:
                    model.eval()
                    name = f'mlp{dim}-{step}'
                    result = evaluate(name, model)
                    torch.save(dict(state={k: v.cpu() for k, v in model.state_dict().items()}, latent_dim=dim,
                                    train_ids=prepared['splits']['train'], training_regions=len(inputs),
                                    conditioning='centered' if args.centered else None,
                                    rgb_checkpoint_sha256=prepared['rgb_checkpoint_sha256']), report / f'{name}.pt')
                    write_json(report / f'{name}-training.json', dict(history=history, seconds=time.monotonic()-start,
                                                                  dev_iou=result['mean_iou']))
            del model, optimizer
    if source_hash != sha256(Path(config['workspace']) / 'autoencoder/checkpoint.pt'):
        raise RuntimeError('Original checkpoint changed')


if __name__ == '__main__':
    main()
