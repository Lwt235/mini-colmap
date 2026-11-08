"""Read-only diagnostic measurements, never used to choose deployed masks."""
import numpy as np


def mask_metrics(prediction,target):
    a,b=np.asarray(prediction,dtype=bool),np.asarray(target,dtype=bool)
    if a.shape!=b.shape:raise ValueError('Diagnostic masks must share the original pixel grid')
    tp=int((a&b).sum());fp=int((a&~b).sum());fn=int((~a&b).sum())
    return dict(iou=tp/max(1,tp+fp+fn),precision=tp/max(1,tp+fp),coverage=tp/max(1,tp+fn),
                prediction_pixels=int(a.sum()),gt_pixels=int(b.sum()),false_positive_pixels=fp,false_negative_pixels=fn)


def sam_union_oracle(labels,target):
    """Exact best IoU union of disjoint region labels, using GT for diagnosis ONLY.

    An optimum is a prefix sorted by foreground/area: adding a region improves
    Jaccard iff its purity exceeds J/(1+J). Not a deployable segmentation method.
    """
    labels=np.asarray(labels);target=np.asarray(target,dtype=bool)
    if labels.shape!=target.shape:raise ValueError('SAM labels and GT must share a grid')
    valid=labels>=0
    if not valid.any():return dict(iou=0.,region_ids=[],gt_coverage=0.)
    ids,inverse=np.unique(labels[valid],return_inverse=True)
    area=np.bincount(inverse);tp=np.bincount(inverse,weights=target[valid],minlength=len(ids))
    order=np.argsort(-tp/area,kind='stable');cum_tp=np.cumsum(tp[order]);cum_area=np.cumsum(area[order])
    iou=cum_tp/np.maximum(1,cum_area+target.sum()-cum_tp);best=int(iou.argmax())
    return dict(iou=float(iou[best]),region_ids=ids[order[:best+1]].tolist(),
                gt_coverage=float(cum_tp[best]/max(1,int(target.sum()))))


def pool_region_codes(latent,labels,region_count):
    """One inference-only spatial control: replace pixels by SAM-region mean codes."""
    latent=np.asarray(latent);labels=np.asarray(labels)
    if latent.ndim!=3 or latent.shape[1:]!=labels.shape:raise ValueError('Invalid latent/label grids')
    valid=labels>=0
    if valid.any() and labels[valid].max()>=region_count:raise ValueError('Foreign region index')
    count=np.bincount(labels[valid],minlength=region_count)
    sums=np.stack([np.bincount(labels[valid],weights=channel[valid],minlength=region_count) for channel in latent],1)
    return (sums/np.maximum(1,count[:,None])).astype(np.float32),count


def projected_support(anchor,target,depth,alpha,mask):
    """Source annotated surface in target FoV; target occlusion is not measured."""
    y,x=np.where((np.asarray(mask)>0)&(alpha>.95)&np.isfinite(depth)&(depth>.01))
    rays=np.stack([x,y,np.ones(len(x))],1)@np.linalg.inv(anchor['K']).T
    points=(rays*depth[y,x,None]-np.asarray(anchor['t']))@np.asarray(anchor['R'])
    xyz=points@np.asarray(target['R']).T+np.asarray(target['t'])
    uv=xyz@np.asarray(target['K']).T
    xy=uv[:,:2]/np.maximum(uv[:,2:],1e-8)
    inside=(xyz[:,2]>.01)&(xy[:,0]>=0)&(xy[:,0]<target['width'])&(xy[:,1]>=0)&(xy[:,1]<target['height'])
    return dict(source_gt_samples=len(points),projected_in_frame=int(inside.sum()),
        in_frame_fraction=float(inside.mean()) if len(points) else None,
        scope='Source annotated surface with frozen RGB expected depth; in-frame support only, not target occlusion or target GT')
