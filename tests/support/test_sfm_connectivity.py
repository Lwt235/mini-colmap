"""Sparse matching bridges and conflict recovery without loosening map geometry."""
import copy

import numpy as np
import pytest

from minicolmap.config import resolve_config
from minicolmap.mapper import VerifiedPair
from minicolmap.pipeline import matching_components
from minicolmap.tracks import build_tracks


def noisy_edges():
    # Two true 3-view cycles, joined by one inconsistent feature edge.
    return {(1, 2): np.array([[0, 0], [1, 1], [0, 1]]),
            (1, 3): np.array([[0, 0], [1, 1]]),
            (2, 3): np.array([[0, 0], [1, 1]])}


def test_split_preserves_consistent_cycles_after_a_false_bridge():
    pairs = noisy_edges()
    before = copy.deepcopy(pairs)
    assert build_tracks(pairs) == []  # Legacy contract remains available.
    expected = [((1, 0), (2, 0), (3, 0)), ((1, 1), (2, 1), (3, 1))]
    assert build_tracks(pairs, conflict_policy='split') == expected
    for key in pairs:
        np.testing.assert_array_equal(pairs[key], before[key])


def test_split_is_stable_under_reordering_duplicates_and_reversed_edges():
    pairs = noisy_edges()
    expected = build_tracks(pairs, conflict_policy='split')
    pairs = {(j, i): np.repeat(matches[::-1, ::-1], 2, axis=0)
             for (i, j), matches in reversed(list(pairs.items()))}
    assert build_tracks(pairs, conflict_policy='split') == expected


def test_split_never_assigns_two_features_from_one_image_to_a_track():
    pairs = {(1, 2): np.array([[0, 0], [1, 0], [2, 1]]),
             (2, 3): np.array([[0, 0], [1, 0]])}
    tracks = build_tracks(pairs, conflict_policy='split')
    flattened = [observation for track in tracks for observation in track]
    assert len(set(flattened)) == len(flattened)
    assert all(len({i for i, _ in track}) == len(track) >= 2 for track in tracks)
    assert build_tracks({}, conflict_policy='split') == []
    with pytest.raises(ValueError, match='conflict_policy'):
        build_tracks({}, conflict_policy='guess')


def test_pair_admission_override_keeps_strong_initialization_and_pnp_gates():
    old = resolve_config({'geometry': {'min_inliers': 30}})
    repaired = resolve_config({'geometry': {'min_inliers': 30, 'min_pair_inliers': 20},
                               'mapping': {'track_conflict_policy': 'split'}})
    assert 'min_pair_inliers' not in old['geometry']
    assert repaired['geometry']['min_inliers'] == 30
    assert repaired['geometry']['min_pair_inliers'] == 20
    assert repaired['mapping']['min_pnp_points'] == old['mapping']['min_pnp_points'] == 15
    assert repaired['mapping']['max_reprojection_px'] == old['mapping']['max_reprojection_px'] == 3


@pytest.mark.parametrize('value', [7, 1.5, True, None, float('nan')])
def test_invalid_pair_override_rejected(value):
    with pytest.raises(ValueError):
        resolve_config({'geometry': {'min_pair_inliers': value}})


def test_invalid_track_policy_rejected():
    with pytest.raises(ValueError, match='track_conflict_policy'):
        resolve_config({'mapping': {'track_conflict_policy': 'merge_all'}})


def test_image_graph_keeps_isolated_nodes_and_sparse_bridge():
    def pair(i, j, count):
        ids = np.arange(count)
        return VerifiedPair(i, j, np.column_stack([ids, ids]), np.eye(3))
    edges = [pair(11, 29, 40), pair(53, 91, 40), pair(29, 53, 20)]
    assert matching_components([11, 29, 53, 91, 103], edges[:2]) == [[11, 29], [53, 91], [103]]
    assert matching_components([11, 29, 53, 91, 103], edges) == [[11, 29, 53, 91], [103]]
