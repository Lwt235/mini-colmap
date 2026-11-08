"""Replay M1 from saved matches without running features/RANSAC or modifying a run.

Examples (PowerShell, from project root)::

    .venv/Scripts/python.exe scripts/profile_initialization.py outputs/RUN --repeat 2 --output reports/init-default.json
    .venv/Scripts/python.exe scripts/profile_initialization.py outputs/RUN --repeat 2 --blas-threads 1 --output reports/init-one-thread.json

Thread settings affect this diagnostic process only. Timings of nested calls
overlap; do not add all detail rows. --profile adds cProfile overhead and writes
separate .prof/.txt files. No algorithm implementation is replaced.
"""

import argparse
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runs', nargs='+', type=Path)
    parser.add_argument('--repeat', type=int, default=1)
    parser.add_argument('--blas-threads', type=int)
    parser.add_argument('--profile', action='store_true')
    parser.add_argument('--qos', choices=('high', 'auto'), help='Process-local Windows QoS comparison; default leaves scheduling untouched')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.repeat < 1 or (args.blas_threads is not None and args.blas_threads < 1):
        parser.error('repeat and blas-threads must be positive')
    for run in args.runs:
        if args.output.resolve().is_relative_to(run.resolve()):
            parser.error('output must be outside the source run directory')
    if args.blas_threads is not None:
        # Must happen before importing NumPy/SciPy; no persistent environment edits.
        for name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS'):
            os.environ[name] = str(args.blas_threads)
    runtime = None
    if args.qos is not None:
        os.environ['MINICOLMAP_WORKER_QOS'] = args.qos
        from minicolmap.ui.runtime import configure_worker_runtime
        runtime = configure_worker_runtime()

    import cProfile
    import ctypes
    import hashlib
    import io
    import json
    import pstats
    import sys
    from contextlib import ExitStack
    from functools import wraps
    from time import perf_counter, process_time
    from unittest.mock import patch

    import numpy as np
    import scipy
    import scipy.optimize._lsq.trf as trf
    import einx
    from minicolmap import geometry, mapper
    from minicolmap.config import load_config
    from minicolmap.io import load_dataset, reconstruction_to_dict
    from minicolmap.models import Reconstruction
    from minicolmap.tracks import build_tracks

    def read_json(path):
        return json.loads(path.read_text(encoding='utf-8-sig'))

    def blas_info():
        result = []
        if os.name == 'nt':
            for module in (np, scipy):
                for dll_path in (Path(module.__file__).parent.parent / (module.__name__ + '.libs')).glob('*openblas*.dll'):
                    dll = ctypes.WinDLL(str(dll_path))
                    for symbol in ('scipy_openblas_get_num_threads64_', 'scipy_openblas_get_num_threads'):
                        try:
                            fn = getattr(dll, symbol)
                        except AttributeError:
                            continue
                        fn.restype = ctypes.c_int
                        result.append({'library': str(dll_path), 'threads': fn()})
                        break
        return result

    inputs = []
    for run in args.runs:
        run = run.resolve()
        config = load_config(run / 'config.json')
        manifest = read_json(run / 'run_status.json')['dataset']
        data = load_dataset(manifest)
        colors = {}
        for image_id, image in data['images'].items():
            with np.load(run / 'features' / f'{image_id}.npz', allow_pickle=False) as cache:
                image.keypoints = cache['keypoints']
                colors[image_id] = cache['colors']
        pairs = []
        for row in read_json(run / 'pairs.json'):
            if row['inliers'] < config['geometry']['min_inliers']:
                continue
            i, j = row['images']
            with np.load(run / 'matches' / f'{i}_{j}.npz', allow_pickle=False) as cache:
                pairs.append(mapper.VerifiedPair(i, j, cache['candidates'][cache['inlier_mask']], cache['F']))
        tracks = build_tracks({(p.image_id1, p.image_id2): p.indices for p in pairs})
        inputs.append((run, config, data, colors, pairs, tracks))

    report = {
        'python': sys.version, 'numpy': np.__version__, 'scipy': scipy.__version__,
        'cpu_count': os.cpu_count(), 'blas': blas_info(),
        'python_trace_active': sys.gettrace() is not None,
        'runtime': runtime,
        'thread_environment': {k: os.environ.get(k) for k in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS')},
        'profile_enabled': args.profile,
        'note': 'Details are inclusive and overlap. Replay excludes feature extraction, matching, UI and disk writes.',
        'measurements': [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for repeat in range(args.repeat):
        # Reverse on alternate rounds to reduce a consistent order/warmup bias.
        ordered = inputs if repeat % 2 == 0 else inputs[::-1]
        for run, config, data, colors, pairs, tracks in ordered:
            details, candidate_rows = {}, []
            current_pair = [None]

            def timed(name, function):
                @wraps(function)
                def call(*a, **kw):
                    start, cpu = perf_counter(), process_time()
                    try:
                        result = function(*a, **kw)
                    finally:
                        elapsed = perf_counter() - start
                        row = details.setdefault(name, {'seconds': 0., 'cpu_seconds': 0., 'calls': 0})
                        row['seconds'] += elapsed
                        row['cpu_seconds'] += process_time() - cpu
                        row['calls'] += 1
                    if name == 'recover_relative_pose':
                        current_pair[0] = {'pose_seconds': elapsed, 'matches': len(a[1])}
                        candidate_rows.append(current_pair[0])
                    elif name == 'refine_two_view' and current_pair[0] is not None:
                        current_pair[0].update(refine_seconds=elapsed, seed_points=len(a[2]), **result[3])
                    elif name == 'lsmr':
                        row['iterations'] = row.get('iterations', 0) + int(result[2])
                    return result
                return call

            model = Reconstruction(data['cameras'], data['images'])
            diagnostics = []
            profiler = cProfile.Profile() if args.profile else None
            with ExitStack() as stack:
                for module, attr, name in (
                    (mapper, 'recover_relative_pose', 'recover_relative_pose'),
                    (mapper, 'triangulate_points', 'mapper_triangulation'),
                    (mapper, 'refine_two_view', 'refine_two_view'),
                    (mapper, 'validate_reconstruction', 'validate_map'),
                    (geometry, 'triangulate_points', 'pose_triangulation'),
                    (np.linalg, 'solve', 'numpy_solve'),
                    (einx, 'dot', 'einx_dot'),
                    (trf, 'lsmr', 'lsmr'),
                ):
                    stack.enter_context(patch.object(module, attr, timed(name, getattr(module, attr))))
                start, cpu = perf_counter(), process_time()
                if profiler:
                    profiler.enable()
                mapper.initialize_map(model, pairs, tracks, config, colors, diagnostics=diagnostics)
                if profiler:
                    profiler.disable()
                elapsed, cpu_elapsed = perf_counter() - start, process_time() - cpu

            for row, diagnostic in zip(candidate_rows, [d for d in diagnostics if 'seed_positive_points' in d]):
                row['images'] = diagnostic['images']
            fingerprint = hashlib.sha256(json.dumps(reconstruction_to_dict(model), sort_keys=True, allow_nan=False).encode()).hexdigest()
            saved_snapshot = run / 'ui' / 'snapshots' / 'snapshot-0001.json'
            saved_fingerprint = (hashlib.sha256(json.dumps(read_json(saved_snapshot), sort_keys=True, allow_nan=False).encode()).hexdigest()
                                 if saved_snapshot.is_file() else None)
            measurement = {
                'run': str(run), 'repeat': repeat + 1, 'wall_seconds': elapsed, 'cpu_seconds': cpu_elapsed,
                'pairs': len(pairs), 'tracks': len(tracks), 'selected_images': list(model.poses),
                'initial_points': len(model.points), 'model_sha256': fingerprint,
                'matches_saved_initial_snapshot': fingerprint == saved_fingerprint if saved_fingerprint else None,
                'refinement_nfev': sum(d.get('refinement', {}).get('nfev', 0) for d in diagnostics),
                'details': details, 'candidates': candidate_rows,
            }
            if profiler:
                prefix = args.output.with_name(f'{args.output.stem}-{run.name}-{repeat + 1}')
                profiler.dump_stats(str(prefix) + '.prof')
                buffer = io.StringIO()
                pstats.Stats(profiler, stream=buffer).strip_dirs().sort_stats('cumulative').print_stats(65)
                Path(str(prefix) + '.txt').write_text(buffer.getvalue(), encoding='utf-8')
            report['measurements'].append(measurement)
            args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
            print(json.dumps({k: v for k, v in measurement.items() if k not in ('details', 'candidates')}, ensure_ascii=False), flush=True)
    print(f'Report: {args.output.resolve()}', flush=True)


if __name__ == '__main__':
    main()
