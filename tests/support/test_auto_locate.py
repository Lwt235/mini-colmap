import numpy as np
from minicolmap.semantics.auto_locate import coarse_views,refine_views,search_key,response_score,compatible_support


def prepared():
    return dict(normalization={'scale':1},splits={'train':list(range(1,41)),'test':[85]},
        frames=[dict(image_id=i,R=np.eye(3).tolist(),t=[np.cos(i/10),np.sin(i/10),i/100]) for i in [*range(1,41),85]])


def test_coarse_cameras_cover_pose_and_refinement_never_uses_test_or_repeats():
    p=prepared();coarse=coarse_views(p)
    assert len(coarse)==18 and len(set(coarse))==18 and 85 not in coarse
    refined=refine_views(p,[{'image_id':coarse[0]},{'image_id':coarse[1]}],coarse)
    assert len(refined)==10 and not set(refined)&set(coarse) and 85 not in refined
    assert coarse_views(p)==coarse


def test_ranking_rejects_flat_or_tiny_activation_and_is_not_a_probability():
    mask=np.zeros((40,40),np.uint8);mask[10:30,10:30]=1
    maps=np.full((3,40,40),.48,np.float32)
    assert response_score(maps,mask,0) is None
    maps[0,mask>0]=.75
    assert response_score(maps,mask,0)['score']>0
    tiny=np.zeros_like(mask);tiny[15:17,15:17]=1
    assert response_score(maps,tiny,0) is None
    assert search_key(' Sink ',{'rgb':'a'})==search_key('sink',{'rgb':'a'})
    assert search_key('sink',{'rgb':'a'})!=search_key('sink',{'rgb':'b'})


def test_large_object_visible_centroids_may_shift_without_creating_two_instances():
    a=dict(center=[0,0,0],bounds={'min':[-1,-1,-1],'max':[1,1,1]})
    b=dict(center=[.5,0,0],bounds={'min':[-.5,-1,-1],'max':[1.5,1,1]})
    assert compatible_support(a,b,1)
    c=dict(center=[3,0,0],bounds={'min':[2.9,-.1,-.1],'max':[3.1,.1,.1]})
    assert not compatible_support(a,c,1)
