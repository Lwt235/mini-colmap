"""Solver-wrapper contracts, rollback behavior and explicit error propagation."""
import copy
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip('scipy')
from scipy.spatial.transform import Rotation

from minicolmap import optimization
from minicolmap.metrics import reconstruction_summary
from minicolmap.models import Camera, ImageRecord, Point3D, Pose, Reconstruction


SETTINGS = {'loss': 'linear', 'f_scale': 1.0, 'max_nfev': 100}


def make_model(variable_camera=False):
    """Known pinhole observations with disturbed initial structure and stable IDs."""
    K = np.array([[800., 0., 320.], [0., 790., 240.], [0., 0., 1.]])
    truth = np.array([
        [-.7, -.3, 4.5], [.5, .3, 5.], [.2, -.4, 6.],
        [-.4, .5, 5.5], [.8, -.1, 7.], [-.2, .1, 7.5],
    ])
    poses = {11: Pose(np.eye(3), np.zeros(3)),
             29: Pose(Rotation.from_rotvec([0., .08, 0.]).as_matrix(), [-1., 0., 0.])}
    if variable_camera:
        poses[103] = Pose(Rotation.from_rotvec([.03, -.07, .01]).as_matrix(), [-.4, .05, .1])
    images = {}
    for image_id, pose in poses.items():
        h = (truth @ pose.R.T + pose.t) @ K.T
        images[image_id] = ImageRecord(image_id, f'{image_id}.jpg', 7, h[:, :2] / h[:, 2:3])
    initial = truth + np.array([.03, -.02, .2])
    points = {101 + 13 * j: Point3D(101 + 13 * j, point, [10, 20, 30],
              tuple((image_id, j) for image_id in reversed(list(poses))))
              for j, point in enumerate(initial)}
    model = Reconstruction({7: Camera(7, 640, 480, K)}, images, copy.deepcopy(poses), points)
    return model, truth, poses


def fake_result(x, success=True):
    return SimpleNamespace(x=np.asarray(x), success=success, nfev=1, message='contract fake')


def assert_unchanged(model, original, pose_collection, point_collection):
    assert model.poses is pose_collection and model.points is point_collection
    for image_id, pose in original.poses.items():
        np.testing.assert_array_equal(model.poses[image_id].R, pose.R)
        np.testing.assert_array_equal(model.poses[image_id].t, pose.t)
    for point_id, point in original.points.items():
        np.testing.assert_array_equal(model.points[point_id].xyz, point.xyz)


def test_packing_sparse_dependencies_and_exact_anchor_preservation(monkeypatch):
    import scipy.optimize

    model, _, _ = make_model(variable_camera=True)
    original = copy.deepcopy(model)
    captured = {}

    def residual_spy(r, t, p, K, camera_indices, point_indices, pixels):
        captured.update(r=r.copy(), t=t.copy(), p=p.copy(), ci=camera_indices.copy(),
                        pi=point_indices.copy(), pixels=pixels.copy())
        return np.zeros(2 * len(pixels))

    def solver_spy(fun, x0, **kwargs):
        # Six variable-pose values followed by all points in stable ID order.
        np.testing.assert_allclose(x0[:3], Rotation.from_matrix(original.poses[103].R).as_rotvec())
        np.testing.assert_array_equal(x0[3:6], original.poses[103].t)
        np.testing.assert_array_equal(x0[6:].reshape(-1, 3),
                                      [original.points[i].xyz for i in sorted(original.points)])
        assert kwargs['method'] == 'trf'
        fun(x0)
        pattern = kwargs['jac_sparsity'].toarray()
        assert pattern.shape == (36, 24)  # 18 observations; 6 pose + 18 point values.
        for obs_idx, (camera_idx, point_idx) in enumerate(zip(captured['ci'], captured['pi'])):
            expected = set(range(6 + 3 * point_idx, 9 + 3 * point_idx))
            if camera_idx == 2:
                expected.update(range(6))
            for row in (2 * obs_idx, 2 * obs_idx + 1):
                assert set(np.flatnonzero(pattern[row])) == expected
        return fake_result(x0)

    monkeypatch.setattr(optimization, 'bundle_residuals', residual_spy)
    monkeypatch.setattr(scipy.optimize, 'least_squares', solver_spy)
    report = optimization.refine_model(model, [29, 11], SETTINGS)
    assert report['accepted'] and report['rejection_reason'] is None
    for image_id in (11, 29):
        np.testing.assert_array_equal(model.poses[image_id].R, original.poses[image_id].R)
        np.testing.assert_array_equal(model.poses[image_id].t, original.poses[image_id].t)
    np.testing.assert_array_equal(captured['ci'], np.tile([2, 1, 0], 6))
    np.testing.assert_array_equal(captured['pi'], np.repeat(np.arange(6), 3))


@pytest.mark.parametrize('fault', ['nan', 'inf', 'zero_depth', 'negative_depth', 'wrong_shape', 'bad_dtype',
                                 'worse_error'])
def test_invalid_candidate_rolls_back_and_reports_rejection(monkeypatch, fault):
    import scipy.optimize

    model, _, _ = make_model()
    original = copy.deepcopy(model)
    pose_collection, point_collection = model.poses, model.points

    def solver(fun, x0, **kwargs):
        x = x0.copy()
        if fault == 'wrong_shape':
            x = x[:-1]
        elif fault == 'bad_dtype':
            x = ['invalid'] * len(x)
        elif fault == 'worse_error':
            x[0] += 10.
        else:
            x[2] = {'nan': np.nan, 'inf': np.inf, 'zero_depth': 0., 'negative_depth': -5.}[fault]
        return fake_result(x)

    monkeypatch.setattr(scipy.optimize, 'least_squares', solver)
    report = optimization.refine_model(model, [11, 29], SETTINGS)
    assert not report['accepted'] and report['rejection_reason']
    assert_unchanged(model, original, pose_collection, point_collection)


@pytest.mark.parametrize('fault', ['nonfinite', 'wrong_shape'])
def test_invalid_residual_trial_is_rejected_without_model_mutation(monkeypatch, fault):
    model, _, _ = make_model()
    original = copy.deepcopy(model)
    poses, points = model.poses, model.points

    def invalid_residual(r, t, p, K, ci, pi, pixels):
        return np.full(2 * len(pixels), np.nan) if fault == 'nonfinite' else np.zeros(len(pixels))

    monkeypatch.setattr(optimization, 'bundle_residuals', invalid_residual)
    report = optimization.refine_model(model, [11, 29], SETTINGS)
    assert not report['accepted'] and 'finite vector' in report['rejection_reason']
    assert_unchanged(model, original, poses, points)


def test_rejected_trial_rolls_back_but_unfinished_b1_stays_visible(monkeypatch):
    import scipy.optimize

    model, _, _ = make_model()
    original = copy.deepcopy(model)
    poses, points = model.poses, model.points

    def solver(fun, x0, **kwargs):
        raise ValueError('trial has zero camera depth')

    monkeypatch.setattr(scipy.optimize, 'least_squares', solver)
    report = optimization.refine_model(model, [11, 29], SETTINGS)
    assert not report['accepted'] and 'zero camera depth' in report['rejection_reason']
    assert_unchanged(model, original, poses, points)

    # Deliberate error-protocol fixture; B1 is implemented in production.
    def unfinished(fun, x0, **kwargs):
        raise NotImplementedError('TODO B1')

    monkeypatch.setattr(scipy.optimize, 'least_squares', unfinished)
    with pytest.raises(NotImplementedError, match='TODO B1'):
        optimization.refine_model(model, [11, 29], SETTINGS)


@pytest.mark.parametrize('fault', ['negative_depth', 'zero_depth', 'nonfinite', 'missing_track'])
def test_invalid_initial_model_is_rejected_before_solver(monkeypatch, fault):
    import scipy.optimize

    model, _, _ = make_model()
    if fault == 'missing_track':
        model.points[101].observations = ()
    else:
        model.points[101].xyz[2] = {'negative_depth': -5., 'zero_depth': 0., 'nonfinite': np.nan}[fault]

    def unexpected(*args, **kwargs):
        pytest.fail('Invalid initial model must not reach the optimizer')

    monkeypatch.setattr(scipy.optimize, 'least_squares', unexpected)
    with pytest.raises(ValueError):
        optimization.refine_model(model, [11, 29], SETTINGS)


def library_projection_residual(r, t, p, K, camera_indices, point_indices, observed):
    """Independent OpenCV projection oracle for optimizer-wrapper integration."""
    cv2 = pytest.importorskip('cv2')
    predicted = []
    for camera_idx, point_idx in zip(camera_indices, point_indices):
        xy, _ = cv2.projectPoints(p[point_idx:point_idx + 1], r[camera_idx], t[camera_idx], K, None)
        predicted.append(xy.reshape(2))
    return (np.asarray(predicted) - observed).ravel()


@pytest.mark.parametrize('variable_camera', [False, True])
def test_real_sparse_solver_reduces_pixel_error_and_keeps_anchors(monkeypatch, variable_camera):
    model, truth, true_poses = make_model(variable_camera)
    if variable_camera:
        model.poses[103] = Pose(Rotation.from_rotvec([.04, -.04, .02]).as_matrix(), [-.38, .06, .08])
    anchors = copy.deepcopy({i: model.poses[i] for i in (11, 29)})
    before = reconstruction_summary(model)['mean_reprojection_error_px']
    monkeypatch.setattr(optimization, 'bundle_residuals', library_projection_residual)
    report = optimization.refine_model(model, [11, 29], SETTINGS)
    assert report['accepted']
    assert reconstruction_summary(model)['mean_reprojection_error_px'] < before * .01
    np.testing.assert_allclose([model.points[i].xyz for i in sorted(model.points)], truth, atol=1e-3)
    for image_id, pose in anchors.items():
        np.testing.assert_array_equal(model.poses[image_id].R, pose.R)
        np.testing.assert_array_equal(model.poses[image_id].t, pose.t)
    if variable_camera:
        np.testing.assert_allclose(model.poses[103].R, true_poses[103].R, atol=1e-3)
        np.testing.assert_allclose(model.poses[103].t, true_poses[103].t, atol=1e-3)
