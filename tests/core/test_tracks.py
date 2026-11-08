"""Public T1 tests: graph integrity and a stable conflict policy."""

import numpy as np
import pytest

from minicolmap.tracks import build_tracks


def test_t1_transitive_tracks_and_deterministic_order():
    pairs = {
        (1, 2): np.array([[4, 7], [8, 9]]),
        (0, 1): np.array([[3, 4], [2, 8]]),
    }
    expected = [((0, 2), (1, 8), (2, 9)), ((0, 3), (1, 4), (2, 7))]
    assert build_tracks(pairs) == expected
    assert build_tracks(dict(reversed(list(pairs.items())))) == expected


def test_t1_duplicate_and_reversed_edges_do_not_duplicate_observations():
    pairs = {
        (0, 1): np.array([[2, 3], [2, 3]]),
        (1, 0): np.array([[3, 2]]),
        (1, 2): np.array([[3, 5]]),
        (0, 2): np.array([[2, 5]]),
    }
    assert build_tracks(pairs) == [((0, 2), (1, 3), (2, 5))]


def test_t1_rejects_whole_conflicting_component_and_keeps_clean_one():
    pairs = {
        (0, 1): np.array([[1, 5], [2, 5], [10, 20]]),
        (1, 2): np.array([[5, 8], [20, 30]]),
    }
    # The first component has image 0 features 1 AND 2; discard it all.
    assert build_tracks(pairs) == [((0, 10), (1, 20), (2, 30))]


def test_t1_empty_input_and_empty_match_arrays():
    assert build_tracks({}) == []
    assert build_tracks({(0, 1): np.empty((0, 2), dtype=np.int64)}) == []


@pytest.mark.parametrize("pairs", [
    {(0, 0): np.array([[0, 1]])},
    {(-1, 2): np.array([[0, 1]])},
    {(0, 1): np.array([0, 1])},
    {(0, 1): np.array([[0.0, 1.0]])},
    {(0, 1): np.array([[-1, 1]])},
])
def test_t1_rejects_bad_ids_and_match_arrays(pairs):
    with pytest.raises(ValueError):
        build_tracks(pairs)
