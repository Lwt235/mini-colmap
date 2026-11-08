import sys
from pathlib import Path
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
from check_semantic_multiview import reproject,select_views


def test_training_anchor_is_not_counted_twice_in_fit_views():
    prepared=dict(normalization={'scale':1},splits={'train':list(range(1,21))},
        frames=[dict(image_id=i,R=np.eye(3).tolist(),t=[i*.01,0,0]) for i in range(1,21)])
    ids=select_views(prepared,1)
    assert len(ids)==6 and len(set(ids))==6 and ids[0]==1


def test_reprojection_compares_shifted_corresponding_pixels_not_same_grid():
    K=[[10,0,9.5],[0,10,9.5],[0,0,1]]
    frame=dict(width=20,height=20,K=K,R=np.eye(3).tolist(),t=[0,0,0])
    depth=np.full((20,20),2.);alpha=np.ones((20,20))
    mask=np.zeros((20,20),np.uint8);mask[4:16,4:16]=1
    other={**frame,'t':[.4,0,0]}
    shifted=np.zeros_like(mask);shifted[4:16,6:18]=1
    result=reproject((frame,depth,alpha),(other,depth,alpha),mask,shifted,1.)
    assert result['reprojection_iou']==1 and result['foreground_agreement']==1
    wrong=reproject((frame,depth,alpha),(other,depth,alpha),mask,mask,1.)
    assert wrong['reprojection_iou']<1


def test_occlusion_does_not_count_invisible_objects_as_disagreement():
    frame=dict(width=20,height=20,K=[[10,0,9.5],[0,10,9.5],[0,0,1]],R=np.eye(3).tolist(),t=[0,0,0])
    depth=np.full((20,20),2.);alpha=np.ones((20,20));mask=np.ones((20,20),np.uint8)
    result=reproject((frame,depth,alpha),(frame,np.full_like(depth,.5),alpha),mask,mask,1.)
    assert result['anchor_foreground_rays']==0 and result['reprojection_iou'] is None
