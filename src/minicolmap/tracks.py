"""Feature-track assembly (T1); input edges must already be inliers."""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray
from typing import TypeVar, Generic, Hashable, TypeAlias


Observation: TypeAlias = tuple[int, int]  # (image_id, zero-based keypoint_index)
Track: TypeAlias = tuple[Observation, ...]  # Same scene point; at most one observation per image.


T = TypeVar("T", bound=Hashable)
class DSU(Generic[T]):
    def __init__(self):
        self.parent: dict[T, T] = {}
        self.size: dict[T, int] = {}

    def find(self, x: T) -> T:
        if x not in self.parent:
            self.parent[x] = x
            self.size[x] = 1

        root = x
        while self.parent[root] != root:
            root = self.parent[root]

        while self.parent[x] != root:
            nxt = self.parent[x]
            self.parent[x] = root
            x = nxt

        return root

    def union(self, a: T, b: T) -> bool:
        ra = self.find(a)
        rb = self.find(b)
        if ra != rb:
            if self.size[ra] < self.size[rb]:
                ra, rb = rb, ra
            self.parent[rb] = ra
            self.size[ra] += self.size[rb]
            return True
        return False

    def connected(self, a: T, b: T) -> bool:
        return self.find(a) == self.find(b)

    def get_size(self, x: T) -> int:
        return self.size[self.find(x)]


def build_tracks(pair_matches: dict[tuple[int, int], NDArray], *,
                 conflict_policy: str = 'reject') -> list[Track]:
    """T1: Join verified pair matches into deterministic multi-view tracks.

    A key (i, j) identifies two distinct nonnegative image IDs. Its
    integer array (M, 2) contains [feature_in_i, feature_in_j], also
    nonnegative. Return connected components with >= 2 observations,
    each tuple lexicographically sorted; sort the list lexicographically.
    Duplicate/reversed edges have no effect. If a component contains
    two distinct features from one image, reject the *whole component*.
    This deliberately conservative conflict policy avoids order-dependent
    merging. No descriptor matching or epipolar verification happens here.

    conflict_policy='split' instead retains cycle-supported, image-consistent
    subtracks. The default 'reject' preserves the original T1 contract.

    Checkpoint: why can a single false match corrupt several images' tracks?
    """
    if conflict_policy not in ('reject', 'split'):
        raise ValueError("conflict_policy must be reject or split")
    if not isinstance(pair_matches, dict):
        raise ValueError("pair_matches must be a dict")
    for pair, matches in pair_matches.items():
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise ValueError("each key must be (image_id_1, image_id_2)")
        if any(isinstance(v, (bool, np.bool_)) or not isinstance(v, (int, np.integer)) or v < 0 for v in pair):
            raise ValueError("image IDs must be nonnegative integers")
        if pair[0] == pair[1]:
            raise ValueError("a pair must contain two distinct images")
        matches = np.asarray(matches)
        if matches.ndim != 2 or matches.shape[1] != 2:
            raise ValueError("each match array must have shape (M, 2)")
        if not np.issubdtype(matches.dtype, np.integer) or np.any(matches < 0):
            raise ValueError("feature indices must be nonnegative integers")
    if conflict_policy == 'split':
        return _split_conflicting_tracks(pair_matches)
    # The original conservative policy remains the default.
    dsu = DSU()
    for (i, j), matches in pair_matches.items():
        for fi, fj in matches:
            a = (int(i), int(fi))
            b = (int(j), int(fj))
            dsu.union(a, b)

    components = {}
    for obs in dsu.parent:
        root = dsu.find(obs)
        components.setdefault(root, []).append(obs)

    tracks = []
    for observations in components.values():
        image_ids = [img_id for img_id, _ in observations]

        if len(image_ids) != len(set(image_ids)):
            continue

        if len(observations) < 2:
            continue

        track = tuple(sorted(observations))
        tracks.append(track)

    tracks.sort()
    return tracks


def _split_conflicting_tracks(pair_matches: dict[tuple[int, int], NDArray]) -> list[Track]:
    """Keep consistent subtracks instead of deleting an entire noisy component.

    Prefer edges supported by observation triangles, then by distinct matches in
    the image pair. Canonical endpoints break ties independently of input order.
    Never merge components containing different features from the same image.
    No coordinate, pose, or reference calibration is used by this graph policy.
    """
    neighbors: dict[Observation, set[Observation]] = {}
    pair_edges = {}
    for (i, j), matches in pair_matches.items():
        key = tuple(sorted((int(i), int(j))))
        edges = pair_edges.setdefault(key, set())
        for fi, fj in matches:
            a, b = sorted(((int(i), int(fi)), (int(j), int(fj))))
            edges.add((a, b))
            neighbors.setdefault(a, set()).add(b)
            neighbors.setdefault(b, set()).add(a)
    ranked = []
    for edges in pair_edges.values():
        for a, b in edges:
            triangles = len(neighbors[a] & neighbors[b])
            ranked.append((-triangles, -len(edges), a, b))
    ranked.sort()
    dsu = DSU()
    image_sets = {obs: {obs[0]} for obs in neighbors}
    for _, _, a, b in ranked:
        ra, rb = dsu.find(a), dsu.find(b)
        if ra == rb or image_sets[ra] & image_sets[rb]:
            continue
        images = image_sets[ra] | image_sets[rb]
        dsu.union(ra, rb)
        root = dsu.find(ra)
        image_sets.pop(ra)
        image_sets.pop(rb)
        image_sets[root] = images
    components = {}
    for obs in neighbors:
        components.setdefault(dsu.find(obs), []).append(obs)
    return sorted(tuple(sorted(observations)) for observations in components.values()
                  if len(observations) >= 2)
