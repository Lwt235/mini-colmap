import json
import numpy as np
import pytest
from minicolmap.semantics.selection import largest_spatial_component,load_selection,selection_directory,SELECTION_ID


def fixture(root,indices=(0,2,4)):
    directory=selection_directory(root,SELECTION_ID);directory.mkdir(parents=True)
    metadata=dict(id=SELECTION_ID,source_gaussians=10,selected_gaussians=len(indices),rgb_checkpoint_sha256='source',center=[0,0,0],bounds={'min':[-1,-1,-1],'max':[1,1,1]})
    (directory/'selection.json').write_text(json.dumps(metadata))
    np.savez(directory/'selection.npz',indices=np.array(indices,dtype=np.int64))
    return directory


def test_spatial_components_remove_detached_points_without_reordering():
    points=np.array([[0,0,0],[.09,.09,0],[.18,.18,0],[5,5,5],[5.01,5,5]])
    mask,sizes=largest_spatial_component(points,.1)
    assert mask.tolist()==[True,True,True,False,False] and sizes==[3,2]
    assert largest_spatial_component(points[:0],.1)[0].size==0


def test_selection_binds_exact_source_and_original_gaussian_indices(tmp_path):
    fixture(tmp_path)
    metadata,indices=load_selection(tmp_path,SELECTION_ID,source_sha256='source',count=10)
    assert indices.tolist()==[0,2,4]
    for kwargs in ({'source_sha256':'other'},{'count':11}):
        with pytest.raises(ValueError):load_selection(tmp_path,SELECTION_ID,**kwargs)
    with pytest.raises(ValueError):selection_directory(tmp_path,'../../other')


@pytest.mark.parametrize('indices',[(2,2,4),(-1,2,4),(4,2,0),(0,2,11)])
def test_selection_rejects_invalid_or_foreign_index_arrays(tmp_path,indices):
    fixture(tmp_path,indices)
    with pytest.raises(ValueError):load_selection(tmp_path,SELECTION_ID,count=10)
