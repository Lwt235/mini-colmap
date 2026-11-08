"""Actual native 3D selection previews and bit-exact restoration, GPU 7 only."""
import json
from pathlib import Path
import cv2,numpy as np,torch
from minicolmap.rendering.live import CudaRenderer
from minicolmap.semantics.project import load_config,project,write_json
from minicolmap.semantics.encoders import gpu_session
from minicolmap.semantics.selection import load_selection
ROOT=Path(__file__).resolve().parents[1]


def main():
    config=load_config(ROOT/'configs/semantic_waldo.json');p=project(config);frames={f['image_id']:f for f in p['frames']}
    cases=json.loads((ROOT/'configs/showcase.json').read_text())['selections'];rows=[]
    with gpu_session({**config,'workspace':str(ROOT/'reports/portfolio-release-20261022')}),torch.inference_mode():
        renderer=CudaRenderer(config['rgb_run'])
        for case in cases:
            meta,_=load_selection(ROOT,case['selection_id']);f=frames[meta['anchor_image_id']]
            camera={k:f[k] for k in ('width','height','K','R','t')};original=renderer.render_rgb(camera).clone()
            timings={}
            for mode in ('highlight','isolate'):
                image=renderer.render_rgb(camera,selection={'id':case['selection_id'],'mode':mode})
                rgb=cv2.cvtColor((image.clamp(0,1).cpu().numpy()*255).round().astype(np.uint8),cv2.COLOR_RGB2BGR)
                path=ROOT/'docs/assets'/case['preview_file'] if mode=='highlight' else ROOT/'reports/portfolio-release-20261022'/f'{case["id"]}-isolate.jpg'
                cv2.imwrite(str(path),rgb)
                values=[]
                for n in range(13):
                    metrics,_=renderer.render(camera,selection={'id':case['selection_id'],'mode':mode})
                    if n>=3:values.append(metrics['render_ms'])
                timings[mode]=float(np.mean(values))
                torch.testing.assert_close(renderer.render_rgb(camera),original,rtol=0,atol=0)
            assert len(renderer.selection_cache)==1
            rows.append(dict(id=case['id'],selection_id=case['selection_id'],restore_bit_exact=True,mean_gpu_ms=timings,
                allocated_mb=torch.cuda.memory_allocated()/1024**2,cached_selections=1,width=f['width'],height=f['height']))
    write_json(ROOT/'reports/portfolio-release-20261022/native-render-check.json',dict(status='passed',per_case=rows,gpu=7))
    print(json.dumps(rows,indent=2))


if __name__=='__main__':main()
