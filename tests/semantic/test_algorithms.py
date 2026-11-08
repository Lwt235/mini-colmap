"""L1–L7 acceptance tests for semantic algorithms, gradients and input contracts."""
import numpy as np
import pytest
import torch

from minicolmap.semantics import algorithms as a


def test_l1_small_objects_survive_containment_and_keep_original_ids():
    masks = np.zeros((3, 4, 4), bool)
    masks[0] = True
    masks[1, 1:3, 1:3] = True
    masks[2, 0, 0] = True
    result = a.masks_to_regions(masks, np.array([.9, .8, .1]), min_area=2, min_quality=.7)
    expected = np.zeros((4, 4), np.int32)
    expected[1:3, 1:3] = 1
    np.testing.assert_array_equal(result, expected)
    assert result.dtype == np.int32


def test_l1_ties_empty_input_and_no_mutation():
    masks = np.ones((2, 2, 2), bool)
    before = masks.copy()
    result = a.masks_to_regions(masks, np.array([.8, .9]), min_area=1)
    assert np.all(result == 1)
    np.testing.assert_array_equal(masks, before)
    empty = a.masks_to_regions(np.empty((0, 2, 3), bool), np.empty(0), min_area=1)
    np.testing.assert_array_equal(empty, np.full((2, 3), -1, np.int32))


def test_l1_rejects_incompatible_mask_metadata():
    with pytest.raises(ValueError):
        a.masks_to_regions(np.zeros((2, 3, 4), bool), np.ones(3))


def test_l2_autoencoder_shapes_seed_dtype_and_gradients():
    torch.manual_seed(7)
    model = a.build_autoencoder(8, 2).double()
    x = torch.randn(2, 3, 8, dtype=torch.float64, requires_grad=True)
    z = model.encode(x)
    assert z.shape == (2, 3, 2)
    y = model(x)
    assert y.shape == x.shape and y.dtype == x.dtype
    torch.testing.assert_close(y, model.decode(z))
    y.square().mean().backward()
    assert x.grad is not None and torch.isfinite(x.grad).all()
    assert any(p.grad is not None and p.grad.abs().sum() > 0 for p in model.parameters())


def test_l2_autoencoder_can_fit_a_small_low_dimensional_manifold():
    torch.manual_seed(0)
    model = a.build_autoencoder(8, 2)
    latent = torch.randn(32, 2)
    # Independent rank-2 fixture, without using the encoder under test.
    target = latent @ torch.tensor([[1., 0., 1., -1., .5, 0., .2, .8], [0., 1., -1., 1., 0., .5, .3, -.7]])
    optimizer = torch.optim.Adam(model.parameters(), lr=.003)
    before = float((model(target) - target).square().mean().detach())
    for _ in range(200):
        optimizer.zero_grad()
        loss = (model(target) - target).square().mean()
        loss.backward()
        optimizer.step()
    after = float((model(target) - target).square().mean().detach())
    assert after < before * .2, (before, after)


def test_l3_loss_preserves_magnitude_and_direction():
    target = torch.tensor([[1., 0.], [0., 1.]])
    prediction = (2 * target).requires_grad_()
    loss = a.reconstruction_loss(prediction, target)
    torch.testing.assert_close(loss, torch.tensor(.5))  # MSE=.5, cosine loss=0
    loss.backward()
    assert prediction.grad is not None and prediction.grad.abs().sum() > 0
    torch.testing.assert_close(a.reconstruction_loss(target, target), torch.tensor(0.))


def test_l4_alpha_normalization_and_zero_coverage_have_correct_gradients():
    weighted = torch.tensor([[[.5, -.25], [3., 4.]]], dtype=torch.float64, requires_grad=True)
    alpha = torch.tensor([[[.25], [0.]]], dtype=torch.float64)
    result = a.normalize_feature_render(weighted, alpha)
    torch.testing.assert_close(result, torch.tensor([[[2., -1.], [0., 0.]]], dtype=torch.float64))
    result.sum().backward()
    torch.testing.assert_close(weighted.grad, torch.tensor([[[4., 4.], [0., 0.]]], dtype=torch.float64))


def test_l5_invalid_targets_are_ignored_without_poisoning_gradient():
    pred = torch.tensor([[[2., 4.], [8., 10.]]], requires_grad=True)
    target = torch.tensor([[[0., 0.], [float('nan'), float('nan')]]])
    valid = torch.tensor([[True, False]])
    loss = a.distillation_loss(pred, target, valid)
    torch.testing.assert_close(loss, torch.tensor(3.))
    loss.backward()
    torch.testing.assert_close(pred.grad, torch.tensor([[[.5, .5], [0., 0.]]]))


def test_l5_empty_supervision_still_backpropagates_zero():
    prediction = torch.randn(2, 2, 3, requires_grad=True)
    loss = a.distillation_loss(prediction, torch.full_like(prediction, float('nan')), torch.zeros(2, 2, dtype=torch.bool))
    assert loss.requires_grad and loss.item() == 0
    loss.backward()
    torch.testing.assert_close(prediction.grad, torch.zeros_like(prediction))


def test_l6_pairwise_worst_negative_not_one_global_softmax():
    features = torch.tensor([[1., 0.], [0., 1.], [0., 0.]])
    positive = torch.tensor([1., 0.])
    negatives = torch.tensor([[0., 1.], [-1., 0.]])
    result = a.text_relevancy(features, positive, negatives, temperature=2.)
    expected = torch.tensor([1/(1+np.exp(-2)), 1/(1+np.exp(2)), 0.], dtype=torch.float32)
    torch.testing.assert_close(result, expected)
    scaled = a.text_relevancy(features * 5, positive * 3, negatives * 2, temperature=2.)
    torch.testing.assert_close(result, scaled)


def test_l7_fusion_repeated_indices_uncovered_and_gradients():
    indices = torch.tensor([2, 0, 2], dtype=torch.int64)
    features = torch.tensor([[2., 4.], [1., -1.], [6., 8.]], requires_grad=True)
    weights = torch.tensor([1., 2., 3.])
    values, coverage = a.fuse_observations(indices, features, weights, 4)
    torch.testing.assert_close(values, torch.tensor([[1., -1.], [0., 0.], [5., 7.], [0., 0.]]))
    torch.testing.assert_close(coverage, torch.tensor([2., 0., 4., 0.]))
    values.sum().backward()
    torch.testing.assert_close(features.grad, torch.tensor([[.25, .25], [1., 1.], [.75, .75]]))


def test_l7_empty_and_invalid_indices():
    values, coverage = a.fuse_observations(torch.empty(0, dtype=torch.int64), torch.empty(0, 2), torch.empty(0), 3)
    torch.testing.assert_close(values, torch.zeros(3, 2))
    torch.testing.assert_close(coverage, torch.zeros(3))
    with pytest.raises(ValueError):
        a.fuse_observations(torch.tensor([3]), torch.ones(1, 2), torch.ones(1), 3)
