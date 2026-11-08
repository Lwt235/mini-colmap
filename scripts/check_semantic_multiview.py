"""Geometry-only view selection and occlusion-aware language consistency audit.

Uses frozen RGB expected depth, not ground-truth depth. No labels, model updates,
threshold search, test-set selection, or 3D object identity claims.
"""
import json
from pathlib import Path
from types import SimpleNamespace
import cv2
import numpy as np
import torch
import langsplat_raw3 as raw
from langsplat_reference_metrics import query_mask
from minicolmap.semantics.encoders import gpu_session
from minicolmap.semantics.project import project,write_json

REPORT=raw.ROOT/'reports/semantic-multiview-20261021'
QUERIES=[('sink',124),('dark cup',124),('knife',62)]


def select_views(prepared,anchor,count=6):
    frames={f['image_id']:f for f in prepared['frames']}
    origin=frames[anchor]
    R=np.asarray(origin['R']);center=-R.T@np.asarray(origin['t'])
    ranked=[]
    for i in dict.fromkeys([anchor,*prepared['splits']['train']]):
        frame=frames[i];rotation=np.asarray(frame['R'])
        angle=float(np.arccos(np.clip(R[2]@rotation[2],-1,1)))
        if angle>np.deg2rad(40):continue
        distance=np.linalg.norm(-rotation.T@np.asarray(frame['t'])-center)/prepared['normalization']['scale']
        ranked.append((distance+.2*angle,i))
    ordered=[i for _,i in sorted(ranked)]
    indices=[0,1,2,4,7,11][:count]
    return [ordered[min(n,len(ordered)-1)] for n in indices if ordered]


def reproject(anchor,other,source_mask,target_mask,scene_scale):
    """Compare corresponding visible rays, not masks on unrelated image grids."""
    frame,depth,alpha=anchor
    target,target_depth,target_alpha=other
    y,x=np.mgrid[0:depth.shape[0]:2,0:depth.shape[1]:2]
    y,x=y.ravel(),x.ravel()
    valid=(alpha[y,x]>.95)&np.isfinite(depth[y,x])&(depth[y,x]>.01)
    y,x=y[valid],x[valid]
    camera=(np.stack([x,y,np.ones_like(x)],axis=-1)@np.linalg.inv(frame['K']).T)*depth[y,x,None]
    world=(camera-np.asarray(frame['t']))@np.asarray(frame['R'])
    projected=world@np.asarray(target['R']).T+np.asarray(target['t'])
    uv=projected@np.asarray(target['K']).T
    positive=projected[:,2]>.01
    xy=np.rint(uv[:,:2]/np.maximum(uv[:,2:],1e-9)).astype(np.int64)
    tx,ty=xy.T
    valid=positive&(tx>=0)&(tx<target['width'])&(ty>=0)&(ty<target['height'])
    x,y,tx,ty,projected=x[valid],y[valid],tx[valid],ty[valid],projected[valid]
    visible=(target_alpha[ty,tx]>.95)&(np.abs(projected[:,2]-target_depth[ty,tx])<.05*np.abs(target_depth[ty,tx])+.002*scene_scale)
    a,b=source_mask[y[visible],x[visible]]>0,target_mask[ty[visible],tx[visible]]>0
    support=int(a.sum());union=int((a|b).sum());intersection=int((a&b).sum())
    return dict(corresponding_rays=len(a),anchor_foreground_rays=support,
        reprojection_iou=float(intersection/union) if support>=25 and union else None,
        foreground_agreement=float(intersection/support) if support>=25 else None,
        scope='Approximate frozen-RGB expected depth, opacity > .95, depth consistency; not annotated accuracy')


def main():
    config=raw.configure();prepared=project(config)
    REPORT.mkdir(parents=True,exist_ok=True)
    groups={anchor:select_views(prepared,anchor) for _,anchor in QUERIES}
    frames={f['image_id']:f for f in prepared['frames']}
    ids=sorted({i for values in groups.values() for i in values})
    write_json(REPORT/'manifest.json',dict(queries=QUERIES,views=groups,gpu=7,view_selection='Camera pose only; anchor plus train views at ranked positions 0,1,2,4,7,11 within 40 degrees',
        model='raw3',threshold=.4,trained=False,test_labels_used=False,
        limitations='Geometric self-consistency can be high for a consistently wrong object. Expected depth can blend surfaces. Occluded/disoccluded objects need manual review.'))
    with gpu_session({**config,'workspace':str(REPORT)}):
        raw.ref.seed();raw.ref.native_imports();raw.install_renderer()
        from gaussian_renderer import render
        models=raw.ref.native_models(prepared,30000)
        codec=raw.ref.upstream_ae();codec.load_state_dict(torch.load(raw.WORK/'autoencoder/best_ckpt.pth',weights_only=True));codec.eval()
        clip=raw.ref.reference_clip(config);clip.set_positives([q for q,_ in QUERIES])
        pipe=SimpleNamespace(debug=False,convert_SHs_python=False,compute_cov3D_python=False)
        opt=SimpleNamespace(include_feature=True)
        views={}
        with torch.inference_mode():
            for n,i in enumerate(ids):
                frame=frames[i];camera=raw.ref.native_camera(frame);maps=[]
                for model in models:
                    latent=render(camera,model,pipe,torch.ones(3,device='cuda'),opt)['language_feature_image']
                    values=latent.permute(1,2,0).reshape(-1,3)
                    scores=[]
                    for chunk in values.split(8192):
                        features=codec.decode(chunk)
                        scores.append(torch.stack([clip.get_relevancy(features,q)[:,0] for q in range(len(QUERIES))]).cpu())
                    maps.append(torch.cat(scores,dim=1).reshape(len(QUERIES),frame['height'],frame['width']).numpy())
                maps=np.stack(maps)
                model=models[0];saved=model._language_feature
                model._language_feature=torch.ones_like(saved)
                z=(model.get_xyz@torch.tensor(frame['R'],device='cuda').T+torch.tensor(frame['t'],device='cuda'))[:,2]
                depth_output=render(camera,model,pipe,torch.zeros(3,device='cuda'),opt,override_color=z[:,None].expand(-1,3).contiguous())
                model._language_feature=saved
                alpha=depth_output['language_feature_image'][0].cpu().numpy()
                depth=depth_output['render'][0].cpu().numpy()/np.maximum(alpha,1e-8)
                views[i]=(frame,depth,alpha,maps)
                np.savez_compressed(REPORT/f'{i:06d}.npz',maps=maps,depth=depth,alpha=alpha)
                write_json(REPORT/'progress.json',dict(status='rendering',completed=n+1,total=len(ids),image_id=i))
    rows=[]
    for q,(text,anchor_id) in enumerate(QUERIES):
        anchor=views[anchor_id]
        level,anchor_mask=query_mask(anchor[3][:,q])
        tiles=[]
        for i in groups[anchor_id]:
            view=views[i];chosen,mask=query_mask(view[3][:,q])
            _,fixed=query_mask(view[3][level:level+1,q])
            row=dict(query=text,anchor_id=anchor_id,image_id=i,level=chosen+1,anchor_level=level+1,
                selected_pixels=int(mask.sum()),adaptive=reproject(anchor[:3],view[:3],anchor_mask,mask,prepared['normalization']['scale']),
                fixed_level=reproject(anchor[:3],view[:3],anchor_mask,fixed,prepared['normalization']['scale']))
            rows.append(row)
            rgb=cv2.imread(view[0]['path'])
            rgb[mask>0]=(.45*rgb[mask>0]+.55*np.array([80,210,115])).astype(np.uint8)
            tile=cv2.resize(rgb,(394,290));caption=np.zeros((52,394,3),np.uint8)
            cv2.putText(caption,f"{text} | camera {i} | L{chosen+1}",(8,20),cv2.FONT_HERSHEY_SIMPLEX,.48,(230,235,230),1)
            score=row['adaptive']['reprojection_iou'];label='anchor' if i==anchor_id else f"reproject IoU: {score:.3f}" if score is not None else 'insufficient visible foreground'
            cv2.putText(caption,label,(8,42),cv2.FONT_HERSHEY_SIMPLEX,.45,(170,200,180),1)
            tiles.append(np.concatenate([tile,caption]))
        contact=np.concatenate([np.concatenate(tiles[:3],axis=1),np.concatenate(tiles[3:6],axis=1)],axis=0)
        cv2.imwrite(str(REPORT/(text.replace(' ','_')+'-views.jpg')),contact)
    write_json(REPORT/'results.json',dict(status='completed',per_view=rows,views=len(ids),objects=len(QUERIES)))
    lines=['# 跨视角语义一致性检查','','GPU 7；固定 raw3 三维模型，无重训、无阈值搜索、未使用 Test 标注。',
        '相机只按位姿选取；每个目标检查锚点与五个邻近／较远视角。','',
        '| 目标 | 相机 | 层级 | 可见前景射线 | 动态层级重投影 IoU | 固定层级重投影 IoU |','|---|---:|---:|---:|---:|---:|']
    for row in rows:
        def fmt(value):return '样本不足' if value is None else f'{value:.3f}'
        lines.append(f"| {row['query']} | {row['image_id']} | {row['level']} | {row['adaptive']['anchor_foreground_rays']} | {fmt(row['adaptive']['reprojection_iou'])} | {fmt(row['fixed_level']['reprojection_iou'])} |")
    lines+=['','## 解读边界','','重投影使用 RGB 高斯的期望深度与透明度筛选，不是真实深度。遮挡、透明物体和多层表面会影响统计。',
        '几何一致不代表语义正确：持续选错同一个物体也能取得高一致性。除锚点已有标注外，其余视图没有人工目标标注。',
        '固定层级只是诊断对照，不据此修改当前默认查询策略。请结合三个多视角拼图，检查选区是否漂移、消失或扩散。']
    (REPORT/'README.md').write_text('\n'.join(lines)+'\n')
    write_json(REPORT/'progress.json',dict(status='completed',views=len(ids),objects=len(QUERIES)))
    print(json.dumps(dict(status='completed',views=len(ids),objects=len(QUERIES))))


if __name__=='__main__':main()
