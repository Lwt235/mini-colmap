"""Read-only progress/results for the isolated LangSplat reference campaigns."""
from pathlib import Path
import json


def read(path):
    try:
        return json.loads(path.read_text())
    except (OSError,ValueError):
        return None


def codec_status(root):
    """Read experiment JSON only; never import models or allocate a GPU."""
    root=Path(root)
    report=root/'reports/semantic-codec-study-20261021'
    manifest=read(report/'manifest.json')
    if not manifest:
        return {'available':False}
    variants=[]
    for spec in manifest.get('variants',[]):
        name=spec['name']
        completed=read(root/'data/semantic_codec_study'/name/'completed.json')
        item={'name':name,'dim':spec['dim'],'trained':bool(completed),'metrics':{}}
        for split in ('dev','test'):
            summary=read(report/name/split/'summary.json')
            if summary:
                item['metrics'][split]={'results':summary['results'],'reconstruction':summary['reconstruction']}
        variants.append(item)
    images=[]
    for path in sorted(report.glob('*/dev/*dark_cup-heatmap.png')):
        mask=path.with_name(path.name.replace('-heatmap.png','-mask.png'))
        images.append({'name':path.parent.parent.name+' · dark cup · 二维重建',
            'heatmap_url':'/api/semantic/codec-study/artifacts/'+str(path.relative_to(report)),
            'mask_url':'/api/semantic/codec-study/artifacts/'+str(mask.relative_to(report))})
    return {'available':True,'complete':(report/'completed.json').exists(),
        'progress':read(report/'progress.json'),'variants':variants,'selection':read(report/'selection.json'),
        'images':images,'has_report':(report/'README.md').exists()}


def status(root):
    root = Path(root)
    report = root/'reports/langsplat-reference-20261021'
    if not (report/'source-manifest.json').is_file():
        return {'available':False}
    result = {'available':True,'protocols':{},'extraction':{'completed':len(list((root/'data/langsplat_reference/language_features').glob('*.json'))),'total':187}}
    for name,folder,workspace in (
            ('matched',report,root/'data/langsplat_reference'),
            ('full_view',report/'full-view-protocol',root/'data/langsplat_reference_full_views')):
        models = []
        for level in (1,2,3):
            directory = workspace/'train'/f'level{level}'
            completed = read(directory/'completed.json')
            progress = read(directory/'progress.json')
            models.append({'level':level,'completed':bool(completed),
                           'iteration':completed['iterations'] if completed else
                           progress['trace'][-1]['iteration'] if progress and progress.get('trace') else 0,
                           'total':30000})
        metrics = {}
        for split in ('dev','test'):
            entry = read(folder/'evaluation'/f'{split}-field'/'summary.json')
            if entry:
                metrics[split] = entry['results']
        images = []
        for path in sorted((folder/'queries').glob('*/query.json'),reverse=True)[:8]:
            query = read(path)
            if query:
                relative = str(path.parent.relative_to(report))
                images.append({'name':f"相机 {query['image_id']} · {query['text']} · 层级 {query['level']}",
                               'heatmap_url':'/api/semantic/reference/artifacts/'+relative+'/heatmap.png',
                               'mask_url':'/api/semantic/reference/artifacts/'+relative+'/mask.png'})
        for path in sorted((folder/'evaluation/dev-field').glob('*-heatmap.png')):
            relative = str(path.relative_to(report))
            mask = path.with_name(path.name.replace('-heatmap.png','-mask.png'))
            images.append({'name':path.stem.removesuffix('-heatmap'),
                           'heatmap_url':'/api/semantic/reference/artifacts/'+relative,
                           'mask_url':'/api/semantic/reference/artifacts/'+str(mask.relative_to(report))})
        result['protocols'][name] = {'controller':read(folder/'controller-status.json'),
            'progress':read(folder/'progress.json'),'models':models,'metrics':metrics,'images':images,
            'complete':(folder/'campaign-completed.json').is_file()}
    result['paired_protocol_2d'] = read(report/'paired-protocol-2d/summary.json')
    result['ae_old_data_control'] = read(report/'ae-old-data-control/summary.json')
    return result
