"""Build minimal gsplat kernel ablations from the verified cached build.

Copies the project build and changes only named kernel lines, never installed
gsplat or its production extension. Requires the already verified local build.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import ninja

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / '.cache/torch_extensions/gsplat_cuda'
TARGET = ROOT / '.cache/gaussian-upstream-audit/kernel-controls'


def main():
    package = Path(next(iter(importlib.util.find_spec('gsplat').submodule_search_locations)))
    sources = package / 'cuda/csrc'
    manifest = {}
    for variant in ('alpha99', 'alpha99_ste', 'circular_footprint'):
        build = TARGET / variant
        if not build.exists():
            shutil.copytree(BASE, build)
        for header in list(sources.glob('*.h')) + list(sources.glob('*.cuh')):
            target_header = build / header.name
            if not target_header.exists():
                shutil.copy2(header, target_header)
        replacements = {}
        names = ['ProjectionEWA3DGSFused.cu'] if variant == 'circular_footprint' else [
            'RasterizeToPixels3DGSFwd.cu', 'RasterizeToPixels3DGSBwd.cu']
        for name in names:
            original = sources / name
            text = original.read_text()
            if variant == 'circular_footprint':
                begin = text.index('    float extend = 3.33f;')
                end = text.index('\n    if (radius_x <= radius_clip', begin)
                text = text[:begin] + '''    // Pinned Graphdeco circular 3-sigma tile footprint.
    float mid = .5f * (covar2d[0][0] + covar2d[1][1]);
    float lambda_max = mid + sqrtf(fmaxf(.1f, mid*mid - det));
    float radius_x = ceilf(3.f * sqrtf(lambda_max));
    float radius_y = radius_x;
''' + text[end:]
            else:
                assert text.count('0.999f') in (1, 2)
                text = text.replace('0.999f', '0.99f')
                if variant == 'alpha99_ste' and 'Bwd' in name:
                    assert text.count('if (opac * vis <= 0.99f)') == 1
                    text = text.replace('if (opac * vis <= 0.99f)', 'if (true)')
            patched = build / name
            if not patched.exists() or patched.read_text() != text:
                patched.write_text(text)
            replacements[str(original)] = str(patched)
            manifest.setdefault(variant, {})[name] = {
                'before': hashlib.sha256(original.read_bytes()).hexdigest(),
                'after': hashlib.sha256(patched.read_bytes()).hexdigest()}
        build_text = (BASE / 'build.ninja').read_text()
        for before, after in replacements.items():
            assert before in build_text
            build_text = build_text.replace(before, after)
        (build / 'build.ninja').write_text(build_text)
        print('Building', variant, flush=True)
        subprocess.run([str(Path(ninja.BIN_DIR) / 'ninja'), '-j', os.environ.get('MAX_JOBS', '2')], cwd=build, check=True)
        manifest[variant]['library_sha256'] = hashlib.sha256((build / 'gsplat_cuda.so').read_bytes()).hexdigest()
    (TARGET / 'manifest.json').write_text(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    main()
