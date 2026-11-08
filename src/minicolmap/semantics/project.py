"""Versioned data split and cache identities. Never rewrite RGB/data sources."""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import cv2
import numpy as np

from ..io import load_dataset, load_reconstruction
from ..rendering.data import scaled_intrinsics


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    temporary.replace(path)


def contained(root, path):
    root = Path(root).resolve()
    result = (root / path).resolve()
    if not result.is_relative_to(root):
        raise ValueError('Semantic paths must stay inside the project directory')
    return result


def load_config(path, *, root=None):
    path = Path(path).resolve()
    root = Path(root).resolve() if root else path.parent.parent
    config = json.loads(path.read_text(encoding='utf-8'))
    required = {'schema_version', 'name', 'dataset', 'annotations', 'rgb_run', 'workspace',
                'model_manifest', 'seed', 'dev_image_ids', 'smoke_image_ids', 'extract',
                'regions', 'autoencoder', 'distill', 'query'}
    if set(config) != required or config['schema_version'] not in (1, 2):
        raise ValueError('Unsupported semantic configuration fields/schema')
    for key in ('dataset', 'annotations', 'rgb_run', 'workspace', 'model_manifest'):
        config[key] = str(contained(root, config[key]))
    if not config['name'] or not isinstance(config['name'], str):
        raise ValueError('Semantic project needs a name')
    for key in ('dev_image_ids', 'smoke_image_ids'):
        value = config[key]
        if (not isinstance(value, list) or (not value and not (key == 'dev_image_ids' and config['schema_version'] == 2)) or len(set(value)) != len(value)
                or any(isinstance(i, bool) or not isinstance(i, int) or i < 1 for i in value)):
            raise ValueError(f'{key} needs distinct positive integer image IDs')
    if not 1 <= config['distill']['max_edge'] <= 1024 or not 1 <= config['extract']['max_edge'] <= 1920:
        raise ValueError('Semantic image size exceeds the supported budget')
    if not 1 <= config['autoencoder']['latent_dim'] <= 256:
        raise ValueError('Use 1..256 latent dimensions')
    for stage in ('autoencoder', 'distill'):
        if config[stage]['steps'] < 1 or config[stage]['learning_rate'] <= 0:
            raise ValueError('Training steps and learning rate must be positive')
    options = config['autoencoder']
    if options.get('codec', 'mlp') not in ('mlp', 'pca'):
        raise ValueError('Unsupported feature codec')
    if options.get('conditioning') not in (None, 'centered'):
        raise ValueError('Unsupported autoencoder conditioning')
    weight = options.get('residual_weight', 0.)
    import math
    if isinstance(weight, bool) or not isinstance(weight, (float, int)) or not math.isfinite(weight) or weight < 0:
        raise ValueError('Invalid residual reconstruction weight')
    if weight and options.get('conditioning') != 'centered':
        raise ValueError('Residual reconstruction requires centered conditioning')
    if options.get('codec') == 'pca' and (options.get('conditioning') is not None or weight):
        raise ValueError('PCA does not use MLP conditioning or residual loss')
    config['_root'] = str(root)
    return config


def identity(config):
    serializable = {k: v for k, v in config.items() if not k.startswith('_')}
    return hashlib.sha256(json.dumps(serializable, sort_keys=True).encode()).hexdigest()


def prepare(config):
    import torch
    output = Path(config['workspace'])
    run = Path(config['rgb_run'])
    dataset = load_dataset(config['dataset'])
    model = load_reconstruction(run / 'source_model.json')
    checkpoint = torch.load(run / 'checkpoint.pt', map_location='cpu', weights_only=True)
    if checkpoint.get('pipeline') != '3dgs' or checkpoint.get('schema_version') != 2:
        raise ValueError('Use a completed SH-colored RGB 3DGS checkpoint')
    annotations = json.loads(Path(config['annotations']).read_text())['frames']
    annotated_ids = {frame['image_id'] for frame in annotations}
    ids = set(model.poses)
    inference_only = config['schema_version'] == 2
    if inference_only and (annotations or config['dev_image_ids']):
        raise ValueError('Unlabelled scene configuration must have empty annotations and Dev IDs')
    if (not annotated_ids <= ids or (not inference_only and not set(config['dev_image_ids']) < annotated_ids)
            or not set(config['smoke_image_ids']) <= ids):
        raise ValueError('Invalid annotation/development/smoke IDs')
    for annotation in annotations:
        image = dataset['images'][annotation['image_id']]
        if image.name != annotation['image_name'] or not Path(annotation['annotation']).is_file():
            raise ValueError('Annotation filename and image ID do not match')
    rgb_holdout = set(checkpoint['validation_ids'])
    splits = {'train': sorted(ids - annotated_ids - rgb_holdout),
              'dev': sorted(config['dev_image_ids']),
              'test': sorted(annotated_ids - set(config['dev_image_ids'])),
              'rgb_holdout': sorted(rgb_holdout - annotated_ids)}
    if not splits['train']:
        raise ValueError('No registered RGB training views available for semantic learning')
    if not set(config['smoke_image_ids']) <= set(splits['train']):
        raise ValueError('Smoke frames must belong to semantic training views')
    frames = []
    for image_id in sorted(ids):
        image = dataset['images'][image_id]
        camera = dataset['cameras'][image.camera_id]
        pixels = cv2.imread(str(dataset['image_paths'][image_id]))
        if pixels is None or pixels.shape[:2] != (camera.height, camera.width):
            raise ValueError(f'Image/calibration size mismatch: {image.name}')
        pose = model.poses[image_id]
        frames.append(dict(image_id=image_id, name=image.name, path=str(dataset['image_paths'][image_id]),
                           image_sha256=sha256(dataset['image_paths'][image_id]),
                           width=camera.width, height=camera.height, K=camera.K.tolist(),
                           R=pose.R.tolist(), t=pose.t.tolist()))
    result = dict(schema_version=config['schema_version'], name=config['name'], config_identity=identity(config),
                  rgb_checkpoint_sha256=sha256(run / 'checkpoint.pt'),
                  source_model_sha256=sha256(run / 'source_model.json'),
                  dataset_sha256=sha256(config['dataset']), normalization=checkpoint['normalization'],
                  gaussian_count=len(checkpoint['state']['means']), frames=frames, splits=splits,
                  smoke_image_ids=config['smoke_image_ids'], annotations=annotations,
                  evaluation_scope=('Unlabelled scene inference only; no semantic accuracy or held-out claim.' if inference_only else 'Language held-out views; existing RGB/SfM may include these photos. Not end-to-end unseen.'),
                  pipeline_variant='Single-level SAM + OpenCLIP educational language field; not exact LangSplat reproduction')
    target = output / 'project.json'
    if target.exists() and json.loads(target.read_text()) != result:
        raise ValueError('Prepared workspace differs; use a new workspace rather than overwrite cached data')
    write_json(target, result)
    return result


def project(config, *, check_source=True):
    result = json.loads((Path(config['workspace']) / 'project.json').read_text())
    if result['config_identity'] != identity(config):
        raise ValueError('Config changed since prepare; choose a new workspace')
    if check_source and (sha256(Path(config['rgb_run']) / 'checkpoint.pt') != result['rgb_checkpoint_sha256']
                         or sha256(config['dataset']) != result['dataset_sha256']
                         or sha256(Path(config['rgb_run']) / 'source_model.json') != result['source_model_sha256']):
        raise ValueError('RGB checkpoint, source model or dataset changed; refusing stale features')
    return result


def image_frame(frame, max_edge):
    if 'image_sha256' in frame and sha256(frame['path']) != frame['image_sha256']:
        raise ValueError('Source photo changed since semantic prepare')
    pixels = cv2.imread(frame['path'], cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
    if pixels is None or pixels.shape[:2] != (frame['height'], frame['width']):
        raise ValueError('Missing or changed source photo')
    ratio = min(1., max_edge / max(frame['width'], frame['height']))
    width, height = max(1, round(frame['width'] * ratio)), max(1, round(frame['height'] * ratio))
    K = scaled_intrinsics(np.asarray(frame['K']), (frame['width'], frame['height']), (width, height))
    if pixels.shape[:2] != (height, width):
        pixels = cv2.resize(pixels, (width, height), interpolation=cv2.INTER_AREA)
    return cv2.cvtColor(pixels, cv2.COLOR_BGR2RGB), {**frame, 'width': width, 'height': height, 'K': K.tolist()}


def exercise_status():
    from . import algorithms
    tree = ast.parse(Path(algorithms.__file__).read_text(encoding='utf-8'))
    return [{'name': node.name, 'id': (ast.get_docstring(node) or '').split(':')[0],
             'status': 'todo' if any(isinstance(child, ast.Raise) and isinstance(child.exc, ast.Call)
                                   and isinstance(child.exc.func, ast.Name) and child.exc.func.id == 'NotImplementedError'
                                   for child in ast.walk(node)) else 'implemented_unverified'}
            for node in tree.body if isinstance(node, ast.FunctionDef)
            and (ast.get_docstring(node) or '').split(':')[0] in {f'L{i}' for i in range(1, 8)}]
