"""Stage commands, separate from ordinary RGB workbench training history."""
import argparse
import json
from pathlib import Path
import sys

from .project import load_config, prepare, project, write_json


def main(argv=None):
    parser = argparse.ArgumentParser(description='SAM/CLIP semantic feature and language-field stages L1–L7')
    parser.add_argument('stage', choices=('prepare', 'doctor', 'status', 'extract', 'regions',
                        'autoencoder', 'validate-autoencoder', 'encode', 'fuse', 'distill', 'query', 'evaluate'))
    parser.add_argument('--config', type=Path, default=Path('configs/semantic_waldo.json'))
    parser.add_argument('--split', choices=('train', 'dev', 'test', 'rgb_holdout', 'smoke', 'all'), default='train')
    parser.add_argument('--image-id', type=int)
    parser.add_argument('--limit', type=int)
    parser.add_argument('--mode', choices=('2d', 'reconstructed', 'fusion', 'distilled'), default='2d')
    parser.add_argument('--text')
    parser.add_argument('--verify', action='store_true', help='Doctor: hash all local model weights')
    args = parser.parse_args(argv)
    config = None
    try:
        config = load_config(args.config)
        if args.limit is not None and args.limit < 1:
            raise ValueError('--limit must be positive')
        if args.stage in ('doctor', 'status'):
            from .status import status
            result = status(config)
            if args.verify:
                from .encoders import model_assets
                model_assets(config, verify=True)
                result['model_hashes_verified'] = True
        elif args.stage == 'prepare':
            result = prepare(config)
            result = {k: v for k, v in result.items() if k not in ('frames', 'annotations')}
        else:
            from . import pipeline
            prepared = project(config)
            frames = pipeline.choose_frames(prepared, args.split, args.image_id, args.limit)
            write_json(Path(config['workspace']) / 'last-command.json', {'stage': args.stage, 'status': 'running'})
            if args.stage == 'regions':
                result = pipeline.regions(config, frames)
            else:
                from .encoders import gpu_session, extract
                with gpu_session(config):
                    if args.stage == 'extract':
                        result = extract(config, frames)
                    elif args.stage == 'autoencoder':
                        result = pipeline.fit_autoencoder(config, prepared)
                    elif args.stage == 'validate-autoencoder':
                        result = pipeline.validate_autoencoder(config, prepared, args.split)
                    elif args.stage == 'encode':
                        result = pipeline.encode(config, prepared, frames)
                    elif args.stage == 'fuse':
                        result = pipeline.fuse(config, prepared)
                    elif args.stage == 'distill':
                        result = pipeline.distill(config, prepared)
                    elif args.stage == 'query':
                        if args.image_id is None or not args.text:
                            raise ValueError('query requires --image-id and --text')
                        result = pipeline.query(config, prepared, frames[0], args.text, args.mode)
                    else:
                        from .evaluation import evaluate
                        result = evaluate(config, prepared, args.split, args.mode)
            write_json(Path(config['workspace']) / 'last-command.json', {'stage': args.stage, 'status': 'completed'})
        print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
        return 0
    except (NotImplementedError, ValueError, OSError, KeyError, RuntimeError, ImportError) as exc:
        todo = isinstance(exc, NotImplementedError)
        if config is not None and args.stage not in ('doctor', 'status', 'prepare'):
            write_json(Path(config['workspace']) / 'last-command.json',
                       {'stage': args.stage, 'status': 'todo' if todo else 'failed', 'message': str(exc)})
        print(('Algorithm not implemented: ' if todo else 'Error: ') + str(exc), file=sys.stderr)
        print('See docs/semantics.md; no replacement algorithm is executed.', file=sys.stderr)
        return 2 if todo else 1
