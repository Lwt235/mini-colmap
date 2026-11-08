"""Versioned, geometry-visible teacher-assisted lifting; unchanged release gates."""
import hashlib
import json
from pathlib import Path
import numpy as np
from .selection_policy import selection_key

VISIBLE_POLICY=dict(version='teacher-visible-v1',max_view_candidates=24,min_in_frame=.3,
    min_reprojection_iou=.25,min_foreground_agreement=.6,max_mask_fraction=.65,
    min_corresponding_foreground=25)


def teacher_identity(root):
    base=Path(root)/'data/langsplat_reference/language_features'
    files=sorted([*base.glob('*_s.npy'),*base.glob('*_f.npy')])
    if not files:raise ValueError('二维区域特征不可用')
    stamps=[(p.name,p.stat().st_size,p.stat().st_mtime_ns) for p in files]
    return hashlib.sha256(json.dumps(stamps).encode()).hexdigest()


def visible_selection_key(text,anchor,source,teacher,hint=None):
    return selection_key(text,anchor,source,dict(policy=VISIBLE_POLICY,teacher_identity=teacher,hint_bounds=hint))


def surface_points(frame,depth,alpha,mask):
    y,x=np.where((mask>0)&(alpha>.95)&np.isfinite(depth)&(depth>.01))
    if len(x)>12000:
        step=int(np.ceil(len(x)/12000));x,y=x[::step],y[::step]
    rays=np.stack([x,y,np.ones(len(x))],1)@np.linalg.inv(frame['K']).T
    return (rays*depth[y,x,None]-np.asarray(frame['t']))@np.asarray(frame['R'])


def in_frame_fraction(points,frame):
    if not len(points):return 0.
    xyz=points@np.asarray(frame['R']).T+np.asarray(frame['t'])
    uv=xyz@np.asarray(frame['K']).T;uv=uv[:,:2]/np.maximum(uv[:,2:],1e-8)
    inside=(xyz[:,2]>.01)&(uv[:,0]>=0)&(uv[:,0]<frame['width'])&(uv[:,1]>=0)&(uv[:,1]<frame['height'])
    return float(inside.mean())
