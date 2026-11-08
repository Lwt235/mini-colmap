"""NeRF input checks: calibrated images/poses do not require points.

A deliberate NotImplementedError protocol fixture verifies that a point-free
input reaches network construction and preserves explicit error reporting.
"""
from __future__ import annotations

import copy
import json

import cv2
import numpy as np
import pytest

from minicolmap.io import load_reconstruction, save_reconstruction
from minicolmap.models import Point3D, Reconstruction, validate_reconstruction
from minicolmap.rendering import data as rendering_data
from minicolmap.rendering.config import resolve_render_config


@pytest.fixture
def posed_manifest(tmp_path):
    K = [[10., .2, 3.], [0., 11., 2.], [0., 0., 1.]]
    rotations = {3: np.eye(3), 11: np.eye(3),
                 19: np.array([[0., -1., 0.], [1., 0., 0.], [0., 0., 1.]])}
    centers = {3: np.array([2., 3., 4.]), 11: np.array([6., 3., 4.]),
               19: np.array([2., 7., 4.])}
    images, poses = [], []
    for image_id in (3, 11, 19):
        path = tmp_path / f'{image_id}.png'
        ok, encoded = cv2.imencode('.png', np.full((6, 8, 3), [20, 90, 180], dtype=np.uint8))
        assert ok
        encoded.tofile(path)
        images.append({'id': image_id, 'name': path.name, 'path': path.name, 'camera_id': 7})
        poses.append({'image_id': image_id, 'R': rotations[image_id].tolist(),
                      't': (-rotations[image_id] @ centers[image_id]).tolist()})
    document = {'cameras': [{'id': 7, 'width': 8, 'height': 6, 'K': K}], 'images': images,
                'pose_convention': 'world_to_camera', 'poses': poses}
    manifest = tmp_path / 'dataset.json'
    manifest.write_text(json.dumps(document), encoding='utf-8')
    return manifest, document, centers


def _save_empty_model(manifest, output):
    model = rendering_data.load_posed_dataset(manifest)
    assert not model.points
    save_reconstruction(model, output)
    return model


def test_posed_dataset_loads_stable_ids_without_sparse_points(posed_manifest):
    manifest, document, centers = posed_manifest
    before = manifest.read_bytes()
    model = rendering_data.load_posed_dataset(manifest)
    assert isinstance(model, Reconstruction)
    assert set(model.images) == set(model.poses) == {3, 11, 19}
    assert model.points == {}
    assert all(image.keypoints.shape == (0, 2) for image in model.images.values())
    for image_id, expected in centers.items():
        np.testing.assert_allclose(model.poses[image_id].center, expected)
    validate_reconstruction(model)
    assert manifest.read_bytes() == before


def test_nerf_accepts_empty_points_and_normalizes_from_camera_centers(posed_manifest, tmp_path):
    manifest, _, _ = posed_manifest
    model_path = tmp_path / 'empty-model.json'
    model = _save_empty_model(manifest, model_path)
    config = resolve_render_config({'method': 'nerf', 'image_downscale': 2, 'holdout_every': 2})
    scene = rendering_data.load_training_scene(model_path, manifest, config)
    np.testing.assert_allclose(scene.center, [2., 3., 4.])
    assert scene.scale == pytest.approx(4.)
    assert scene.normalization_source == 'camera_centers'
    assert scene.points.shape == scene.colors.shape == (0, 3)
    assert scene.train_ids == [3, 19] and scene.validation_ids == [11]
    # Verify the normalized frame still projects arbitrary world points exactly.
    world = np.array([3., 4., 10.])
    normalized = (world - scene.center) / scene.scale
    for image_id, frame in scene.frames.items():
        original = model.cameras[7].K @ (model.poses[image_id].R @ world + model.poses[image_id].t)
        actual = frame.K @ (frame.R @ normalized + frame.t)
        np.testing.assert_allclose(actual[:2] / actual[2],
                                  (original[:2] / original[2] + .5) / 2 - .5, atol=1e-10)


def test_3dgs_rejects_empty_cloud_even_when_all_poses_are_valid(posed_manifest, tmp_path):
    manifest, _, _ = posed_manifest
    model_path = tmp_path / 'empty-model.json'
    _save_empty_model(manifest, model_path)
    with pytest.raises(ValueError):
        rendering_data.load_training_scene(model_path, manifest, resolve_render_config({'method': '3dgs'}))


def test_nerf_normalization_does_not_change_when_sparse_points_change(posed_manifest, tmp_path):
    manifest, _, _ = posed_manifest
    original = rendering_data.load_posed_dataset(manifest)
    config = resolve_render_config({'method': 'nerf', 'image_downscale': 2})
    scenes = []
    for index, positions in enumerate(([], [[0., 0., 0.]], [[1e6, -4e6, 8e6], [-8e5, 3., 9.]])):
        model = copy.deepcopy(original)
        model.points = {i + 1: Point3D(i + 1, xyz, [10, 20, 30]) for i, xyz in enumerate(positions)}
        path = tmp_path / f'model-{index}.json'
        save_reconstruction(model, path)
        scenes.append(rendering_data.load_training_scene(path, manifest, config))
    for actual in scenes[1:]:
        np.testing.assert_array_equal(actual.center, scenes[0].center)
        assert actual.scale == scenes[0].scale
        for image_id in actual.frames:
            np.testing.assert_array_equal(actual.frames[image_id].R, scenes[0].frames[image_id].R)
            np.testing.assert_array_equal(actual.frames[image_id].t, scenes[0].frames[image_id].t)


def test_nerf_coincident_camera_centers_use_documented_unit_scale(posed_manifest, tmp_path):
    manifest, document, _ = posed_manifest
    center = np.array([2., 3., 4.])
    for record in document['poses']:
        record['t'] = (-np.array(record['R']) @ center).tolist()
    manifest.write_text(json.dumps(document), encoding='utf-8')
    model_path = tmp_path / 'coincident.json'
    _save_empty_model(manifest, model_path)
    scene = rendering_data.load_training_scene(model_path, manifest, resolve_render_config({'method': 'nerf'}))
    np.testing.assert_allclose(scene.center, center)
    assert scene.scale == 1.
    assert scene.normalization_source == 'camera_centers_unit_fallback'
    assert all(np.isfinite(f.t).all() for f in scene.frames.values())
    assert all(np.allclose(-f.R.T @ f.t, 0.) for f in scene.frames.values())


@pytest.mark.parametrize('pose_count', [0, 1])
def test_insufficient_poses_remain_valid_dataset_but_cannot_train(posed_manifest, tmp_path, pose_count):
    manifest, document, _ = posed_manifest
    document['poses'] = document['poses'][:pose_count]
    manifest.write_text(json.dumps(document), encoding='utf-8')
    model = rendering_data.load_posed_dataset(manifest)
    assert len(model.poses) == pose_count and model.points == {}
    path = tmp_path / f'{pose_count}-poses.json'
    save_reconstruction(model, path)
    with pytest.raises(ValueError):
        rendering_data.load_training_scene(path, manifest, resolve_render_config({'method': 'nerf'}))


@pytest.mark.parametrize('bad', ['missing', 'invalid_rotation', 'duplicate_id',
                                  'unknown_image', 'wrong_convention', 'nonfinite_translation'])
def test_posed_dataset_rejects_invalid_pose_contract(posed_manifest, bad):
    manifest, document, _ = posed_manifest
    if bad == 'missing':
        document.pop('poses')
    elif bad == 'invalid_rotation':
        document['poses'][0]['R'][0][0] = 2.
    elif bad == 'duplicate_id':
        document['poses'].append(copy.deepcopy(document['poses'][0]))
    elif bad == 'unknown_image':
        document['poses'][0]['image_id'] = 999
    elif bad == 'wrong_convention':
        document['pose_convention'] = 'camera_to_world'
    else:
        document['poses'][0]['t'][0] = float('nan')
    manifest.write_text(json.dumps(document), encoding='utf-8')
    with pytest.raises(ValueError):
        rendering_data.load_posed_dataset(manifest)


def test_point_free_nerf_training_freezes_poses_and_reaches_student_todo(posed_manifest, tmp_path, monkeypatch):
    pytest.importorskip('torch', reason='optional render extra is not installed')
    from minicolmap.rendering import algorithms
    from minicolmap.rendering.pipeline import train
    manifest, _, _ = posed_manifest
    before = manifest.read_bytes()
    calls = []
    # Deliberate error-protocol fixture at the implemented network boundary.
    def learner_boundary(*args):
        calls.append(args)
        raise NotImplementedError('TODO N4 point-free input boundary')
    monkeypatch.setattr(algorithms, 'build_nerf', learner_boundary)
    output = tmp_path / 'nerf-point-free'
    with pytest.raises(NotImplementedError, match='TODO N4'):
        train(None, manifest, output, {'method': 'nerf', 'iterations': 1})
    assert calls and manifest.read_bytes() == before
    frozen = load_reconstruction(output / 'source_model.json')
    assert frozen.points == {} and set(frozen.poses) == {3, 11, 19}
    status = json.loads((output / 'run_status.json').read_text(encoding='utf-8'))
    assert status['status'] == 'todo'
    prepared = json.loads((output / 'training_scene.json').read_text(encoding='utf-8'))
    assert prepared['source_points'] == 0
    assert prepared['normalization']['source'] == 'camera_centers'
    assert not (output / 'summary.json').exists()


def test_cli_nerf_accepts_dataset_without_model(posed_manifest, tmp_path, monkeypatch):
    from minicolmap.cli import main
    from minicolmap.rendering import pipeline
    manifest, _, _ = posed_manifest
    calls = []
    def trainer(model, dataset, output, config):
        calls.append((model, dataset, output, config))
        return {'pipeline': 'nerf', 'source_points': 0}
    monkeypatch.setattr(pipeline, 'train', trainer)
    assert main(['train', '--dataset', str(manifest), '--output', str(tmp_path / 'cli'), '--method', 'nerf']) == 0
    assert calls[0][0] is None
    assert calls[0][1] == manifest and calls[0][3]['method'] == 'nerf'
