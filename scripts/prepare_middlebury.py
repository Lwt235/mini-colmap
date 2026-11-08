"""Download and configure the calibrated Middlebury TempleRing photo dataset.

Only K and images enter dataset.json. Published R/t are retained separately
for future evaluation and are never supplied as reconstructed camera poses.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import urllib.request
import zipfile

import cv2
import numpy as np

from minicolmap.io import load_dataset


PROJECT = Path(__file__).resolve().parents[1]
SOURCE_PAGE = 'https://vision.middlebury.edu/mview/data/'
SOURCE_URL = SOURCE_PAGE + 'data/templeRing.zip'
# Observed official archive hash, 2026-10-16; pins the downloaded version.
ARCHIVE_SHA256 = '5f871fe96d25f510eac026c66c3a4c38229326260986e9926cba8a64e88c8359'


def save_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n',
                    encoding='utf-8')


def prepare(archive: Path, destination: Path) -> dict:
    """Verify official bytes and build input manifests without changing pixels."""
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != ARCHIVE_SHA256:
        raise ValueError(f'Unexpected archive SHA256: {digest}; do not use an unverified archive')
    if destination.exists() and any(destination.iterdir()):
        raise ValueError(f'Destination must be empty: {destination}')

    with zipfile.ZipFile(archive) as z:
        bad_member = z.testzip()
        if bad_member:
            raise ValueError(f'ZIP CRC check failed: {bad_member}')
        lines = z.read('templeRing/templeR_par.txt').decode('ascii').splitlines()
        lines = [line.strip() for line in lines if line.strip()]
        expected_count = int(lines[0])
        records = []
        for line in lines[1:]:
            fields = line.split()
            if len(fields) != 22:
                raise ValueError('Expected image name plus 21 camera parameter values')
            name = fields[0]
            if PurePosixPath(name).name != name or '\\' in name or not name.endswith('.png'):
                raise ValueError('Unexpected image filename in calibration')
            values = np.asarray(fields[1:], dtype=np.float64)
            if not np.isfinite(values).all():
                raise ValueError('Nonfinite calibration')
            records.append((name, values[:9].reshape(3, 3),
                            values[9:18].reshape(3, 3), values[18:21]))
        records.sort(key=lambda record: record[0])
        if len(records) != expected_count or len({r[0] for r in records}) != expected_count:
            raise ValueError('Camera record count is inconsistent')
        K = records[0][1]
        if not all(np.array_equal(record[1], K) for record in records):
            raise ValueError('This preparation expects TempleRing to have shared intrinsics')

        images_dir = destination / 'images'
        reference_dir = destination / 'reference'
        images_dir.mkdir(parents=True)
        reference_dir.mkdir()
        for name in ('templeR_par.txt', 'templeR_ang.txt', 'README.txt'):
            (reference_dir / name).write_bytes(z.read('templeRing/' + name))
        dimensions = None
        checksums = {}
        image_records = []
        reference_poses = []
        for image_id, (name, _, R, t) in enumerate(records, start=1):
            raw = z.read('templeRing/' + name)
            decoded = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8),
                                   cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
            if decoded is None:
                raise ValueError(f'Cannot decode {name}')
            height, width = decoded.shape[:2]
            if dimensions is None:
                dimensions = (width, height)
            if dimensions != (width, height):
                raise ValueError('Image dimensions differ')
            (images_dir / name).write_bytes(raw)
            checksums[name] = hashlib.sha256(raw).hexdigest()
            image_records.append({'id': image_id, 'name': name, 'camera_id': 1,
                                  'path': 'images/' + name})
            reference_poses.append({'image_id': image_id, 'name': name,
                                    'R': R.tolist(), 't': t.tolist()})

    width, height = dimensions
    camera = {'id': 1, 'width': width, 'height': height, 'K': K.tolist()}
    # templeR_ang.txt has a large capture gap between filenames 0005/0006.
    # 0013..0020 are eight nearby views from one continuous capture arc.
    selections = {
        'dataset.json': image_records[12:20],
        'dataset_5.json': image_records[:5],
        'dataset_first8.json': image_records[:8],
        'dataset_20.json': image_records[:20],
        'dataset_all.json': image_records,
    }
    for filename, selected in selections.items():
        manifest = {'cameras': [camera], 'images': selected}
        save_json(destination / filename, manifest)
        load_dataset(destination / filename)
    save_json(reference_dir / 'poses.json', {
        'purpose': 'Evaluation only; not passed into SfM estimation',
        'pose_convention': 'world_to_camera', 'poses': reference_poses,
    })
    provenance = {
        'dataset': 'Middlebury Multi-View Stereo: TempleRing',
        'source_page': SOURCE_PAGE, 'download_url': SOURCE_URL,
        'archive_sha256': digest, 'archive_bytes': archive.stat().st_size,
        'download_date': '2026-10-16', 'image_count': len(image_records),
        'width': width, 'height': height, 'shared_K': K.tolist(),
        'preprocessing': 'None: original PNG bytes, original dimensions/orientation. '
                         'Publisher states radial distortion was already removed.',
        'selection': {filename: [item['name'] for item in selected]
                      for filename, selected in selections.items()},
        'selection_reason': 'Default 0013..0020 follows a continuous arc in official '
                            'templeR_ang.txt; file order 0005->0006 has a large view gap. '
                            'Capture angles only choose input views; R/t never enter estimation.',
        'intrinsics_source': 'Official templeR_par.txt, row-major K',
        'extrinsics_usage': 'Saved under reference/ for evaluation only',
        'image_sha256': checksums,
        'citation': 'Seitz, Curless, Diebel, Scharstein, Szeliski. A Comparison and '
                    'Evaluation of Multi-View Stereo Reconstruction Algorithms. CVPR 2006.',
    }
    save_json(destination / 'provenance.json', provenance)
    return {'destination': str(destination), 'downloaded_images': len(image_records),
            'default_input_images': 8, 'width': width, 'height': height, 'K': K.tolist()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, default=PROJECT / '.cache/datasets/templeRing.zip')
    parser.add_argument('--destination', type=Path, default=PROJECT / 'data/middlebury_temple_ring')
    args = parser.parse_args()
    if args.destination.exists() and any(args.destination.iterdir()):
        parser.error('Destination already contains files; choose a new --destination')
    if not args.archive.exists():
        args.archive.parent.mkdir(parents=True, exist_ok=True)
        partial = args.archive.with_suffix(args.archive.suffix + '.part')
        print(f'Downloading {SOURCE_URL}', flush=True)
        with urllib.request.urlopen(SOURCE_URL, timeout=60) as response, partial.open('wb') as output:
            shutil.copyfileobj(response, output)
        if hashlib.sha256(partial.read_bytes()).hexdigest() != ARCHIVE_SHA256:
            raise ValueError('Downloaded bytes differ from the pinned official archive')
        partial.replace(args.archive)
    print(json.dumps(prepare(args.archive, args.destination), indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
