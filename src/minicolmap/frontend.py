"""OpenCV SIFT extraction, descriptor matching and diagnostic previews."""
from pathlib import Path

import numpy as np


def _cv2():
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("OpenCV is missing. Install the project with pip install -e '.[dev]'.") from exc
    return cv2


def load_rgb(path: Path):
    """Decode encoded pixels without EXIF rotation; K must describe this same orientation."""
    cv2 = _cv2()
    raw = np.fromfile(path, dtype=np.uint8)
    image = cv2.imdecode(raw, cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
    if image is None:
        raise ValueError(f"Cannot decode image: {path}")
    return cv2.cvtColor(image, cv2.COLOR_BGR2RGB)


def extract_features(path: Path, max_features: int = 4000):
    """Return xy(N,2), descriptors(N,128), RGB(H,W,3); preserve encoded pixel orientation."""
    cv2 = _cv2()
    rgb = load_rgb(path)
    keypoints, descriptors = cv2.SIFT_create(nfeatures=max_features).detectAndCompute(
        cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY), None)
    xy = np.asarray([k.pt for k in keypoints], dtype=np.float64).reshape(-1, 2)
    if descriptors is None:
        descriptors = np.empty((0, 128), dtype=np.float32)
    return xy, descriptors, rgb


def match_descriptors(a, b, ratio: float = 0.75):
    """Bidirectional ratio-test + mutual consistency, returning one-to-one indices(M,2)."""
    a, b = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)
    if a.ndim != 2 or b.ndim != 2 or a.shape[1] != 128 or b.shape[1] != 128:
        raise ValueError("SIFT descriptors must have shape (N,128)")
    if not np.isfinite(a).all() or not np.isfinite(b).all() or not 0 < ratio < 1:
        raise ValueError("Descriptors must be finite and ratio must be in (0,1)")
    if min(len(a), len(b)) < 2:
        return np.empty((0, 2), dtype=np.int64)
    cv2 = _cv2()
    matcher = cv2.BFMatcher(cv2.NORM_L2)

    def passing(source, target):
        return {m.queryIdx: m.trainIdx for neighbors in matcher.knnMatch(source, target, k=2)
                if len(neighbors) == 2 for m, n in [neighbors] if m.distance < ratio * n.distance}

    forward, reverse = passing(a, b), passing(b, a)
    return np.asarray([(i, j) for i, j in sorted(forward.items()) if reverse.get(j) == i],
                      dtype=np.int64).reshape(-1, 2)


def save_match_preview(path, rgb1, rgb2, xy1, xy2, indices, limit: int = 100):
    """Save a bounded diagnostic image; Unicode Windows paths are supported."""
    cv2 = _cv2()
    keypoints1 = [cv2.KeyPoint(float(x), float(y), 3) for x, y in xy1]
    keypoints2 = [cv2.KeyPoint(float(x), float(y), 3) for x, y in xy2]
    matches = [cv2.DMatch(int(i), int(j), 0.0) for i, j in indices[:limit]]
    canvas = cv2.drawMatches(cv2.cvtColor(rgb1, cv2.COLOR_RGB2BGR), keypoints1,
                             cv2.cvtColor(rgb2, cv2.COLOR_RGB2BGR), keypoints2, matches, None,
                             flags=cv2.DrawMatchesFlags_NOT_DRAW_SINGLE_POINTS)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, encoded = cv2.imencode('.png', canvas)
    if not ok:
        raise RuntimeError("Cannot encode match preview")
    encoded.tofile(path)

