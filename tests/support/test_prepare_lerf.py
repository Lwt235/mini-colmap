"""Protect imported camera geometry and archive containment."""
import importlib.util
from pathlib import Path
import struct
import zipfile

import cv2
import numpy as np
import pytest
import json

from minicolmap.io import load_dataset, load_reconstruction
from minicolmap.rendering.data import load_posed_dataset


def script():
    spec = importlib.util.spec_from_file_location('prepare_lerf', Path(__file__).resolve().parents[2] / 'scripts/prepare_lerf.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture_model(directory, *, wrong_track=False):
    # Noncontiguous IDs, +90 degree world-to-camera rotation about Z.
    camera = struct.pack('<QiiQQ4d', 1, 3, 1, 100, 80, 50, 60, 50, 40)
    (directory / 'cameras.bin').write_bytes(camera)
    q = np.sqrt(.5)
    image = struct.pack('<Qi7di', 1, 7, q, 0, 0, q, 0, 0, 1, 3)
    # X=(1,0,1) -> Xc=(0,1,2) -> COLMAP pixel (50,70).
    image += b'frame.jpg\0' + struct.pack('<Qddq', 1, 50, 70, 91)
    (directory / 'images.bin').write_bytes(image)
    point = struct.pack('<QQ3d3BdQii', 1, 91, 1, 0, 1, 10, 20, 30, 0, 1, 7, int(wrong_track))
    (directory / 'points3D.bin').write_bytes(point)


def test_import_preserves_world_to_camera_and_pixel_reprojection(tmp_path):
    fixture_model(tmp_path)
    model = script().read_model(tmp_path)
    camera, pose, point = model.cameras[3], model.poses[7], model.points[91]
    np.testing.assert_allclose(pose.R @ [1, 0, 0], [0, 1, 0], atol=1e-12)
    np.testing.assert_allclose(pose.center, [0, 0, -1], atol=1e-12)
    projected = camera.K @ (pose.R @ point.xyz + pose.t)
    np.testing.assert_allclose(projected[:2] / projected[2], [49.5, 69.5])
    np.testing.assert_allclose(model.images[7].keypoints[0], projected[:2] / projected[2])
    assert point.observations == ((7, 0),)


def test_import_rejects_inconsistent_tracks(tmp_path):
    fixture_model(tmp_path, wrong_track=True)
    with pytest.raises(ValueError, match='track disagrees'):
        script().read_model(tmp_path)


def test_duplicate_image_observation_retains_closest_fixed_projection(tmp_path):
    fixture_model(tmp_path)
    path = tmp_path / 'images.bin'
    payload = path.read_bytes()[:82]  # count + 64-byte header + frame.jpg NUL
    assert payload.endswith(b'frame.jpg\0')
    path.write_bytes(payload + struct.pack('<Q', 2) + struct.pack('<ddqddq', 55, 70, 91, 50, 70, 91))
    (tmp_path / 'points3D.bin').write_bytes(struct.pack('<QQ3d3BdQiiii', 1, 91, 1, 0, 1, 10, 20, 30, 0, 2, 7, 0, 7, 1))
    audit = []
    model = script().read_model(tmp_path, audit)
    assert model.points[91].observations == ((7, 1),)
    assert audit == [dict(point_id=91, image_id=7, original_indices=[0, 1], retained_index=1)]


def test_import_rejects_distorted_camera_and_truncation(tmp_path):
    fixture_model(tmp_path)
    path = tmp_path / 'cameras.bin'
    path.write_bytes(struct.pack('<QiiQQ8d', 1, 3, 4, 100, 80, *([1.] * 8)))
    with pytest.raises(ValueError, match='distortion'):
        script().read_model(tmp_path)
    path.write_bytes(struct.pack('<Q', 100))
    with pytest.raises(ValueError, match='count exceeds'):
        script().read_model(tmp_path)


def test_extract_rejects_zip_path_escape(tmp_path):
    archive = tmp_path / 'bad.zip'
    with zipfile.ZipFile(archive, 'w') as output:
        output.writestr('../outside.txt', 'bad')
    with pytest.raises(ValueError, match='Unsafe ZIP'):
        script().extract(archive, tmp_path / 'raw')
    assert not (tmp_path / 'outside.txt').exists()


def test_prepared_manifests_preserve_photos_and_keep_sfm_input_unposed(tmp_path):
    scene = tmp_path / 'raw/example'
    sparse = scene / 'sparse/0'
    sparse.mkdir(parents=True)
    fixture_model(sparse)
    (scene / 'images').mkdir()
    photo = scene / 'images/frame.jpg'
    assert cv2.imwrite(str(photo), np.full((80, 100, 3), 120, np.uint8))
    label = scene.parent / 'label/example'
    label.mkdir(parents=True)
    (label / 'frame.json').write_text(json.dumps(dict(info=dict(name='frame.jpg', width=100, height=80),
        objects=[dict(category='cup', segmentation=[[1, 1], [2, 2], [1, 2]])])))
    destination, output = tmp_path / 'data/example', tmp_path / 'outputs/example'
    script().prepare_scene(scene, destination, output)
    assert (destination / 'images/frame.jpg').read_bytes() == photo.read_bytes()
    assert set(load_dataset(destination / 'dataset_all.json')['images']) == {7}
    assert 'poses' not in json.loads((destination / 'dataset_12.json').read_text())
    assert set(load_posed_dataset(destination / 'dataset_posed_all.json').poses) == {7}
    assert set(load_reconstruction(output / 'model.json').points) == {91}
    assert json.loads((destination / 'semantic_annotations.json').read_text())['frames'][0]['categories'] == ['cup']
