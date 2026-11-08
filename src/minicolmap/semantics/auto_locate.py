"""CPU-only camera search policy; no labels or annotated test cameras."""
import hashlib
import json
import numpy as np
from .selection_policy import normalized_text

SEARCH_POLICY = dict(version='auto-locate-v3', max_edge=320, coarse_views=18,
    refinement_views=10, min_peak=.55, min_contrast=.04, min_pixels=80,
    max_area_fraction=.65, cluster_radius_scale=.04, min_support_views=3,
    min_reprojection_iou=.25, min_foreground_agreement=.5, ambiguous_score_ratio=.8,
    min_box_overlap=.25, selection_visibility='field-visible-v1')


def compatible_support(a,b,scale):
    """Large objects have moving visible centroids; also compare surface bounds.

    This only permits a reprojection comparison; it never proves object identity.
    """
    if np.linalg.norm(np.asarray(a['center'])-b['center'])<=SEARCH_POLICY['cluster_radius_scale']*scale:return True
    alo,ahi=np.asarray(a['bounds']['min']),np.asarray(a['bounds']['max'])
    blo,bhi=np.asarray(b['bounds']['min']),np.asarray(b['bounds']['max'])
    overlap=np.maximum(0,np.minimum(ahi,bhi)-np.maximum(alo,blo)).prod()
    volume=min(np.maximum(ahi-alo,0).prod(),np.maximum(bhi-blo,0).prod())
    return volume>0 and overlap/volume>=SEARCH_POLICY['min_box_overlap']


def search_key(text, identity):
    value=dict(text=normalized_text(text),source=identity,policy=SEARCH_POLICY)
    return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()[:32]


def pose_features(prepared):
    frames={f['image_id']:f for f in prepared['frames']}
    ids=sorted(set(prepared['splits']['train']))
    values=[]
    for i in ids:
        f=frames[i];R=np.asarray(f['R']);C=-R.T@np.asarray(f['t'])
        values.append(np.r_[C/prepared['normalization']['scale'],.4*R[2]])
    return ids,np.asarray(values)


def coarse_views(prepared):
    ids,values=pose_features(prepared)
    if not ids:return []
    chosen=[int(np.argmin(np.linalg.norm(values-values.mean(0),axis=1)))]
    for _ in range(min(len(ids),SEARCH_POLICY['coarse_views'])-1):
        distances=np.min(np.linalg.norm(values[:,None]-values[chosen][None,:],axis=2),axis=1)
        distances[chosen]=-1
        chosen.append(int(np.argmax(distances)))
    return [ids[i] for i in chosen]


def refine_views(prepared, ranked, seen):
    ids,values=pose_features(prepared);lookup={i:n for n,i in enumerate(ids)}
    anchors=[r['image_id'] for r in ranked[:3] if r['image_id'] in lookup]
    candidates=[]
    for anchor in anchors:
        order=np.argsort(np.linalg.norm(values-values[lookup[anchor]],axis=1),kind='stable')
        candidates.append([ids[n] for n in order if ids[n] not in seen])
    result=[]
    for n in range(len(ids)):
        for rows in candidates:
            if n<len(rows) and rows[n] not in result:result.append(rows[n])
            if len(result)==SEARCH_POLICY['refinement_views']:return result
    return result


def response_score(maps, mask, level):
    values=np.asarray(maps[level]);fg=values[mask>0];bg=values[mask==0]
    count=len(fg);fraction=count/values.size
    if not count or not len(bg):return None
    peak=float(np.percentile(fg,95));contrast=peak-float(np.median(bg))
    if (count<SEARCH_POLICY['min_pixels'] or fraction>SEARCH_POLICY['max_area_fraction']
            or peak<SEARCH_POLICY['min_peak'] or contrast<SEARCH_POLICY['min_contrast']):return None
    # Ranking score, not probability: discourage tiny isolated peaks and broad masks.
    score=(peak-.5)*min(1.,np.sqrt(fraction/.08))+ .15*contrast
    return dict(score=float(score),peak=peak,contrast=contrast,area_fraction=fraction,pixels=count)
