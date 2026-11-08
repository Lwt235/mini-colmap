"""Fresh frozen RGB expected depth: source GT knife FoV support, not target GT."""
import json
from pathlib import Path
from types import SimpleNamespace
import numpy as np
import torch
import langsplat_raw3 as raw
from langsplat_reference_metrics import annotations
from minicolmap.semantics.project import project,write_json,sha256
from minicolmap.semantics.encoders import gpu_session
from minicolmap.semantics.diagnostics import projected_support


def main():
    report=raw.ROOT/'reports/semantic-stage-diagnostics-20261022'
    config=raw.configure();prepared=project(config);frames={f['image_id']:f for f in prepared['frames']}
    ann=next(a for a in prepared['annotations'] if a['image_id']==62)
    truth=annotations(ann['annotation'])['knife']['mask'];frame=frames[62]
    with gpu_session({**config,'workspace':str(report)}),torch.inference_mode():
        _,GaussianModel=raw.ref.native_imports();raw.install_renderer()
        from gaussian_renderer import render
        params,_=torch.load(raw.ref.rgb_checkpoint(config,prepared),map_location='cuda',weights_only=True)
        model=GaussianModel(3);model.restore(params,SimpleNamespace(include_feature=True),mode='eval')
        model._language_feature=torch.ones((len(model.get_xyz),3),device='cuda')
        z=(model.get_xyz@torch.tensor(frame['R'],device='cuda').T+torch.tensor(frame['t'],device='cuda'))[:,2]
        result=render(raw.ref.native_camera(frame),model,SimpleNamespace(debug=False,convert_SHs_python=False,compute_cov3D_python=False),
            torch.zeros(3,device='cuda'),SimpleNamespace(include_feature=True),override_color=z[:,None].expand(-1,3).contiguous())
        alpha=result['language_feature_image'][0].cpu().numpy();depth=result['render'][0].cpu().numpy()/np.maximum(alpha,1e-8)
    rows=[dict(anchor_id=62,image_id=i,**projected_support(frame,frames[i],depth,alpha,truth)) for i in (63,94)]
    write_json(report/'visibility.json',rows)
    write_json(report/'visibility-source.json',dict(rgb_sha256=sha256(Path(config['rgb_run'])/'checkpoint.pt'),
        depth='Fresh native frozen RGB expected depth',test_labels_used=False,source_gt='Dev camera 62 knife only'))
    print(json.dumps(rows,indent=2))


if __name__=='__main__':main()
