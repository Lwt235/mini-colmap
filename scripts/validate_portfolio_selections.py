"""Independent Dev polygon check of fixed selected Gaussians; no selection tuning."""
import json
from pathlib import Path
import cv2,numpy as np,torch
from minicolmap.semantics.project import load_config,project,write_json,sha256
from minicolmap.semantics.encoders import gpu_session
from minicolmap.semantics.diagnostics import mask_metrics
from minicolmap.rendering.live import CudaRenderer,normalize_camera
from minicolmap.rendering.gaussian_backend import rasterize_gsplat
from langsplat_reference_metrics import annotations
ROOT=Path(__file__).resolve().parents[1];REPORT=ROOT/'reports/portfolio-release-20261022'


def main():
    config=load_config(ROOT/'configs/semantic_waldo.json');p=project(config)
    frames={f['image_id']:f for f in p['frames']};records={a['image_id']:a for a in p['annotations']}
    campaign=json.loads((REPORT/'geometry-visible-campaign.json').read_text())
    cases=[(r['text'],r['top1_diagnostic']['selection_id'],62 if r['text']=='knife' else 124) for r in campaign]
    experiments=json.loads((REPORT/'visible-lift-experiment.json').read_text())
    cases +=[(r['text'],r['selection_id'],124) for r in experiments if r['text']=='dark cup']
    rows=[]
    with gpu_session({**config,'workspace':str(REPORT)}),torch.inference_mode():
        renderer=CudaRenderer(config['rgb_run'])
        for text,sid,i in cases:
            folder=ROOT/'data/object_selections'/sid
            with np.load(folder/'candidate.npz',allow_pickle=False) as data:indices=data['indices']
            f=frames[i];camera={k:f[k] for k in ('width','height','K','R','t')}
            n=normalize_camera(camera,renderer.normalization);K,R,t=[torch.tensor(n[k],device='cuda',dtype=torch.float32) for k in ('K','R','t')]
            colors=torch.zeros((len(renderer.state['means']),3),device='cuda');colors[torch.as_tensor(indices,device='cuda')]=1
            alpha=rasterize_gsplat(renderer.state['means'],renderer.covariance,colors,renderer.opacities,K,R,t,f['width'],f['height'],torch.zeros(3,device='cuda'))[...,0].cpu().numpy()
            mask=alpha>.2;truth=annotations(records[i]['annotation'])[text]['mask']
            row=dict(query=text,selection_id=sid,image_id=i,**mask_metrics(mask,truth));rows.append(row)
            image=(renderer.render_rgb(camera).clamp(0,1).cpu().numpy()*255).round().astype(np.uint8)
            image=cv2.cvtColor(image,cv2.COLOR_RGB2BGR);image[mask]=(.35*image[mask]+.65*np.array([85,220,135])).astype(np.uint8)
            contours,_=cv2.findContours(truth,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE);cv2.drawContours(image,contours,-1,(50,90,255),2)
            cv2.imwrite(str(REPORT/f'{text.replace(" ","_")}-{sid}-dev.jpg'),image)
    write_json(REPORT/'dev-selection-check.json',dict(per_query=rows,threshold=.2,
        scope='Original Dev polygon grid; used for development inspection, not blind generalization. Selection fit did not read manual GT.'))
    print(json.dumps(rows,indent=2))


if __name__=='__main__':main()
