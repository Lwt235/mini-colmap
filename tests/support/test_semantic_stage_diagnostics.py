import itertools
import numpy as np
import pytest
from minicolmap.semantics.diagnostics import mask_metrics,sam_union_oracle,pool_region_codes


def test_sam_union_oracle_matches_exhaustive_search_for_disjoint_regions():
    rng=np.random.default_rng(21)
    for _ in range(20):
        labels=rng.integers(-1,5,size=(8,9));target=rng.random((8,9))>.7
        result=sam_union_oracle(labels,target)
        actual=max(mask_metrics(np.isin(labels,subset),target)['iou']
            for n in range(6) for subset in itertools.combinations(range(5),n))
        assert result['iou']==pytest.approx(actual)
        assert mask_metrics(np.isin(labels,result['region_ids']),target)['iou']==pytest.approx(actual)


def test_pooling_preserves_region_means_and_never_uses_invalid_label_minus_one():
    labels=np.array([[0,0,-1],[1,1,1]])
    codes=np.array([[[1,3,1000],[6,9,12]],[[2,4,1000],[8,11,14]]],dtype=np.float32)
    means,counts=pool_region_codes(codes,labels,3)
    np.testing.assert_allclose(means,[[2,3],[9,11],[0,0]])
    assert counts.tolist()==[2,3,0]
    with pytest.raises(ValueError):pool_region_codes(codes,labels,1)
    with pytest.raises(ValueError):mask_metrics(np.zeros((2,3)),np.zeros((3,2)))


def test_projected_support_distinguishes_out_of_frame_from_visible_claims():
    from minicolmap.semantics.diagnostics import projected_support
    f=dict(width=10,height=10,K=[[10,0,4.5],[0,10,4.5],[0,0,1]],R=np.eye(3).tolist(),t=[0,0,0])
    depth=np.full((10,10),2.);alpha=np.ones((10,10));mask=np.zeros((10,10));mask[3:7,3:7]=1
    same=projected_support(f,f,depth,alpha,mask)
    assert same['source_gt_samples']==16 and same['in_frame_fraction']==1
    other=projected_support(f,{**f,'t':[10,0,0]},depth,alpha,mask)
    assert other['in_frame_fraction']==0
    empty=projected_support(f,f,depth,np.zeros_like(alpha),mask)
    assert empty['in_frame_fraction'] is None
