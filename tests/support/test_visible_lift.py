import sys,json
from pathlib import Path
import numpy as np
from minicolmap.semantics.visible_lift import surface_points,in_frame_fraction,visible_selection_key
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
from geometry_visible_selection import GeometryPipeline


def frame(i,t):
    return dict(image_id=i,width=20,height=20,K=[[10,0,9.5],[0,10,9.5],[0,0,1]],R=np.eye(3).tolist(),t=t)


def test_visible_samples_exclude_absent_target_camera_and_invalid_depth():
    f=frame(101,[0,0,0]);depth=np.full((20,20),2.);alpha=np.ones_like(depth);mask=np.zeros_like(depth);mask[4:16,4:16]=1
    points=surface_points(f,depth,alpha,mask)
    assert len(points)==144 and in_frame_fraction(points,f)==1
    assert in_frame_fraction(points,frame(1,[10,0,0]))==0
    assert len(surface_points(f,depth,np.zeros_like(alpha),mask))==0
    assert visible_selection_key('sink',101,{'a':1},'teacher-a')!=visible_selection_key('sink',101,{'a':1},'teacher-b')


def test_geometry_fit_view_filter_replaces_out_of_frame_view_without_labels(tmp_path):
    frames={i:frame(i,[0 if i==101 else i*.001,0,0]) for i in [101,*range(1,9)]};frames[1]=frame(1,[10,0,0])
    worker=object.__new__(GeometryPipeline);worker.directory=tmp_path
    mask=np.zeros((20,20));mask[4:16,4:16]=1
    worker.views={101:dict(depth=np.full((20,20),2.),alpha=np.ones((20,20)),mask=mask)}
    worker.render_views=lambda *args:None
    p=dict(splits={'train':list(range(1,9))},normalization={'scale':1})
    selected=worker.prepare_fit_views(p,frames,101,[101,1,2,3,4,5],None,None,None)
    assert len(selected)==6 and selected[0]==101 and 1 not in selected
    audit=json.loads((tmp_path/'visible-view-audit.json').read_text())
    assert next(r for r in audit['views'] if r['image_id']==1)['eligible'] is False
