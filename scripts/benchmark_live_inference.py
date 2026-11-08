"""Numerical parity and timing of inference-only SH fusion; GPU 7 only."""
import json,time
from pathlib import Path
import numpy as np
import torch
from minicolmap.rendering.live import CudaRenderer
from minicolmap.rendering.data import scaled_intrinsics
from minicolmap.semantics.encoders import gpu_session
from minicolmap.semantics.project import load_config,project,write_json

ROOT=Path(__file__).resolve().parents[1]
REPORT=ROOT/'reports/live-optimization-20261021'


def main():
    config=load_config(ROOT/'configs/semantic_waldo.json')
    prepared=project(config)
    with gpu_session({**config,'workspace':str(REPORT)}):
        renderer=CudaRenderer(config['rgb_run'])
        cameras=[]
        for i in (62,99,124):
            f=next(f for f in prepared['frames'] if f['image_id']==i)
            w,h=1280,942
            cameras.append(dict(width=w,height=h,K=scaled_intrinsics(np.array(f['K']),(f['width'],f['height']),(w,h)).tolist(),R=f['R'],t=f['t']))
        parity=[]
        with torch.inference_mode():
            for i,camera in zip((62,99,124),cameras):
                expected=renderer.render_rgb(camera,fused=False)
                actual=renderer.render_rgb(camera)
                error=(actual-expected).abs()
                mse=float((actual-expected).square().mean())
                row=dict(image_id=i,max_abs=float(error.max()),mean_abs=float(error.mean()),psnr_db=float(-10*np.log10(max(mse,1e-30))))
                if row['psnr_db']<60 or row['max_abs']>.005:
                    raise ValueError(f'Inference color parity failed: {row}')
                parity.append(row)
            timings={}
            for fused in (False,True):
                values=[]
                for n in range(27):
                    torch.cuda.synchronize();start=time.perf_counter()
                    renderer.render_rgb(cameras[n%3],fused=fused)
                    torch.cuda.synchronize()
                    if n>=6:values.append(1000*(time.perf_counter()-start))
                timings['fused' if fused else 'learner']=dict(mean_ms=float(np.mean(values)),p95_ms=float(np.percentile(values,95)))
            transport={}
            for quality,scale in [('final',1),('interactive',.6)]:
                f=next(f for f in prepared['frames'] if f['image_id']==124)
                w,h=round(1280*scale),round(720*scale)
                # Match the UI vertical field of view, including horizontal crop.
                ratio=h/f['height'];K=np.array(f['K'])*ratio;K[2]=[0,0,1];K[0,2]=(w-1)/2;K[1,2]=(h-1)/2
                camera=dict(width=w,height=h,K=K.tolist(),R=f['R'],t=f['t'])
                rows=[renderer.render(camera,quality=quality)[0] for _ in range(12)][3:]
                transport[quality]={key:float(np.mean([row[key] for row in rows])) for key in ('render_ms','readback_ms','encode_ms','bytes')}
                transport[quality].update(width=w,height=h)
    write_json(REPORT/'native-inference.json',dict(status='passed',gpu=7,parity=parity,timings=timings,transport=transport,
        scope='Same real cameras, covariance/opacity/SH/model; no pruning; numerical RGB control before JPEG; local timing, not Windows tunnel FPS'))
    print(json.dumps(dict(parity=parity,timings=timings,transport=transport),indent=2))


if __name__=='__main__':main()
