"""Lift cached multi-view masks through the frozen native compositor.

Only an independent scalar membership logit per Gaussian is optimized. RGB,
opacity, geometry, existing semantic fields, and learner source remain frozen.
"""
import json,time
from pathlib import Path
from types import SimpleNamespace
import cv2
import numpy as np
import torch
import langsplat_raw3 as raw
from langsplat_reference_metrics import query_mask
from minicolmap.semantics.encoders import gpu_session
from minicolmap.semantics.project import project,write_json,sha256
from minicolmap.semantics.selection import largest_spatial_component,selection_directory,SELECTION_ID
from selection_exports import save_selection

ROOT=raw.ROOT;REPORT=ROOT/'reports/sink-selection-20261021'


def main():
    config=raw.configure();prepared=project(config);output=selection_directory(ROOT,SELECTION_ID)
    if (output/'selection.json').exists():raise ValueError('Final selection exists; use a new explicit version')
    output.mkdir(parents=True,exist_ok=True)
    source=ROOT/'reports/semantic-multiview-20261021'
    manifest=json.loads((source/'manifest.json').read_text());ids=manifest['views']['124']
    frames={f['image_id']:f for f in prepared['frames']}
    checkpoint=torch.load(Path(config['rgb_run'])/'checkpoint.pt',map_location='cpu',weights_only=True)
    state=checkpoint['state'];norm=checkpoint['normalization'];scale=norm['scale']
    with gpu_session({**config,'workspace':str(REPORT)}):
        raw.ref.seed();_,GaussianModel=raw.ref.native_imports();raw.install_renderer()
        from gaussian_renderer import render
        model=GaussianModel(3)
        params,_=torch.load(raw.ref.rgb_checkpoint(config,prepared),map_location='cuda',weights_only=True)
        model.restore(params,SimpleNamespace(include_feature=True),mode='eval')
        for name in ('_xyz','_features_dc','_features_rest','_scaling','_rotation','_opacity'):getattr(model,name).requires_grad_(False)
        logits=torch.full((len(model.get_xyz),1),-2.,device='cuda',requires_grad=True)
        optimizer=torch.optim.Adam([logits],lr=.03)
        cameras=[];targets=[];alphas=[]
        for i in ids:
            frame=frames[i]
            with np.load(source/f'{i:06d}.npz',allow_pickle=False) as data:
                _,mask=query_mask(data['maps'][:,0]);alpha=data['alpha'].copy()
            mask=cv2.erode(mask,np.ones((5,5),np.uint8))
            cameras.append(raw.ref.native_camera(frame));targets.append(torch.as_tensor(mask,device='cuda',dtype=torch.float32));alphas.append(torch.as_tensor(alpha,device='cuda'))
        pipe=SimpleNamespace(debug=False,convert_SHs_python=False,compute_cov3D_python=False);opt=SimpleNamespace(include_feature=True)
        history=[];started=time.monotonic()
        for step in range(1200):
            view=step%len(ids)
            model._language_feature=logits.sigmoid().expand(-1,3).contiguous()
            prediction=render(cameras[view],model,pipe,torch.zeros(3,device='cuda'),opt)['language_feature_image'][0]
            alpha=alphas[view];valid=alpha>.95
            prediction=(prediction/alpha.clamp_min(1e-6)).clamp(1e-5,1-1e-5)
            target=targets[view];fg=valid&(target>.5);bg=valid&~fg
            loss=.5*(-prediction[fg].log().mean()-(1-prediction[bg]).log().mean())
            if not torch.isfinite(loss):raise ValueError('Non-finite independent selection loss')
            optimizer.zero_grad(set_to_none=True);loss.backward();optimizer.step()
            if step%100==0 or step==1199:
                row=dict(step=step+1,loss=float(loss.detach()),seconds=time.monotonic()-started);history.append(row)
                write_json(REPORT/'fit-progress.json',row)
        probabilities=logits.detach().sigmoid().cpu().numpy().ravel()
        torch.save(logits.detach().cpu(),output/'membership_logits.pt')
        for expected,actual in zip(params[1:7],(model._xyz,model._features_dc,model._features_rest,model._scaling,model._rotation,model._opacity)):
            torch.testing.assert_close(expected,actual,atol=0,rtol=0)
    points=state['means'].numpy()*scale+np.array(norm['center'])
    anchor=frames[124]
    with np.load(source/'000124.npz',allow_pickle=False) as data:
        _,anchor_mask=query_mask(data['maps'][:,0]);depth=data['depth'];alpha=data['alpha']
    y,x=np.where((anchor_mask>0)&(alpha>.95))
    rays=np.stack([x,y,np.ones(len(x))],axis=1)@np.linalg.inv(anchor['K']).T
    surface=(rays*depth[y,x,None]-np.asarray(anchor['t']))@np.asarray(anchor['R'])
    lower,upper=np.quantile(surface,[.02,.98],axis=0);padding=.1*(upper-lower)+.005*scale
    roi=(points>=lower-padding).all(axis=1)&(points<=upper+padding).all(axis=1)
    candidates=np.flatnonzero((probabilities>=.6)&(state['opacity_logits'].sigmoid().numpy()>=.005)&roi)
    # RGB surface centers can have small gaps across occluders. Use a coarse
    # support graph; keep only original indices, never synthesize/fill geometry.
    connected,components=largest_spatial_component(points[candidates],.025*scale)
    indices=candidates[connected].astype(np.int64)
    if len(indices)<100:raise ValueError('Insufficient coherent object support')
    selected=points[indices];center=np.average(selected,axis=0,weights=probabilities[indices])
    metadata=dict(schema_version=1,id=SELECTION_ID,name='水槽',text='sink',model='raw3',anchor_image_id=124,status='prototype',
        rgb_checkpoint_sha256=prepared['rgb_checkpoint_sha256'],source_gaussians=len(points),selected_gaussians=len(indices),
        center=center.tolist(),bounds={'min':selected.min(0).tolist(),'max':selected.max(0).tolist()},
        coordinate_system='original_sfm_world',units='reconstruction units; metric scale not calibrated',view_ids=ids,
        method='Independent membership fit through frozen Gaussian compositor, followed by threshold and largest spatial support component',
        parameters=dict(steps=1200,seed=0,learning_rate=.03,threshold=.6,min_opacity=.005,voxel_size_scale=.025,
                        anchor_depth_roi=dict(lower=(lower-padding).tolist(),upper=(upper+padding).tolist(),surface_quantiles=[.02,.98],padding_fraction=.1),
                        loss='equal-weight foreground/background BCE on opacity-normalized membership; alpha>.95; mask eroded5'),
        candidates=len(candidates),component_sizes=components,original_geometry_frozen=True,
        scope='Prototype visible surface selection; may include occluding clutter, omit hidden surfaces; not a watertight mesh or human 3D annotation')
    save_selection(output,state,norm,indices,metadata,membership=probabilities[indices])
    write_json(REPORT/'build.json',metadata);write_json(REPORT/'fit-history.json',history)
    print(json.dumps(dict(id=SELECTION_ID,candidates=len(candidates),selected=len(indices),bounds=metadata['bounds'],loss=history[-1]),indent=2))


if __name__=='__main__':main()
