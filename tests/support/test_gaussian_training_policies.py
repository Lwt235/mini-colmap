"""State transport, train-only screen gradients, density and objective regression."""
import copy

import pytest

torch = pytest.importorskip('torch')
from minicolmap.rendering.algorithms import initialize_gaussians, densify_and_prune
from minicolmap.rendering.config import resolve_render_config
from minicolmap.rendering.gaussian_training import (
    DensityStatistics, adaptive_topology, make_optimizer, photometric_loss,
    replace_topology, reset_opacity, ssim_image, update_learning_rates)
from minicolmap.rendering.gaussian_training import optimizer_step


def scene_state():
    values = initialize_gaussians(torch.tensor([[0., 0., 2.], [.2, 0., 2.], [-.2, 0., 3.]]),
                                  torch.full((3, 3), .4), .02, .3)
    return {key: torch.nn.Parameter(value.detach().clone()) for key, value in values.items()}


def test_adam_moments_follow_retained_identity_and_new_rows_start_empty():
    state = scene_state()
    config = resolve_render_config({'gaussian': {'optimizer_state': 'preserve'}})
    optimizer = make_optimizer(state, config)
    sum(value.square().sum() for value in state.values()).backward()
    optimizer.step()
    saved = {key: copy.deepcopy(optimizer.state[value]) for key, value in state.items()}
    sources = torch.tensor([2, 0, 2])  # prune old row 1, reorder survivors, append one child
    revised = {key: value[sources].detach().clone() for key, value in state.items()}
    revised['means'][2, 0] += .1
    migrated, same_optimizer = replace_topology(state, revised, optimizer, sources,
                                               torch.tensor([False, False, True]), config)
    assert same_optimizer is optimizer
    for key, value in migrated.items():
        moments = optimizer.state[value]
        assert moments['step'] == saved[key]['step']
        for name in ('exp_avg', 'exp_avg_sq'):
            torch.testing.assert_close(moments[name][:2], saved[key][name][[2, 0]])
            assert torch.count_nonzero(moments[name][2]) == 0
        assert state[key] not in optimizer.state
    optimizer.zero_grad(set_to_none=True)
    sum(value.square().sum() for value in migrated.values()).backward()
    optimizer.step()
    assert all(optimizer.state[value]['step'] == 2 for value in migrated.values())


def test_explicit_reset_reproduces_historical_optimizer_restart():
    state = scene_state()
    config = resolve_render_config({'gaussian': {'optimizer_state': 'reset'}})
    optimizer = make_optimizer(state, config)
    sum(value.square().sum() for value in state.values()).backward()
    optimizer.step()
    revised = {key: value.detach().clone() for key, value in state.items()}
    _, replacement = replace_topology(state, revised, optimizer, torch.arange(3), torch.zeros(3, dtype=torch.bool), config)
    assert replacement is not optimizer and not replacement.state


def test_sh_update_ratio_matches_independent_adam_learning_rates():
    state = scene_state()
    config = resolve_render_config({'gaussian': {'sh_rest_lr_ratio': .05, 'adam_eps': 1e-15}})
    optimizer = make_optimizer(state, config)
    dc = torch.nn.Parameter(state['sh_coeffs'][:, :1].detach().clone())
    rest = torch.nn.Parameter(state['sh_coeffs'][:, 1:].detach().clone())
    separate = torch.optim.Adam([{'params': [dc], 'lr': config['learning_rate']},
                                {'params': [rest], 'lr': config['learning_rate'] * .05}], eps=1e-15)
    for _ in range(4):
        optimizer.zero_grad(set_to_none=True)
        separate.zero_grad(set_to_none=True)
        state['sh_coeffs'].grad = torch.full_like(state['sh_coeffs'], 1e-10)
        dc.grad, rest.grad = torch.full_like(dc, 1e-10), torch.full_like(rest, 1e-10)
        optimizer_step(optimizer, state, config)
        separate.step()
        torch.testing.assert_close(state['sh_coeffs'][:, :1], dc, rtol=1e-6, atol=1e-7)
        torch.testing.assert_close(state['sh_coeffs'][:, 1:], rest, rtol=1e-6, atol=1e-7)


def test_uniform_named_groups_match_the_old_single_group_adam_exactly():
    first = scene_state()
    second = {key: torch.nn.Parameter(value.detach().clone()) for key, value in first.items()}
    config = resolve_render_config()
    old = torch.optim.Adam(list(first.values()), lr=config['learning_rate'])
    named = make_optimizer(second, config)
    for _ in range(4):
        for values, optimizer in ((first, old), (second, named)):
            optimizer.zero_grad(set_to_none=True)
            sum(value.square().sum() for value in values.values()).backward()
            optimizer.step()
        for key in first:
            torch.testing.assert_close(first[key], second[key], rtol=0, atol=0)


def test_legacy_lineage_preserves_old_values_and_identifies_children():
    state = scene_state()
    with torch.no_grad():
        state['opacity_logits'][1] = -20
    args = (state, torch.tensor([1., 1., 0.]), .1, .005, 8)
    old = densify_and_prune(*args)
    revised, sources, is_new = densify_and_prune(*args, return_lineage=True)
    assert sources.tolist() == [0, 2, 0] and is_new.tolist() == [False, False, True]
    for key in old:
        torch.testing.assert_close(old[key], revised[key], rtol=0, atol=0)


def test_grouped_lr_schedule_updates_only_position_rate():
    config = resolve_render_config({'iterations': 100, 'gaussian': {'lr_mode': 'grouped'}})
    optimizer = make_optimizer(scene_state(), config)
    before = {group['name']: group['lr'] for group in optimizer.param_groups}
    update_learning_rates(optimizer, config, 100)
    after = {group['name']: group['lr'] for group in optimizer.param_groups}
    assert after['means'] == pytest.approx(before['means'] * .01)
    assert all(after[key] == before[key] for key in after if key != 'means')


def test_ssim_identity_and_combined_loss_have_finite_nonzero_gradient():
    torch.manual_seed(4)
    target = torch.rand((16, 19, 3))
    assert float(ssim_image(target, target)) == pytest.approx(1, abs=1e-6)
    prediction = (target * .7).detach().requires_grad_()
    config = resolve_render_config({'gaussian': {'loss': 'l1_ssim'}})
    loss = photometric_loss(prediction, target, config)
    loss.backward()
    assert float(loss) > 0 and torch.isfinite(prediction.grad).all() and prediction.grad.abs().sum() > 0


def test_density_accumulates_only_visible_rows_and_rescales_pixel_gradient():
    state = scene_state()
    statistics = DensityStatistics(state)
    means = torch.tensor([[[1., 2.], [3., 4.]]], requires_grad=True)
    means.retain_grad()
    (means * means.new_tensor([.1, .2])).sum().backward()
    statistics.accumulate({'means2d': means, 'indices': torch.tensor([0, 2]),
                           'radii': torch.tensor([[4., 0.]]), 'width': 100, 'height': 50})
    assert statistics.count.tolist() == [1, 0, 0]
    assert float(statistics.gradient[0]) == pytest.approx(50 ** .5)
    assert float(statistics.radius[0]) == pytest.approx(.04)


def test_absolute_screen_gradients_preserve_signal_after_signed_cancellation():
    state = scene_state()
    means = torch.zeros((1, 1, 2), requires_grad=True)
    (means * 0).sum().backward()
    means.absgrad = torch.tensor([[[.2, .4]]])
    info = {'means2d': means, 'indices': torch.tensor([0]), 'radii': torch.tensor([[2.]]), 'width': 100, 'height': 50}
    signed, absolute = DensityStatistics(state), DensityStatistics(state)
    signed.accumulate(info)
    absolute.accumulate({**info, 'absgrad': True})
    assert signed.gradient[0] == 0
    assert float(absolute.gradient[0]) == pytest.approx(200 ** .5)


def test_screen_large_splat_splits_even_when_small_in_world_units():
    state = scene_state()
    with torch.no_grad():
        state['log_scales'][:] = torch.tensor(.005).log()
    statistics = DensityStatistics(state)
    statistics.gradient[0] = statistics.count[0] = 1
    statistics.radius[0] = .08
    options = resolve_render_config()['gaussian']
    options.update(max_gaussians=10, grow_screen_radius=.05)
    revised, sources, _ = adaptive_topology(state, statistics, options)
    assert sources.tolist() == [1, 2, 0, 0]
    torch.testing.assert_close(revised['means'][-2:].mean(0), state['means'][0])
    assert torch.all(revised['log_scales'][-2:] < state['log_scales'][0])


def test_stochastic_split_is_seeded_and_samples_all_covariance_axes():
    count = 2048
    values = initialize_gaussians(torch.zeros(count, 3), torch.full((count, 3), .4), .02, .3)
    with torch.no_grad():
        values['log_scales'][:] = torch.tensor([.03, .02, .005]).log()
    statistics = DensityStatistics(values)
    statistics.gradient[:] = statistics.count[:] = 1
    options = resolve_render_config()['gaussian']
    options.update(max_gaussians=count*2, split_mode='stochastic')
    torch.manual_seed(23)
    revised, _, _ = adaptive_topology(values, statistics, options)
    torch.manual_seed(23)
    repeated, _, _ = adaptive_topology(values, statistics, options)
    torch.testing.assert_close(revised['means'], repeated['means'], rtol=0, atol=0)
    expected = torch.tensor([.03, .02, .005]).square()
    torch.testing.assert_close(revised['means'].var(0), expected, rtol=.08, atol=1e-7)


def test_adaptive_density_clones_small_splits_large_and_prunes_unreliable():
    state = scene_state()
    with torch.no_grad():
        state['log_scales'][0] = -.5  # too large, prune
        state['log_scales'][1] = torch.tensor([.03, .02, .02]).log()
        state['log_scales'][2] = torch.tensor(.005).log()
    statistics = DensityStatistics(state)
    statistics.gradient[:] = 1
    statistics.count[:] = 1
    options = resolve_render_config()['gaussian']
    options['max_gaussians'] = 10
    revised, sources, is_new = adaptive_topology(state, statistics, options)
    assert sources.tolist() == [2, 2, 1, 1]
    assert is_new.tolist() == [False, True, True, True]
    torch.testing.assert_close(revised['means'][2:].mean(0), state['means'][1])
    assert torch.all(revised['log_scales'][2:] < state['log_scales'][1])
    assert len(revised['means']) <= options['max_gaussians']


def test_opacity_reset_does_not_erase_other_parameters_moments():
    state = scene_state()
    optimizer = make_optimizer(state, resolve_render_config())
    sum(value.square().sum() for value in state.values()).backward()
    optimizer.step()
    before = optimizer.state[state['means']]['exp_avg'].clone()
    reset_opacity(state, optimizer)
    assert state['opacity_logits'].sigmoid().max() <= .01000001
    assert optimizer.state[state['opacity_logits']]['exp_avg'].count_nonzero() == 0
    torch.testing.assert_close(optimizer.state[state['means']]['exp_avg'], before)


@pytest.mark.parametrize('options', [
    {'optimizer_state': 'unknown'}, {'lr_mode': 'unknown'}, {'loss': 'unknown'},
    {'density_strategy': 'unknown'}, {'ssim_weight': 1.1}, {'grow_grad2d': -1},
    {'densify_start': 10, 'densify_stop': 9}, {'opacity_reset_every': True},
    {'position_lr_final_ratio': 0}, {'prune_scale': float('nan')},
    {'initial_scale_mode': 'unknown'}, {'scene_normalization': 'unknown'},
    {'adam_eps': 0}, {'adam_eps': float('nan')}, {'sh_rest_lr_ratio': 1.1}, {'position_lr_multiplier': 0},
    {'absgrad': 1}, {'absgrad': True}, {'grow_screen_radius': -1},
    {'split_mode': 'unknown'},
])
def test_new_policy_configuration_rejects_invalid_values(options):
    with pytest.raises(ValueError):
        resolve_render_config({'gaussian': options})
