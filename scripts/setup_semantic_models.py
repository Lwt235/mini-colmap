"""Download exactly two public weights; verify before publishing local manifest."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from minicolmap.semantics.project import sha256, write_json

CLIP_REVISION = '7288da5a0d6f0b51c4a2b27c624837a9236d0112'
CLIP_SHA256 = '3f25d29d3cc74e1d25d47e0593b4dd0864ced1e9b4d8e486a247ca4502f227f1'
SAM_SIZE = 2564550879
# Filled from the downloaded official file and validated by real SAM loading.
SAM_SHA256 = 'a7bf3b02f3ebf1267aba913ff637d9a2d5c33d3173bb679e46d9f338c26f262e'
SAM_URL = 'https://dl.fbaipublicfiles.com/segment_anything/sam_vit_h_4b8939.pth'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    root = args.root.resolve()
    sam = root / 'models/sam/sam_vit_h_4b8939.pth'
    clip = root / 'models/openclip/open_clip_model.safetensors'
    if not args.verify_only:
        sam.parent.mkdir(parents=True, exist_ok=True)
        if not sam.exists():
            part = sam.with_suffix(sam.suffix + '.part')
            subprocess.run(['curl', '--fail', '--location', '--retry', '3', '--continue-at', '-',
                            '--output', str(part), SAM_URL], check=True)
            if part.stat().st_size != SAM_SIZE or (SAM_SHA256 and sha256(part) != SAM_SHA256):
                raise ValueError('SAM download size/hash mismatch')
            part.replace(sam)
        # hf is supplied by the same virtual environment as this script.
        executable = Path(sys.executable).parent / 'hf'
        environment = {**os.environ, 'HF_HUB_DISABLE_XET': '1', 'HF_HUB_DOWNLOAD_TIMEOUT': '120'}
        if not clip.exists():
            subprocess.run([str(executable), 'download', 'laion/CLIP-ViT-B-16-laion2B-s34B-b88K',
                            'open_clip_model.safetensors', 'open_clip_config.json', 'README.md',
                            '--revision', CLIP_REVISION, '--local-dir', str(clip.parent)],
                           env=environment, check=True)
    if sam.stat().st_size != SAM_SIZE or clip.stat().st_size != 598516980:
        raise ValueError('Model download is incomplete')
    sam_hash, clip_hash = sha256(sam), sha256(clip)
    if (SAM_SHA256 and sam_hash != SAM_SHA256) or clip_hash != CLIP_SHA256:
        raise ValueError('Model checksum mismatch')
    manifest = {'schema_version': 1,
                'sam': {'path': str(sam.relative_to(root)), 'size': sam.stat().st_size, 'sha256': sam_hash,
                        'source': SAM_URL, 'license': 'Apache-2.0', 'architecture': 'vit_h',
                        'source_revision': 'dca509fe793f601edb92606367a655c15ac00fdf'},
                'clip': {'path': str(clip.relative_to(root)), 'size': clip.stat().st_size, 'sha256': clip_hash,
                         'source': 'https://huggingface.co/laion/CLIP-ViT-B-16-laion2B-s34B-b88K',
                         'revision': CLIP_REVISION, 'license': 'MIT', 'architecture': 'ViT-B-16',
                         'pretrained': 'laion2b_s34b_b88k', 'feature_dim': 512}}
    write_json(root / 'models/manifest.json', manifest)
    print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    main()
