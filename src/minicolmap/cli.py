"""CLI for reconstruction, rendering, semantic fields and dependency diagnostics."""
import argparse
import importlib
import json
import sys
from pathlib import Path


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == 'semantic':
        from .semantics.cli import main as semantic_main
        return semantic_main(argv[1:])
    parser = argparse.ArgumentParser(description='Mini-COLMAP: incremental SfM and neural view synthesis')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('semantic', help='SAM/CLIP semantic feature and language-field stages (L1–L7)')
    sub.add_parser('doctor', help='Check actual dependency imports')
    ui = sub.add_parser('ui', help='Open the local reconstruction and 3D visualization workbench')
    ui.add_argument('--port', type=int, default=8765)
    ui.add_argument('--no-browser', action='store_true', help='Start without opening a browser tab')
    ui.add_argument('--project', type=Path, help='Project root containing data/ and outputs/')
    synthetic = sub.add_parser('synthetic', help='Generate a geometry fixture with known correspondences')
    synthetic.add_argument('--output', type=Path, default=Path('data/synthetic.npz'))
    synthetic.add_argument('--seed', type=int, default=0)
    synthetic.add_argument('--points', type=int, default=80)
    synthetic.add_argument('--cameras', type=int, default=4)
    synthetic.add_argument('--noise-px', type=float, default=0.0)
    synthetic.add_argument('--outlier-ratio', type=float, default=0.0)
    run = sub.add_parser('run', help='Run incremental SfM from calibrated photos')
    run.add_argument('--dataset', type=Path, required=True)
    run.add_argument('--output', type=Path, required=True)
    run.add_argument('--config', type=Path)
    inspect = sub.add_parser('inspect', help='Inspect a saved reconstruction, without ground truth')
    inspect.add_argument('model', type=Path)
    export = sub.add_parser('export', help='Export a saved model to COLMAP text and PLY')
    export.add_argument('model', type=Path)
    export.add_argument('--output', type=Path, required=True)
    train = sub.add_parser('train', help='Train NeRF from posed photos; initialize 3DGS from an existing point model')
    train.add_argument('--model', type=Path, help='Optional for NeRF with a posed dataset; required for 3DGS')
    train.add_argument('--dataset', type=Path, required=True)
    train.add_argument('--output', type=Path, required=True)
    train.add_argument('--method', choices=('nerf', '3dgs'))
    train.add_argument('--config', type=Path)
    train.add_argument('--checkpoint', type=Path, help='Fine-tune a matching 3DGS checkpoint with fresh Adam moments')
    render = sub.add_parser('render', help='Render a saved training checkpoint from an existing camera')
    render.add_argument('--run', type=Path, required=True)
    render.add_argument('--image-id', type=int, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'ui':
            if not 1 <= args.port <= 65535:
                raise ValueError('--port must be between 1 and 65535')
            from .ui import run as run_ui
            run_ui(port=args.port, open_browser=not args.no_browser, project_root=args.project)
            return 0
        if args.command == 'doctor':
            missing = []
            for name in ('numpy', 'cv2', 'scipy', 'pytest'):
                try:
                    module = importlib.import_module(name)
                    print(f'{name}: {module.__version__}')
                except ImportError:
                    missing.append(name)
                    print(f'{name}: MISSING')
            print('Dependency checks do not certify reconstruction or rendering quality; see docs/evaluation.md.')
            return 1 if missing else 0
        if args.command == 'synthetic':
            from .synthetic import generate_scene, save_scene
            if args.output.exists():
                raise ValueError('Fixture already exists; choose a different --output')
            scene = generate_scene(seed=args.seed, num_points=args.points, num_cameras=args.cameras,
                                   noise_px=args.noise_px, outlier_ratio=args.outlier_ratio)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            save_scene(scene, args.output)
            print(f'Geometry fixture saved: {args.output.resolve()} (not a SIFT or SfM result)')
            return 0
        if args.command == 'train':
            from .rendering.config import resolve_render_config
            from .rendering.pipeline import train as train_rendering
            settings = json.loads(args.config.read_text(encoding='utf-8-sig')) if args.config else {}
            if not isinstance(settings, dict):
                raise ValueError('Rendering config must be a JSON object')
            if args.method:
                settings['method'] = args.method
            extra = {'initial_checkpoint': args.checkpoint} if args.checkpoint is not None else {}
            result = train_rendering(args.model, args.dataset, args.output, resolve_render_config(settings), **extra)
        elif args.command == 'render':
            from .rendering.pipeline import render_checkpoint
            result = render_checkpoint(args.run, image_id=args.image_id)
        elif args.command == 'run':
            from .config import load_config
            from .pipeline import reconstruct
            result = reconstruct(args.dataset, args.output, load_config(args.config))
        else:
            from .io import load_reconstruction
            from .metrics import reconstruction_summary
            model = load_reconstruction(args.model)
            if args.command == 'export':
                from .export import write_colmap_text, write_ply
                if args.output.exists() and any(args.output.iterdir()):
                    raise ValueError('Export output must be empty; choose a new directory')
                args.output.mkdir(parents=True, exist_ok=True)
                write_colmap_text(model, args.output / 'sparse' / '0')
                write_ply(model, args.output / 'points.ply')
            result = reconstruction_summary(model)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except NotImplementedError as exc:
        print(f'Algorithm not implemented: {exc}', file=sys.stderr)
        print('See docs/architecture.md and docs/evaluation.md for algorithm modules and validation.', file=sys.stderr)
        return 2
    except (ValueError, RuntimeError, OSError, KeyError, ImportError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
