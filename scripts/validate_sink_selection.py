"""Actual selected-Gaussian rendering on five unused views, plus undo/perf checks."""
import json,time
from pathlib import Path
import cv2
import numpy as np
import torch
import langsplat_raw3 as raw
from langsplat_reference_metrics import query_mask
from minicolmap.rendering.live import CudaRenderer,normalize_camera
from minicolmap.rendering.gaussian_backend import rasterize_gsplat
from minicolmap.semantics.encoders import gpu_session
from minicolmap.semantics.project import project,write_json,sha256
from minicolmap.semantics.selection import load_selection,SELECTION_ID

ROOT=raw.ROOT;REPORT=ROOT/'reports/sink-selection-20261021'


def main():
    config=raw.configure();prepared=project(config);metadata,indices=load_selection(ROOT,SELECTION_ID)
    frames={f['image_id']:f for f in prepared['frames']}
    center=np.array(metadata['center']);anchor=frames[124];R0=np.array(anchor['R']);C0=-R0.T@np.array(anchor['t'])
    ranking=[]
    for i in prepared['splits']['train']:
        if i in metadata['view_ids']:continue
        f=frames[i];R=np.array(f['R']);t=np.array(f['t']);p=R@center+t
        if p[2]<=.01:continue
        uv=np.array(f['K'])@p;uv=uv[:2]/uv[2]
        if not (f['width']*.15<uv[0]<f['width']*.85 and f['height']*.15<uv[1]<f['height']*.85):continue
        distance=np.linalg.norm(-R.T@t-C0)/prepared['normalization']['scale']
        angle=np.arccos(np.clip(R[2]@R0[2],-1,1))
        ranking.append((distance+.2*angle,i))
    ordered=[i for _,i in sorted(ranking)];validation_ids=[ordered[i] for i in (0,1,3,5,8)]
    write_json(REPORT/'validation-policy.json',dict(selection_id=SELECTION_ID,validation_ids=validation_ids,
        excluded_fit_ids=metadata['view_ids'],test_labels_used=False,selection='Pose proximity and selected-center field of view only',
        contribution_threshold=.2,comparison='Independent-view 2D semantic query agreement, not human 3D annotation'))
    with gpu_session({**config,'workspace':str(REPORT)}):
        raw.ref.seed();renderer=CudaRenderer(config['rgb_run'])
        raw.ref.native_imports();raw.install_renderer()
        from gaussian_renderer import render
        from types import SimpleNamespace
        models=raw.ref.native_models(prepared,30000)
        codec=raw.ref.upstream_ae();codec.load_state_dict(torch.load(raw.WORK/'autoencoder/best_ckpt.pth',weights_only=True));codec.eval()
        clip=raw.ref.reference_clip(config);clip.set_positives(['sink'])
        pipe=SimpleNamespace(debug=False,convert_SHs_python=False,compute_cov3D_python=False);opt=SimpleNamespace(include_feature=True)
        indicator=torch.zeros((len(renderer.state['means']),3),device='cuda');indicator[torch.as_tensor(indices,device='cuda')]=1
        rows=[];tiles=[]
        with torch.inference_mode():
            for n,i in enumerate([124,*validation_ids]):
                frame=frames[i];camera={key:frame[key] for key in ('width','height','K','R','t')}
                original=renderer.render_rgb(camera)
                highlighted=renderer.render_rgb(camera,selection={'id':SELECTION_ID,'mode':'highlight'})
                isolated=renderer.render_rgb(camera,selection={'id':SELECTION_ID,'mode':'isolate'})
                restored=renderer.render_rgb(camera)
                torch.testing.assert_close(restored,original,rtol=0,atol=0)
                normalized=normalize_camera(camera,renderer.normalization)
                K,R,t=[torch.tensor(normalized[key],device='cuda',dtype=torch.float32) for key in ('K','R','t')]
                influence=rasterize_gsplat(renderer.state['means'],renderer.covariance,indicator,renderer.opacities,K,R,t,
                    frame['width'],frame['height'],torch.zeros(3,device='cuda'))[...,0].cpu().numpy()
                mask=influence>.2
                maps=[]
                for model in models:
                    latent=render(raw.ref.native_camera(frame),model,pipe,torch.ones(3,device='cuda'),opt)['language_feature_image']
                    latent=latent.permute(1,2,0).reshape(-1,3)
                    values=[clip.get_relevancy(codec.decode(chunk),0)[:,0].cpu() for chunk in latent.split(8192)]
                    maps.append(torch.cat(values).reshape(frame['height'],frame['width']).numpy())
                level,reference=query_mask(np.stack(maps));reference=reference>0
                intersection=int((mask&reference).sum());union=int((mask|reference).sum())
                row=dict(image_id=i,independent=i!=124,selected_pixels=int(mask.sum()),query_pixels=int(reference.sum()),
                    selected_query_precision=intersection/max(1,int(mask.sum())),query_coverage=intersection/max(1,int(reference.sum())),
                    query_iou=intersection/max(1,union),query_level=level+1,restore_bit_exact=True)
                rows.append(row)
                for name,image in [('original',original),('highlight',highlighted),('isolate',isolated)]:
                    array=cv2.cvtColor((image.clamp(0,1).cpu().numpy()*255).round().astype(np.uint8),cv2.COLOR_RGB2BGR)
                    cv2.imwrite(str(REPORT/f'{i:06d}-{name}.jpg'),array)
                cv2.imwrite(str(REPORT/f'{i:06d}-influence.png'),(np.clip(influence,0,1)*255).astype(np.uint8))
                tile=cv2.imread(str(REPORT/f'{i:06d}-highlight.jpg'));tile=cv2.resize(tile,(394,290))
                caption=np.zeros((50,394,3),np.uint8)
                cv2.putText(caption,f"camera {i} | {'anchor' if i==124 else 'unused view'}",(8,20),cv2.FONT_HERSHEY_SIMPLEX,.48,(220,235,225),1)
                cv2.putText(caption,f"precision {row['selected_query_precision']:.2f} / coverage {row['query_coverage']:.2f}",(8,42),cv2.FONT_HERSHEY_SIMPLEX,.45,(170,210,185),1)
                tiles.append(np.concatenate([tile,caption]))
                write_json(REPORT/'progress.json',dict(stage='validation',completed=n+1,total=6,image_id=i))
            perf={};f=frames[124];camera={key:f[key] for key in ('width','height','K','R','t')}
            for mode in ('none','highlight','isolate'):
                values=[]
                for n in range(18):
                    result,_=renderer.render(camera,selection=None if mode=='none' else {'id':SELECTION_ID,'mode':mode})
                    if n>=3:values.append(result['render_ms'])
                perf[mode]=dict(mean_gpu_ms=float(np.mean(values)),p95_gpu_ms=float(np.percentile(values,95)))
        torch.cuda.synchronize()
    montage=np.concatenate([np.concatenate(tiles[:3],axis=1),np.concatenate(tiles[3:],axis=1)],axis=0)
    cv2.imwrite(str(REPORT/'validation-views.jpg'),montage)
    actual=sha256(Path(config['rgb_run'])/'checkpoint.pt')
    if actual!=prepared['rgb_checkpoint_sha256']:raise ValueError('Original RGB model changed')
    write_json(REPORT/'validation.json',dict(status='completed',selection_id=SELECTION_ID,selected_gaussians=len(indices),
        independent_views=validation_ids,per_view=rows,performance=perf,original_rgb_preserved=True,restore_bit_exact=True,
        scope='Occlusion-aware Gaussian contribution vs independent 2D query; not annotated 3D accuracy'))
    print(json.dumps(dict(per_view=rows,performance=perf),indent=2))


if __name__=='__main__':main()
