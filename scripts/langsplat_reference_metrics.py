"""Run pinned upstream metric functions with visualization replaced by no-ops.

Keep both the project's fixed-threshold/polygon metric and upstream's smoothed,
normalized, multi-level mask/bounding-box protocol. Never mix their numbers.
"""
from __future__ import annotations
import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Dict
import cv2
import numpy as np
import torch


def smooth_majority(mask):
    """Exact upstream seven-pixel window, including its exclusive H-1/W-1 edge."""
    h,w = mask.shape
    if min(h,w)<2:
        raise ValueError('Upstream majority filter needs at least a 2x2 grid')
    integral = cv2.integral(mask.astype(np.uint8),sdepth=cv2.CV_32S)
    y0,y1 = np.maximum(np.arange(h)-3,0),np.minimum(np.arange(h)+4,h-1)
    x0,x1 = np.maximum(np.arange(w)-3,0),np.minimum(np.arange(w)+4,w-1)
    ones = integral[y1[:,None],x1]-integral[y0[:,None],x1]-integral[y1[:,None],x0]+integral[y0[:,None],x0]
    size = (y1-y0)[:,None]*(x1-x0)
    return (2*ones>size).astype(mask.dtype)


def annotations(path, *, rounded=False):
    record = __import__('json').loads(Path(path).read_text())
    size = (record['info']['height'],record['info']['width'])
    result = {}
    for item in record['objects']:
        entry = result.setdefault(item['category'],dict(mask=np.zeros(size,np.uint8),bboxes=[]))
        polygon = np.asarray(item['segmentation'])
        if rounded:
            polygon = np.rint(polygon)
        cv2.fillPoly(entry['mask'],[polygon.astype(np.int32)],1)
        entry['bboxes'].append(np.asarray(item['bbox']).reshape(4))
    for entry in result.values():
        entry['bboxes'] = np.stack(entry['bboxes'])
    return result


class NoFiles:
    def __init__(self,name=''):
        self.name = name
    def __truediv__(self,name):
        return NoFiles(str(name))
    @property
    def parent(self):
        return self
    def mkdir(self,*args,**kwargs):
        pass


def load_metrics(upstream):
    """Execute the actual AST bodies, including source head selection and rescale."""
    masks = {}
    def capture(mask,path):
        if path.name.startswith('chosen_'):
            masks[path.name[7:-4]] = mask.copy()
    palette = SimpleNamespace(ColormapOptions=lambda *a,**k:None,
                              apply_colormap=lambda value,*a:value.repeat(1,1,3))
    namespace = dict(torch=torch,np=np,cv2=cv2,Path=Path,Dict=Dict,
                     smooth=smooth_majority,colormaps=palette,
                     colormap_saving=lambda *a,**k:None,vis_mask_save=capture,
                     show_result=lambda *a,**k:None)
    source = Path(upstream)/'eval/evaluate_iou_loc.py'
    tree = ast.parse(source.read_text())
    nodes = [node for node in tree.body if isinstance(node,ast.FunctionDef)
             and node.name in ('activate_stream','lerf_localization')]
    if len(nodes)!=2:
        raise ValueError('Pinned upstream metric entry points missing')
    exec(compile(ast.Module(body=nodes,type_ignores=[]),str(source),'exec'),namespace)
    # Verify the acceleration against upstream's actual implementation on edges/ties.
    utils = Path(upstream)/'eval/utils.py'
    function = [n for n in ast.parse(utils.read_text()).body if isinstance(n,ast.FunctionDef) and n.name=='smooth']
    check = dict(np=np)
    exec(compile(ast.Module(body=function,type_ignores=[]),str(utils),'exec'),check)
    for seed in range(3):
        mask = np.random.default_rng(seed).integers(0,2,(17,19),dtype=np.uint8)
        np.testing.assert_array_equal(check['smooth'](mask),smooth_majority(mask))
    return namespace,masks


def query_mask(maps, threshold=.4):
    """The upstream activation/mask operations, without needing any annotations."""
    maps = np.asarray(maps,np.float32)
    if maps.ndim!=3 or not np.isfinite(maps).all():
        raise ValueError('Query expects finite level/height/width relevance maps')
    values = torch.from_numpy(maps.copy())
    kernel = np.ones((30,30))/(30**2)
    for level in range(len(values)):
        avg = torch.from_numpy(cv2.filter2D(maps[level],-1,kernel))
        values[level] = .5*(avg+values[level])
    chosen = int(values.flatten(1).max(1).values.argmax())
    output = values[chosen]
    output = output-output.min()
    output = output/(output.max()+1e-9)
    output = (output*2-1).clamp(0,1)
    return chosen,smooth_majority((output.numpy()>threshold).astype(np.uint8))


def evaluate_maps(upstream, maps, names, annotation_path, *, source=None):
    """maps: levels x queries x original-GT-height x original-GT-width."""
    from minicolmap.semantics.evaluation import binary_metrics
    maps = np.asarray(maps,np.float32)
    if maps.ndim!=4 or maps.shape[1]!=len(names) or not np.isfinite(maps).all():
        raise ValueError('Invalid relevancy map grid')
    gt = annotations(annotation_path)
    rounded = annotations(annotation_path,rounded=True)
    gt = {name:gt[name] for name in names}
    if maps.shape[2:]!=next(iter(gt.values()))['mask'].shape:
        raise ValueError('Metric maps must use original annotation pixels')
    tensor = torch.from_numpy(maps.copy())
    if len(maps)==1:
        # Source localization assumes exactly three channels after cv2.filter2D.
        # Duplicating a single head leaves its scores, max and masks unchanged.
        tensor = tensor.repeat(3,1,1,1)
    class Maps:
        positives = names
        def get_max_across(self, sem_map):
            return tensor.clone()
    namespace,captured = source or load_metrics(upstream)
    captured.clear()
    # The upstream visualization path expects three-channel images; no files.
    image = torch.zeros((*maps.shape[2:],3))
    ious,levels = namespace['activate_stream'](None,image,Maps(),image_name=NoFiles(),
                                             img_ann=gt,thresh=.4,colormap_options=None)
    localization = namespace['lerf_localization'](None,image,Maps(),NoFiles(),gt)
    rows = []
    for i,name in enumerate(names):
        head = int(maps[:,i].reshape(len(maps),-1).max(1).argmax())
        heat = maps[head,i]
        common = binary_metrics(heat>.5,rounded[name]['mask']>0,heat)
        rows.append(dict(query=name,official_iou=float(ious[i]),official_level=int(levels[i])+1,
                         common_level=head+1,common=common))
    return dict(official=dict(mean_iou=float(np.mean(ious)),bbox_localization_rate=float(localization/len(names)),
                              mask_threshold=.4,filter_size=30,polygon_rounding='truncate'),
                common=dict(mean_iou=float(np.mean([r['common']['iou'] for r in rows])),
                            polygon_peak_rate=float(np.mean([r['common']['peak_inside_gt'] for r in rows])),
                            mask_threshold=.5,polygon_rounding='nearest'),per_query=rows),dict(captured)
