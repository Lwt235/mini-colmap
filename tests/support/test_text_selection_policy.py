import pytest
from minicolmap.semantics.selection_policy import assess_evidence, normalized_text, selection_key, valid_selection_id


def evidence():
    rows = [dict(query_pixels=200, selected_query_precision=.8, query_coverage=.7, query_iou=.6) for _ in range(5)]
    consistency = [dict(reprojection_iou=.7, foreground_agreement=.8) for _ in range(5)]
    return rows, consistency


def test_evidence_requires_both_independent_views_and_reprojection():
    rows, consistency = evidence()
    assert assess_evidence(rows,consistency,200,10000)['accepted']
    for row in rows[:3]:row['query_coverage']=.1
    result=assess_evidence(rows,consistency,200,10000)
    assert not result['accepted'] and result['good_validation_views']==2
    rows, consistency = evidence()
    for row in consistency[:3]:row['reprojection_iou']=None
    result=assess_evidence(rows,consistency,200,10000)
    assert not result['accepted'] and result['consistent_fit_views']==2


def test_evidence_does_not_publish_empty_small_or_scene_wide_regions():
    rows, consistency = evidence()
    for count in (0,99,1501):assert not assess_evidence(rows,consistency,count,10000)['accepted']
    for row in rows:row['query_pixels']=10
    assert not assess_evidence(rows,consistency,200,10000)['accepted']
    assert not assess_evidence([],consistency,200,10000)['accepted']


def test_identity_normalizes_text_and_invalidates_changed_camera_or_source():
    key=selection_key(' Dark   CUP ',124,{'rgb':'a'})
    assert key==selection_key('dark cup',124,{'rgb':'a'}) and valid_selection_id(key)
    assert key!=selection_key('dark cup',125,{'rgb':'a'})
    assert key!=selection_key('dark cup',124,{'rgb':'b'})
    for value in ('../../x','object-foo',None,[], 'object-'+'a'*25):assert not valid_selection_id(value)
    for value in ('', 'x\n', [], 'a'*161):
        with pytest.raises(ValueError):normalized_text(value)
