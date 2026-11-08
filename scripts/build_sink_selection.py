"""Build a conservative sink selection from six cached semantic/depth views.

No GPU, training, annotation lookup, or original model writes. All thresholds
are declared here before validation; output is a reviewable prototype selection.
"""
import json,zipfile
from pathlib import Path
import cv2
import numpy as np
import torch
from langsplat_reference_metrics import query_mask
from minicolmap.semantics.project import load_config,project,sha256,write_json
from minicolmap.semantics.selection import largest_spatial_component,selection_directory,SELECTION_ID

ROOT=Path(__file__).resolve().parents[1]
REPORT=ROOT/'reports/sink-selection-20261021'


def main():
    config=load_config(ROOT/'configs/semantic_waldo.json');prepared=project(config)
    output=selection_directory(ROOT,SELECTION_ID)
    if (output/'selection.json').exists():raise ValueError('Selection already exists; keep it immutable and use an explicit new version')
    REPORT.mkdir(parents=True,exist_ok=True);output.mkdir(parents=True,exist_ok=True)
    checkpoint=torch.load(Path(config['rgb_run'])/'checkpoint.pt',map_location='cpu',weights_only=True)
    state=checkpoint['state'];norm=checkpoint['normalization'];scale=norm['scale']
    points=state['means'].numpy().astype(np.float64)*scale+np.array(norm['center'])
    opacity=state['opacity_logits'].sigmoid().numpy()
    size=state['log_scales'].exp().numpy().max(axis=1)
    from minicolmap.rendering.algorithms import gaussian_covariance
    covariance=gaussian_covariance(state['log_scales'],state['quaternions']).numpy()*scale**2
    frames={f['image_id']:f for f in prepared['frames']}
    source=ROOT/'reports/semantic-multiview-20261021'
    manifest=json.loads((source/'manifest.json').read_text());ids=manifest['views']['124']
    positive=np.zeros(len(points),np.uint8);visible=np.zeros_like(positive);views=[]
    for image_id in ids:
        frame=frames[image_id]
        with np.load(source/f'{image_id:06d}.npz',allow_pickle=False) as data:
            depth,alpha=data['depth'],data['alpha'];level,mask=query_mask(data['maps'][:,0])
        interior=cv2.erode(mask,np.ones((5,5),np.uint8))
        camera=points@np.asarray(frame['R']).T+np.asarray(frame['t'])
        image=camera@np.asarray(frame['K']).T
        xy=np.rint(image[:,:2]/np.maximum(image[:,2:],1e-9)).astype(np.int64)
        x,y=xy.T;valid=(camera[:,2]>.01)&(x>=0)&(x<frame['width'])&(y>=0)&(y<frame['height'])
        index=np.flatnonzero(valid);xx,yy=x[index],y[index]
        axis=np.asarray(frame['R'])[2]
        sigma=np.sqrt(np.maximum(np.einsum('i,nij,j->n',axis,covariance[index],axis),0))
        match=(alpha[yy,xx]>.95)&(np.abs(camera[index,2]-depth[yy,xx])<.025*np.abs(depth[yy,xx])+.001*scale+2*sigma)
        index=index[match];xx,yy=x[index],y[index]
        visible[index]+=1;positive[index]+=interior[yy,xx].astype(np.uint8)
        views.append(dict(image_id=image_id,level=level+1,visible_gaussians=len(index),positive_gaussians=int(interior[yy,xx].sum()),cache_sha256=sha256(source/f'{image_id:06d}.npz')))
    # Several audit views see only a corner of the basin. Requiring three
    # sightings selects their tiny common intersection instead of the object.
    # Two positive views with >=80% agreement still reject single-view floaters.
    candidates=(visible>=2)&(positive>=2)&(positive>=.8*visible)&(opacity>=.005)&(size<=.03)
    candidate_indices=np.flatnonzero(candidates)
    connected,components=largest_spatial_component(points[candidates],.01*scale)
    indices=candidate_indices[connected].astype(np.int64)
    if len(indices)<100:raise ValueError('Insufficient connected sink support; do not publish an empty/weak selection')
    selected=points[indices];minimum=selected.min(axis=0);maximum=selected.max(axis=0)
    center=np.average(selected,axis=0,weights=opacity[indices])
    metadata=dict(schema_version=1,id=SELECTION_ID,name='水槽',text='sink',model='raw3',anchor_image_id=124,
        status='prototype',rgb_checkpoint_sha256=prepared['rgb_checkpoint_sha256'],source_gaussians=len(points),
        selected_gaussians=len(indices),center=center.tolist(),bounds={'min':minimum.tolist(),'max':maximum.tolist()},
        coordinate_system='original_sfm_world',units='reconstruction units; metric scale not calibrated',
        view_ids=ids,method='Gaussian 2-sigma view-depth support voting, eroded query masks, largest 26-connected spatial voxel component',
        parameters=dict(min_views=2,min_positive_votes=2,min_positive_fraction=.8,min_opacity=.005,max_normalized_sigma=.03,
            alpha_min=.95,relative_depth_tolerance=.025,absolute_depth_tolerance_scale=.001,view_depth_sigmas=2,erosion_pixels=5,voxel_size_scale=.01),
        candidates=len(candidate_indices),component_sizes=components,views=views,
        scope='Conservative visible surface prototype; not a watertight object mesh or annotated 3D ground truth; hidden surfaces may be omitted')
    np.savez_compressed(output/'selection.npz',indices=indices,positive_votes=positive[indices],visible_votes=visible[indices])
    write_json(output/'selection.json',metadata)
    np.savez_compressed(output/'gaussians.npz',indices=indices,means_world=selected.astype(np.float32),
        log_scales_world=(state['log_scales'][indices].numpy()+np.log(scale)),quaternions_wxyz=state['quaternions'][indices].numpy(),
        opacity_logits=state['opacity_logits'][indices].numpy(),sh_coeffs=state['sh_coeffs'][indices].numpy())
    colors=np.clip(.5+.28209479177387814*state['sh_coeffs'][indices,0].numpy(),0,1)
    with (output/'centers.ply').open('w') as file:
        file.write(f'ply\nformat ascii 1.0\ncomment Gaussian centers; colors are DC preview, not a mesh\nelement vertex {len(indices)}\nproperty float x\nproperty float y\nproperty float z\nproperty uchar red\nproperty uchar green\nproperty uchar blue\nend_header\n')
        np.savetxt(file,np.column_stack([selected,np.rint(colors*255)]),fmt=['%.7g','%.7g','%.7g','%d','%d','%d'])
    (output/'README.txt').write_text('Independent prototype sink selection. Original RGB checkpoint is unchanged.\nselection.npz contains original Gaussian indices and votes. gaussians.npz contains world-frame Gaussian parameters; quaternions are wxyz. centers.ply contains centers and DC colors, not a mesh.\nCoordinates use arbitrary SfM reconstruction scale, not meters. Bounds contain selected Gaussian centers. This is a visible surface selection; hidden surfaces may be omitted.\n')
    with zipfile.ZipFile(output/'selection.zip','w',zipfile.ZIP_DEFLATED) as archive:
        for name in ('selection.json','selection.npz','gaussians.npz','centers.ply','README.txt'):archive.write(output/name,name)
    write_json(REPORT/'build.json',metadata)
    print(json.dumps({key:metadata[key] for key in ('id','selected_gaussians','candidates','center','bounds','component_sizes')},indent=2))


if __name__=='__main__':main()
