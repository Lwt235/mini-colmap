"""Read-only bridge from calibrated SfM results to appearance training data."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

import cv2
import numpy as np

from ..frontend import load_rgb
from ..io import load_dataset, load_reconstruction
from ..models import Pose, Reconstruction, validate_reconstruction


@dataclass
class Frame:
    image_id: int
    name: str
    K: np.ndarray
    R: np.ndarray
    t: np.ndarray
    rgb: np.ndarray  # float32(H,W,3), RGB in [0,1], original sRGB values

    @property
    def width(self):
        return self.rgb.shape[1]

    @property
    def height(self):
        return self.rgb.shape[0]


@dataclass
class TrainingScene:
    frames: dict[int, Frame]
    points: np.ndarray
    colors: np.ndarray
    center: np.ndarray
    scale: float
    train_ids: list[int]
    validation_ids: list[int]
    normalization_source: str = 'sparse_points'


def load_posed_dataset(path: str | Path) -> Reconstruction:
    """Read photos + explicit world-to-camera poses, without constructing points.

    An empty/one-camera pose list is valid data; training checks its own minimum.
    No pose is guessed from image order, EXIF, or an identity default.
    """
    dataset = load_dataset(path)
    document = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if 'poses' not in document:
        raise ValueError('照片数据集尚无相机位姿；请先导入 K/R/t 或估计位姿。NeRF 不要求点云')
    if document.get('pose_convention') != 'world_to_camera':
        raise ValueError('位姿数据集必须声明 pose_convention=world_to_camera')
    if not isinstance(document['poses'], list):
        raise ValueError('poses 必须是 image_id/R/t 对象列表')
    poses = {}
    for record in document['poses']:
        if not isinstance(record, dict) or set(record) != {'image_id', 'R', 't'}:
            raise ValueError('每个位姿必须包含且仅包含 image_id、R、t')
        image_id = record['image_id']
        if isinstance(image_id, bool) or not isinstance(image_id, int) or image_id not in dataset['images']:
            raise ValueError('位姿 image_id 必须对应数据集图像')
        if image_id in poses:
            raise ValueError(f'重复位姿 image_id：{image_id}')
        poses[image_id] = Pose(record['R'], record['t'])
    model = Reconstruction(dataset['cameras'], dataset['images'], poses, {})
    validate_reconstruction(model)
    return model


def split_views(ids: list[int], holdout_every: int) -> tuple[list[int], list[int]]:
    ids = sorted(ids)
    if len(ids) < 2:
        raise ValueError("新视图训练至少需要两台已注册且有原图的相机")
    validation = ids[holdout_every - 1::holdout_every] or [ids[-1]]
    validation_set = set(validation)
    return [i for i in ids if i not in validation_set], validation


def scaled_intrinsics(K: np.ndarray, old_size: tuple[int, int],
                      new_size: tuple[int, int]) -> np.ndarray:
    """OpenCV resize uses half-pixel mapping; transform the whole K, including skew."""
    sx, sy = new_size[0] / old_size[0], new_size[1] / old_size[1]
    transform = np.array([[sx, 0, (sx - 1) / 2], [0, sy, (sy - 1) / 2], [0, 0, 1.]])
    return transform @ K


def gaussian_initial_scales(points: np.ndarray, options: dict) -> np.ndarray:
    """Fixed legacy scale or RMS distance to up to three neighboring seeds.

    In knn mode initial_scale is a ceiling, so isolated outliers do not start
    as giant splats. A positive floor handles duplicate positions. Query the
    actual selected seeds; no quadratic distance matrix or CUDA is required.
    """
    maximum = options['initial_scale']
    if options.get('initial_scale_mode', 'fixed') == 'fixed' or len(points) < 2:
        return np.full(len(points), maximum)
    from scipy.spatial import cKDTree
    distances = cKDTree(points).query(points, k=min(4, len(points)))[0][:, 1:]
    return np.clip(np.sqrt(np.mean(distances ** 2, axis=1)), min(1e-7, maximum), maximum)


def load_training_scene(model_path: str | Path, dataset_path: str | Path, config: dict) -> TrainingScene:
    model = load_reconstruction(model_path)
    dataset = load_dataset(dataset_path)
    method = config['method']
    if method == '3dgs' and not model.points:
        raise ValueError("3DGS 需要用于初始化的稀疏点云；请先构建或加载带位姿的点云模型")
    train_ids, validation_ids = split_views(list(model.poses), config['holdout_every'])
    points = np.array([p.xyz for _, p in sorted(model.points.items())], dtype=np.float64).reshape(-1, 3)
    colors = np.array([p.color for _, p in sorted(model.points.items())], dtype=np.float32).reshape(-1, 3) / 255
    if method == 'nerf':
        # NeRF never uses points to define its required input or normalization.
        # Adding/removing/changing an optional visualization cloud cannot change
        # the training coordinate system for the same photographs and poses.
        centers = np.array([pose.center for _, pose in sorted(model.poses.items())])
        center = np.median(centers, axis=0)
        scale = float(np.max(np.linalg.norm(centers - center, axis=1)))
        normalization_source = 'camera_centers'
        if scale < 1e-8:
            scale = 1.
            normalization_source = 'camera_centers_unit_fallback'
    elif config['gaussian'].get('scene_normalization', 'sparse_points') == 'camera_extent':
        # Camera baseline sets the optimizer/density units, independent of far
        # sparse-point outliers. Only training camera centers define this frame.
        centers = np.array([model.poses[i].center for i in train_ids])
        center = np.mean(centers, axis=0)
        scale = 1.1 * float(np.max(np.linalg.norm(centers - center, axis=1)))
        normalization_source = 'training_camera_extent'
    else:
        center = np.median(points, axis=0)
        scale = float(np.max(np.linalg.norm(points - center, axis=1)))
        if scale < 1e-8:
            scale = max(float(np.linalg.norm(p.center - center)) for p in model.poses.values())
        normalization_source = 'sparse_points'
    if not np.isfinite(scale) or scale < 1e-8:
        raise ValueError("源地图空间尺度退化")
    frames = {}
    for image_id, pose in sorted(model.poses.items()):
        image = model.images[image_id]
        actual = dataset['images'].get(image_id)
        camera = model.cameras[image.camera_id]
        if actual is None or actual.name != image.name or actual.camera_id != image.camera_id:
            raise ValueError(f"数据集与源模型的图像身份不匹配：{image_id}")
        dataset_camera = dataset['cameras'][actual.camera_id]
        if (dataset_camera.width != camera.width or dataset_camera.height != camera.height
                or not np.allclose(dataset_camera.K, camera.K, atol=1e-10, rtol=0)):
            raise ValueError(f"数据集与源模型的内参不匹配：{image_id}")
        rgb = load_rgb(dataset['image_paths'][image_id])
        if rgb.shape[:2] != (camera.height, camera.width):
            raise ValueError(f"图像尺寸与相机不匹配：{image_id}")
        width = max(1, camera.width // config['image_downscale'])
        height = max(1, camera.height // config['image_downscale'])
        pixels = cv2.resize(rgb, (width, height), interpolation=cv2.INTER_AREA)
        frames[image_id] = Frame(image_id, image.name,
            scaled_intrinsics(camera.K, (camera.width, camera.height), (width, height)),
            pose.R.copy(), (pose.R @ center + pose.t) / scale,
            pixels.astype(np.float32) / 255)
    return TrainingScene(frames, ((points - center) / scale).astype(np.float32),
                         colors, center, scale, train_ids, validation_ids, normalization_source)
