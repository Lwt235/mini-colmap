"""Independent Gaussian selections; no changes to learned geometry or colors."""
from collections import deque
from pathlib import Path
import json
import numpy as np

SELECTION_ID='sink-v5'


def largest_spatial_component(points,cell_size):
    if len(points)==0:return np.zeros(0,dtype=bool),[]
    if not np.isfinite(points).all() or not np.isfinite(cell_size) or cell_size<=0:
        raise ValueError('Invalid selection geometry')
    cells=np.floor((points-points.min(axis=0))/cell_size).astype(np.int64)
    unique,inverse,counts=np.unique(cells,axis=0,return_inverse=True,return_counts=True)
    lookup={tuple(cell):i for i,cell in enumerate(unique)}
    remaining=set(lookup);components=[]
    offsets=[(x,y,z) for x in (-1,0,1) for y in (-1,0,1) for z in (-1,0,1) if (x,y,z)!=(0,0,0)]
    while remaining:
        seed=min(remaining);remaining.remove(seed);queue=deque([seed]);indices=[]
        while queue:
            cell=queue.popleft();indices.append(lookup[cell])
            for offset in offsets:
                neighbor=tuple(a+b for a,b in zip(cell,offset))
                if neighbor in remaining:remaining.remove(neighbor);queue.append(neighbor)
        components.append(indices)
    components.sort(key=lambda c:(-int(counts[c].sum()),min(c)))
    keep=np.zeros(len(unique),bool);keep[components[0]]=True
    return keep[inverse],[int(counts[c].sum()) for c in components]


def selection_directory(root,selection_id):
    from .selection_policy import valid_selection_id
    if not valid_selection_id(selection_id):raise ValueError('无效的选区编号')
    base=(Path(root)/'data/object_selections').resolve()
    directory=(base/selection_id).resolve()
    if not directory.is_relative_to(base):raise ValueError('选区目录越界')
    return directory


def load_selection(root,selection_id,*,source_sha256=None,count=None):
    directory=selection_directory(root,selection_id)
    metadata=json.loads((directory/'selection.json').read_text())
    if metadata.get('id')!=selection_id:raise ValueError('选区编号不匹配')
    if selection_id.startswith('scene-'):
        from .project import contained, load_config, project
        from .scene_worker import source_identity
        config=load_config(contained(root, metadata.get('scene_config','')),root=root)
        if (metadata.get('supervision')!='scene-distilled-v1' or config['schema_version']!=2
                or metadata.get('source_scene')!=config['name']
                or metadata.get('source_identity')!=source_identity(config,project(config))):
            raise ValueError('场景选区源模型已变化')
    elif selection_id!=SELECTION_ID:
        from .selection_policy import source_identity
        if metadata.get('source_identity')!=source_identity(root):raise ValueError('选区源模型已更新，请重新生成')
        if metadata.get('supervision')=='teacher-visible-v1':
            from .visible_lift import teacher_identity
            if metadata.get('teacher_identity')!=teacher_identity(root):raise ValueError('二维校验特征已更新，请重新生成')
        checked=json.loads((directory/'validation.json').read_text())
        if checked.get('selection_id')!=selection_id or not checked.get('evidence',{}).get('accepted'):
            raise ValueError('选区尚未通过一致性检查')
    if 'selection_sha256' in metadata:
        from .project import sha256
        if sha256(directory/'selection.npz')!=metadata['selection_sha256']:raise ValueError('选区索引文件校验失败')
    if source_sha256 and metadata.get('rgb_checkpoint_sha256')!=source_sha256:
        raise ValueError('选区与当前 RGB 模型不匹配')
    coordinates=np.asarray([metadata['center'],metadata['bounds']['min'],metadata['bounds']['max']],dtype=float)
    if coordinates.shape!=(3,3) or not np.isfinite(coordinates).all() or (coordinates[1]>coordinates[2]).any():
        raise ValueError('选区空间范围无效')
    if count is not None and metadata.get('source_gaussians')!=count:raise ValueError('选区源高斯数量不匹配')
    with np.load(directory/'selection.npz',allow_pickle=False) as data:indices=data['indices'].copy()
    if (indices.dtype!=np.int64 or indices.ndim!=1 or not len(indices) or (indices<0).any()
            or np.any(indices[1:]<=indices[:-1]) or len(indices)!=metadata['selected_gaussians']
            or (count is not None and indices.max()>=count)):
        raise ValueError('选区高斯索引无效')
    return metadata,indices


def catalogue(root):
    values=[]
    directory=selection_directory(root,SELECTION_ID)
    if all((directory/name).is_file() for name in ('selection.json','selection.npz','selection.zip')):
        validation=Path(root)/'reports/sink-selection-20261021/validation.json'
        try:
            checked=json.loads(validation.read_text())
            if checked.get('selection_id')==SELECTION_ID and checked.get('status')=='completed':
                metadata,_=load_selection(root,SELECTION_ID)
                values.append({**metadata,'export_url':f'/api/demo/selections/{SELECTION_ID}/export',
                    'inspection_url':f'/api/demo/selections/{SELECTION_ID}/report'})
        except (OSError,ValueError,KeyError):pass
    for path in sorted((Path(root)/'data/object_selections').glob('object-*/selection.json')):
        try:
            metadata,_=load_selection(root,path.parent.name)
            if not (path.parent/'selection.zip').is_file():continue
            sid=metadata['id']
            values.append({**metadata,'export_url':f'/api/demo/selections/{sid}/export',
                'inspection_url':f'/api/demo/selections/{sid}/report'})
        except (OSError,ValueError,KeyError):continue
    return values
