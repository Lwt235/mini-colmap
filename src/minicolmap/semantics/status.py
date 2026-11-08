"""Read-only status API: imports no torch and launches no GPU work."""
from pathlib import Path
import importlib.metadata
import json

from .project import exercise_status


def status(config):
    workspace = Path(config['workspace'])
    prepared_path = workspace / 'project.json'
    prepared = json.loads(prepared_path.read_text()) if prepared_path.is_file() else None
    manifest_path = Path(config['model_manifest'])
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    models = []
    for key in ('sam', 'clip'):
        item = manifest.get(key, {})
        path = Path(config['_root']) / item.get('path', 'models/missing')
        models.append({'name': key, 'ready': path.is_file() and path.stat().st_size == item.get('size'),
                       'size': item.get('size'), 'sha256': item.get('sha256'), 'source': item.get('source')})
    stages = {}
    for name in ('raw', 'regions', 'codes'):
        ids = [int(p.stem) for p in (workspace / name).glob('*.npz') if p.stem.isdigit()]
        stages[name] = {'count': len(ids), 'image_ids': sorted(ids)}
    for name in ('autoencoder', 'fusion', 'distilled'):
        stages[name] = {'checkpoint_exists': (workspace / name / 'checkpoint.pt').exists()}
        progress = workspace / name / 'progress.json'
        if progress.exists():
            stages[name]['progress'] = json.loads(progress.read_text())
    versions = {}
    for package in ('torch', 'torchvision', 'segment-anything', 'open-clip-torch', 'timm', 'huggingface-hub'):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    previews = [{'image_id': int(p.stem), 'url': f'/api/semantic/artifacts/raw/{p.name}'}
                for p in sorted((workspace / 'raw').glob('*.png'))[:12] if p.stem.isdigit()]
    queries = []
    for path in sorted((workspace / 'queries').glob('*/query.json'), reverse=True)[:12]:
        item = json.loads(path.read_text())
        queries.append({k: v for k, v in item.items() if k != 'directory'} |
                       {'url': f'/api/semantic/artifacts/queries/{path.parent.name}/overlay.png'})
    return {'name': config['name'], 'prepared': bool(prepared), 'models': models,
            'versions': versions, 'splits': {k: len(v) for k, v in prepared['splits'].items()} if prepared else {},
            'stages': stages, 'exercises': exercise_status(), 'previews': previews, 'queries': queries,
            'evaluation_scope': prepared['evaluation_scope'] if prepared else None,
            'last_command': json.loads((workspace / 'last-command.json').read_text()) if (workspace / 'last-command.json').exists() else None}
