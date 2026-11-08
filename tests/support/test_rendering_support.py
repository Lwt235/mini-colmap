"""Rendering engineering checks; protocol doubles below are NOT learning algorithms."""
from __future__ import annotations

import hashlib
import json

import cv2
import numpy as np
import pytest

from minicolmap.io import save_reconstruction
from minicolmap.models import Camera, ImageRecord, Point3D, Pose, Reconstruction
from minicolmap.rendering.config import capabilities, resolve_render_config, validate_gaussian_count
from minicolmap.rendering.data import load_training_scene, scaled_intrinsics, split_views


@pytest.fixture
def appearance_scene(tmp_path):
    camera = Camera(7, 8, 6, [[10, .2, 3], [0, 11, 2], [0, 0, 1]])
    poses = {3: Pose(np.eye(3), np.zeros(3)),
             11: Pose(np.eye(3), [-.5, 0, 0]),
             19: Pose([[0, -1., 0], [1., 0, 0], [0, 0, 1]], [0, -.7, 0])}
    points = np.array([[-.2, .1, 3], [.2, -.1, 4]])
    images = {}
    records = []
    for image_id in (3, 11, 19, 29):
        pose = poses.get(image_id, Pose(np.eye(3), np.zeros(3)))
        h = (points @ pose.R.T + pose.t) @ camera.K.T
        images[image_id] = ImageRecord(image_id, f'{image_id}.png', 7, h[:, :2] / h[:, 2:3])
        rgb = np.full((6, 8, 3), [image_id, 90, 190], dtype=np.uint8)
        ok, encoded = cv2.imencode('.png', cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
        assert ok
        encoded.tofile(tmp_path / f'{image_id}.png')
        records.append({'id': image_id, 'name': f'{image_id}.png', 'camera_id': 7, 'path': f'{image_id}.png'})
    model = Reconstruction({7: camera}, images, poses,
        {i + 1: Point3D(i + 1, xyz, [10, 80, 200], tuple((j, i) for j in poses))
         for i, xyz in enumerate(points)})
    model_path = tmp_path / 'model.json'
    dataset_path = tmp_path / 'dataset.json'
    save_reconstruction(model, model_path)
    dataset_path.write_text(json.dumps({'cameras': [{'id': 7, 'width': 8, 'height': 6, 'K': camera.K.tolist()}],
                                      'images': records}))
    return model_path, dataset_path, model


@pytest.mark.parametrize('override', [
    {'method': 'fake'}, {'device': 'metal'}, {'iterations': True}, {'iterations': 0},
    {'learning_rate': float('nan')}, {'near': 2, 'far': 1}, {'unknown': 1},
    {'image_downscale': 0}, {'holdout_every': 1}, {'samples_per_ray': 1},
    {'gaussian': {'initial_opacity': 1}}, {'gaussian': {'max_gaussians': 0}},
    {'nerf': {'levels_xyz': -1}}, {'nerf': {'unknown': 2}},
])
def test_render_config_rejects_invalid_settings(override):
    with pytest.raises(ValueError):
        resolve_render_config(override)


def test_config_is_independent_and_capabilities_do_not_require_cuda():
    config = resolve_render_config({'method': '3dgs', 'gaussian': {'max_gaussians': 12}})
    config['nerf']['levels_xyz'] = 999
    assert resolve_render_config()['nerf']['levels_xyz'] == 6
    assert capabilities()['methods'] == ['nerf', '3dgs']


@pytest.mark.parametrize('count', [1, 100001, 1000000, 5000000])
def test_million_gaussian_limits_match_configuration_and_live_checkpoint_validation(count):
    settings = resolve_render_config({'method': '3dgs', 'device': 'cuda',
        'gaussian': {'backend': 'gsplat', 'max_gaussians': count}})
    assert settings['gaussian']['max_gaussians'] == validate_gaussian_count(count) == count
    assert capabilities()['limits']['max_gaussians'] == 5000000
    assert resolve_render_config()['gaussian']['max_gaussians'] == 512


@pytest.mark.parametrize('count', [0, -1, True, 1.5, 5000001, float('inf'), float('nan')])
def test_gaussian_count_limits_still_reject_invalid_configuration_and_checkpoint_counts(count):
    with pytest.raises(ValueError, match='max_gaussians'):
        validate_gaussian_count(count)
    with pytest.raises(ValueError, match='max_gaussians'):
        resolve_render_config({'gaussian': {'max_gaussians': count}})


def test_view_split_uses_stable_ids_not_positions_and_preserves_holdout():
    assert split_views([19, 3, 11, 29], 2) == ([3, 19], [11, 29])
    assert split_views([19, 3], 8) == ([3], [19])
    with pytest.raises(ValueError):
        split_views([3], 4)


@pytest.mark.parametrize('iterations', [6, 8])
def test_short_adaptive_training_leaves_a_view_cycle_after_opacity_reset(appearance_scene, monkeypatch, iterations):
    pytest.importorskip('torch')
    from minicolmap.rendering import pipeline
    original = pipeline.reset_opacity
    last_step, resets = [0], []

    def record_reset(state, optimizer):
        resets.append(last_step[0] + 1)
        original(state, optimizer)

    def observe(event):
        if event.get('kind') == 'progress':
            last_step[0] = event['completed']

    monkeypatch.setattr(pipeline, 'reset_opacity', record_reset)
    model, dataset, _ = appearance_scene
    summary = pipeline.train(model, dataset, model.parent / f'short-{iterations}', {
        'method': '3dgs', 'iterations': iterations, 'image_downscale': 2, 'background': 'black',
        'holdout_every': 2, 'preview_every': iterations,
        'gaussian': {'backend': 'reference', 'density_strategy': 'adaptive',
                     'densify_every': 0, 'densify_start': 0, 'densify_stop': 100,
                     'opacity_reset_every': 3}}, on_event=observe)
    assert summary['train_ids'] == [3, 19]
    assert resets == [3]  # Step 6 must not erase opacity just before completion.
    assert summary['step'] == iterations


def test_resize_intrinsics_follow_half_pixel_mapping_including_skew():
    K = np.array([[10., .7, 3], [0, 12, 2], [0, 0, 1]])
    scaled = scaled_intrinsics(K, (8, 6), (3, 2))
    point = np.array([.4, -.2, 2.])
    old = K @ point
    new = scaled @ point
    np.testing.assert_allclose(new[:2] / new[2],
        (old[:2] / old[2] + .5) * [3/8, 2/6] - .5)


def test_preparation_preserves_projection_and_reads_only_registered_images(appearance_scene):
    model_path, dataset_path, model = appearance_scene
    original = model_path.read_bytes()
    scene = load_training_scene(model_path, dataset_path,
                                resolve_render_config({'image_downscale': 2, 'holdout_every': 2}))
    assert scene.train_ids == [3, 19] and scene.validation_ids == [11]
    assert set(scene.frames) == {3, 11, 19}
    assert scene.frames[3].rgb.shape == (3, 4, 3)
    np.testing.assert_allclose(scene.frames[3].rgb[0, 0], [3/255, 90/255, 190/255])
    for image_id, frame in scene.frames.items():
        p = scene.points @ frame.R.T + frame.t
        pixels = p @ frame.K.T
        np.testing.assert_allclose(pixels[:, :2] / pixels[:, 2:3],
            (model.images[image_id].keypoints + .5) / 2 - .5, atol=1e-6)
    assert model_path.read_bytes() == original


@pytest.mark.parametrize('change', ['identity', 'intrinsics', 'dimensions', 'missing_photo'])
def test_preparation_rejects_mismatched_source(appearance_scene, change):
    model_path, dataset_path, model = appearance_scene
    data = json.loads(dataset_path.read_text())
    if change == 'identity':
        data['images'][0]['name'] = 'other.png'
    elif change == 'intrinsics':
        data['cameras'][0]['K'][0][0] += 1
    elif change == 'dimensions':
        ok, encoded = cv2.imencode('.png', np.zeros((7, 8, 3), dtype=np.uint8))
        assert ok
        encoded.tofile(dataset_path.parent / '3.png')
    else:
        (dataset_path.parent / '3.png').unlink()
    dataset_path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        load_training_scene(model_path, dataset_path, resolve_render_config())


def install_protocol_doubles(monkeypatch, method):
    """Nonphysical constant fixtures test persistence, not rendering accuracy."""
    torch = pytest.importorskip('torch', reason='optional render extra is not installed')
    from minicolmap.rendering import algorithms as learner

    class ConstantField(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.color = torch.nn.Parameter(torch.zeros(3))

        def forward(self, xyz, directions):
            return self.color.sigmoid().expand(len(xyz), 3), torch.ones(len(xyz), device=xyz.device)

    monkeypatch.setattr(learner, 'build_nerf', lambda *args: ConstantField())
    monkeypatch.setattr(learner, 'generate_rays', lambda K, R, t, px:
        (torch.zeros((len(px), 3), device=K.device), torch.ones((len(px), 3), device=K.device)))
    monkeypatch.setattr(learner, 'volume_render', lambda rgb, *args: {'rgb': rgb.mean(dim=1)})
    monkeypatch.setattr(learner, 'initialize_gaussians', lambda points, colors, *args:
        {'means': points.clone(), 'log_scales': torch.zeros_like(points),
         'quaternions': torch.zeros((len(points), 4)),
         'sh_coeffs': torch.zeros((len(points), (args[-1] + 1)**2, 3)),
         'opacity_logits': torch.zeros(len(points))})
    monkeypatch.setattr(learner, 'gaussian_covariance', lambda logs, quats:
        torch.eye(3).expand(len(logs), 3, 3))
    monkeypatch.setattr(learner, 'evaluate_sh', lambda coefficients, directions, degree:
        coefficients[:, 0].sigmoid())
    monkeypatch.setattr(learner, 'project_gaussians', lambda means, *args:
        {'means2d': torch.zeros((len(means), 2)), 'cov2d': torch.eye(2).expand(len(means), 2, 2),
         'depths': torch.ones(len(means)), 'visible': torch.ones(len(means), dtype=torch.bool)})
    monkeypatch.setattr(learner, 'splat_gaussians', lambda xy, cov, depth, colors, opacity, w, h, bg:
        {'rgb': colors.mean(dim=0).expand(h, w, 3)})
    return torch


@pytest.mark.parametrize('method,extra', [('nerf',{}), ('3dgs',{}),
    ('3dgs',{'gaussian': {'sh_degree': 2, 'sh_degree_interval': 1}}),
    ('nerf',{'background':'black','stratified_sampling':True,'ray_sampling':'all_images'})])
def test_training_checkpoint_preview_and_camera_render_protocol(appearance_scene, monkeypatch, method, extra):
    torch = install_protocol_doubles(monkeypatch, method)
    from minicolmap.rendering.pipeline import train, render_checkpoint
    model_path, dataset_path, model = appearance_scene
    source_hash = hashlib.sha256(model_path.read_bytes()).hexdigest()
    output = model_path.parent / method
    events = []
    config = resolve_render_config({'method': method, 'iterations': 2, 'preview_every': 1,
                                  'image_downscale': 2, 'rays_per_batch': 3, **extra})
    summary = train(model_path, dataset_path, output, config, on_event=events.append)
    assert summary['step'] == 2 and summary['train_ids'] == [3, 11] and summary['validation_ids'] == [19]
    assert summary['metrics']['psnr_scope'] == 'mean_validation_view_mse'
    assert len(summary['previews']) == 2
    assert all((output / item['path']).is_file() for item in summary['previews'])
    saved = torch.load(output / 'checkpoint.pt', weights_only=True)
    assert saved['step'] == 2 and saved['pipeline'] == method
    assert json.loads((output / 'run_status.json').read_text())['status'] == 'completed'
    assert any(e['kind'] == 'preview' and 'metrics' in e for e in events)
    assert hashlib.sha256(model_path.read_bytes()).hexdigest() == source_hash
    preview = render_checkpoint(output, image_id=3)
    assert (output / preview['path']).exists()
    assert (output / preview['reference_path']).exists()
    reference = cv2.imdecode(np.fromfile(output / preview['reference_path'], dtype=np.uint8), cv2.IMREAD_COLOR)
    np.testing.assert_array_equal(reference[0, 0], [190, 90, 3])
    custom = render_checkpoint(output, camera={'width': 4, 'height': 3, 'K': model.cameras[7].K.tolist(),
        'R': model.poses[19].R.tolist(), 't': model.poses[19].t.tolist()})
    assert (output / custom['path']).exists()
    if method == '3dgs':
        data = np.load(output / 'gaussians.npz')
        np.testing.assert_allclose(data['means'], np.array([p.xyz for p in model.points.values()]), atol=1e-6)
        assert saved['schema_version'] == 2
        expected_degree = config['gaussian']['sh_degree']
        assert saved['active_sh_degree'] == expected_degree
        np.testing.assert_array_equal(data['sh_coeffs'], saved['state']['sh_coeffs'])
        assert data['sh_coeffs'].shape == (2, (expected_degree+1)**2, 3)
        assert data['active_sh_degree'] == expected_degree
        assert str(data['sh_direction']) == 'camera_to_gaussian_world'
        assert 'colors' not in data.files, 'A single RGB must not replace view-dependent appearance'
    with pytest.raises(ValueError, match='输出已存在'):
        train(model_path, dataset_path, output, config)
    with pytest.raises(ValueError):
        render_checkpoint(output, camera={'width': 513, 'height': 1, 'K': np.eye(3), 'R': np.eye(3), 't': [0, 0, 0]})


@pytest.mark.parametrize('method', ['nerf', '3dgs'])
def test_training_publishes_fixed_reference_once_across_preview_intervals(appearance_scene, monkeypatch, method):
    install_protocol_doubles(monkeypatch, method)
    from minicolmap.rendering import pipeline
    model_path, dataset_path, _ = appearance_scene
    published = []
    original = pipeline._write_image

    def writer(path, rgb):
        if path.name.startswith('reference-'):
            if path.exists():
                raise PermissionError('The HTTP reader keeps the reference open')
            published.append(path)
        return original(path, rgb)

    monkeypatch.setattr(pipeline, '_write_image', writer)
    output = model_path.parent / f'locked-reference-{method}'
    settings = resolve_render_config({'method': method, 'iterations': 3, 'preview_every': 1,
                                      'image_downscale': 1, 'rays_per_batch': 3})
    summary = pipeline.train(model_path, dataset_path, output, settings)
    assert summary['step'] == 3 and len(summary['previews']) == 3
    assert len(published) == 1
    assert json.loads((output / 'run_status.json').read_text())['status'] == 'completed'


def test_unfinished_math_is_persisted_as_todo_not_success(appearance_scene, monkeypatch):
    pytest.importorskip('torch', reason='optional render extra is not installed')
    from minicolmap.rendering import algorithms as learner
    from minicolmap.rendering.pipeline import train
    # Protocol fixture verifies todo persistence without changing production N4.
    def unfinished(*args, **kwargs):
        raise NotImplementedError('TODO N4 test boundary')
    monkeypatch.setattr(learner, 'build_nerf', unfinished)
    model_path, dataset_path, _ = appearance_scene
    output = model_path.parent / 'todo'
    with pytest.raises(NotImplementedError, match='TODO N4'):
        train(model_path, dataset_path, output)
    status = json.loads((output / 'run_status.json').read_text())
    assert status['status'] == 'todo' and not (output / 'summary.json').exists()


def test_cuda_request_fails_explicitly_when_unavailable(monkeypatch):
    torch = pytest.importorskip('torch', reason='optional render extra is not installed')
    from minicolmap.rendering.pipeline import _torch
    monkeypatch.setattr(torch.cuda, 'is_available', lambda: False)
    with pytest.raises(ValueError, match='CUDA'):
        _torch('cuda')


def test_unfinished_sh_is_reported_as_s6_todo(appearance_scene, monkeypatch):
    install_protocol_doubles(monkeypatch, '3dgs')
    from minicolmap.rendering import algorithms as learner
    from minicolmap.rendering.pipeline import train
    # Protocol fixture verifies explicit SH error reporting, not unfinished S6.
    def unfinished(*args):
        raise NotImplementedError('TODO S6 test boundary')
    monkeypatch.setattr(learner, 'evaluate_sh', unfinished)
    model_path, dataset_path, _ = appearance_scene
    output = model_path.parent / 'sh-todo'
    with pytest.raises(NotImplementedError, match='TODO S6'):
        train(model_path, dataset_path, output, {'method': '3dgs', 'iterations': 1})
    assert json.loads((output / 'run_status.json').read_text())['status'] == 'todo'
    assert not (output / 'summary.json').exists()


def test_legacy_rgb_only_gaussian_checkpoint_is_not_mistaken_for_sh(appearance_scene, monkeypatch):
    torch = install_protocol_doubles(monkeypatch, '3dgs')
    from minicolmap.rendering.pipeline import train, render_checkpoint
    model_path, dataset_path, _ = appearance_scene
    output = model_path.parent / 'legacy'
    train(model_path, dataset_path, output, {'method': '3dgs', 'iterations': 1})
    checkpoint = torch.load(output / 'checkpoint.pt', weights_only=True)
    checkpoint['schema_version'] = 1
    checkpoint['state']['color_logits'] = checkpoint['state'].pop('sh_coeffs')[:, 0]
    torch.save(checkpoint, output / 'checkpoint.pt')
    with pytest.raises(ValueError, match='RGB-only'):
        render_checkpoint(output, image_id=3)


def test_sampler_covers_the_configured_near_far_interval(monkeypatch):
    torch = install_protocol_doubles(monkeypatch, 'nerf')
    from minicolmap.rendering import algorithms as learner
    from minicolmap.rendering.pipeline import _nerf_rays
    captured = {}
    def capture(rgb, sigma, positions, far, background):
        captured.update(t=positions.detach(), far=far)
        return {'rgb': rgb.mean(dim=1)}
    monkeypatch.setattr(learner, 'volume_render', capture)
    config = resolve_render_config({'near': 1., 'far': 5., 'samples_per_ray': 4})
    field = learner.build_nerf(0, 0, 4)
    _nerf_rays(field, torch.eye(3), torch.eye(3), torch.zeros(3), torch.zeros((2, 2)), config, torch)
    torch.testing.assert_close(captured['t'], torch.tensor([[1., 2, 3, 4], [1., 2, 3, 4]]))
    assert captured['far'] == 5.


def test_no_supported_training_camera_fails_without_using_validation(appearance_scene, monkeypatch):
    torch = install_protocol_doubles(monkeypatch, '3dgs')
    from minicolmap.rendering import algorithms as learner
    from minicolmap.rendering.pipeline import train
    original = learner.initialize_gaussians
    def behind(*args):
        state = original(*args)
        state['means'][:, 2] = -100
        return state
    monkeypatch.setattr(learner, 'initialize_gaussians', behind)
    model_path, dataset_path, _ = appearance_scene
    events = []
    with pytest.raises(ValueError, match='所有训练视角'):
        train(model_path, dataset_path, model_path.parent / 'behind',
              {'method': '3dgs', 'iterations': 1}, on_event=events.append)
    assert any(e.get('kind') == 'warning' for e in events)
    assert not any(e.get('kind') == 'complete' for e in events)


def test_camera_normalization_ignores_far_points_and_heldout_camera(appearance_scene, tmp_path):
    model_path, dataset_path, model = appearance_scene
    config = resolve_render_config(dict(method='3dgs', holdout_every=2, image_downscale=2,
        gaussian=dict(scene_normalization='camera_extent')))
    first = load_training_scene(model_path, dataset_path, config)
    model.points[99999] = Point3D(99999, [1e9, 1e9, 1e9], [10, 20, 30])
    heldout = first.validation_ids[0]
    model.poses[heldout] = Pose(model.poses[heldout].R, [1e5, 1e5, 1e5])
    changed_path = tmp_path / 'outlier.json'
    save_reconstruction(model, changed_path)
    second = load_training_scene(changed_path, dataset_path, config)
    assert first.normalization_source == second.normalization_source == 'training_camera_extent'
    np.testing.assert_array_equal(first.center, second.center)
    assert first.scale == second.scale
    for image_id in first.train_ids:
        a, b = first.frames[image_id], second.frames[image_id]
        np.testing.assert_array_equal(a.t, b.t)
        points = first.points @ a.R.T + a.t
        pixels = points @ a.K.T
        np.testing.assert_allclose(pixels[:, :2] / pixels[:, 2:3],
            (model.images[image_id].keypoints + .5) / 2 - .5, atol=1e-6)


def test_empty_density_update_stops_with_diagnosis_before_publishing_checkpoint(appearance_scene, tmp_path):
    pytest.importorskip('torch')
    from minicolmap.rendering.pipeline import train
    model_path, dataset_path, _ = appearance_scene
    original = model_path.read_bytes()
    output = tmp_path / 'all-pruned'
    with pytest.raises(ValueError, match='未应用该次裁剪'):
        train(model_path, dataset_path, output, dict(method='3dgs', iterations=2,
            gaussian=dict(density_strategy='adaptive', densify_start=1, densify_stop=3,
                          densify_every=1, min_opacity=.99, opacity_reset_every=0)))
    diagnosis = json.loads((output / 'density_failure.json').read_text())
    assert diagnosis['step'] == 1 and diagnosis['before'] == diagnosis['opacity_rejected'] == 2
    assert not (output / 'checkpoint.pt').exists()
    assert model_path.read_bytes() == original


def test_large_checkpoint_camera_preview_resizes_intrinsics_without_retraining(appearance_scene, monkeypatch):
    torch = install_protocol_doubles(monkeypatch, '3dgs')
    from minicolmap.rendering import pipeline
    model_path, dataset_path, _ = appearance_scene
    output = model_path.parent / 'large-preview'
    pipeline.train(model_path, dataset_path, output, dict(method='3dgs', iterations=1))
    checkpoint = torch.load(output / 'checkpoint.pt', weights_only=True)
    frame = next(f for f in checkpoint['frames'] if f['image_id'] == 3)
    frame.update(width=1200, height=800, K=[[800., 0, 599.5], [0, 800., 399.5], [0, 0, 1]])
    torch.save(checkpoint, output / 'checkpoint.pt')
    before = (output / 'checkpoint.pt').read_bytes()
    seen = []
    def render(state, actual, config, module, **kwargs):
        seen.append(actual)
        return torch.zeros((actual['height'], actual['width'], 3))
    monkeypatch.setattr(pipeline, '_render_gaussian_preview', render)
    preview = pipeline.render_checkpoint(output, image_id=3)
    actual = seen[0]
    assert (actual['width'], actual['height']) == (512, 341)
    np.testing.assert_allclose(actual['K'], scaled_intrinsics(np.array(frame['K']), (1200, 800), (512, 341)))
    assert (output / 'checkpoint.pt').read_bytes() == before
    pixels = cv2.imread(str(output / preview['path']))
    reference = cv2.imread(str(output / preview['reference_path']))
    assert pixels.shape == reference.shape == (341, 512, 3)


def test_finetuning_loads_matching_checkpoint_without_reinitializing(appearance_scene, monkeypatch):
    torch = install_protocol_doubles(monkeypatch, '3dgs')
    from minicolmap.rendering import pipeline
    model_path, dataset_path, _ = appearance_scene
    base = model_path.parent / 'original'
    pipeline.train(model_path, dataset_path, base, dict(method='3dgs', iterations=1))
    original = (base / 'checkpoint.pt').read_bytes()
    def forbidden(*args, **kwargs):
        raise AssertionError('warm start must not initialize sparse seeds')
    monkeypatch.setattr(pipeline.learner, 'initialize_gaussians', forbidden)
    output = model_path.parent / 'finetuned'
    result = pipeline.train(model_path, dataset_path, output, dict(method='3dgs', iterations=1),
                            initial_checkpoint=base / 'checkpoint.pt')
    assert result['warm_start']['source_step'] == 1
    assert json.loads((output / 'initialization.json').read_text())['scale_mode'] == 'checkpoint'
    checkpoint = torch.load(output / 'checkpoint.pt', weights_only=True)
    assert checkpoint['warm_start']['source_step'] == 1
    assert (base / 'checkpoint.pt').read_bytes() == original
    with pytest.raises(ValueError, match='训练/验证划分'):
        pipeline.train(model_path, dataset_path, model_path.parent / 'wrong-split',
                       dict(method='3dgs', iterations=1, holdout_every=2), initial_checkpoint=base / 'checkpoint.pt')
