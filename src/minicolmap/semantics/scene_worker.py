"""Generic, unlabelled scene indexing and text retrieval. Runs on the explicitly selected physical GPU."""
import io
import json
import os
from pathlib import Path
import sys
import traceback
import zipfile

from .project import load_config, prepare, project, sha256, write_json


def train(config, update):
    import torch
    from .encoders import extract
    from . import pipeline
    workspace = Path(config['workspace'])
    if not (workspace / 'project.json').exists():
        checkpoint = torch.load(Path(config['rgb_run'])/'checkpoint.pt', map_location='cpu', weights_only=True)
        from ..io import load_reconstruction
        model = load_reconstruction(Path(config['rgb_run'])/'source_model.json')
        ids = sorted(set(model.poses) - set(checkpoint['validation_ids']))
        if len(ids) < 2: raise ValueError('至少需要两个 RGB 训练视角才能生成语义索引')
        if config['smoke_image_ids'] != ids[:1]:
            config['smoke_image_ids'] = ids[:1]
            write_json(Path(config['_root'])/'configs'/f"semantic_{config['name']}.json", {k:v for k,v in config.items() if not k.startswith('_')})
        update('prepare', '正在绑定照片与三维模型')
        prepare(config)
    prepared = project(config)
    frames = pipeline.choose_frames(prepared, 'train')
    update('extract', f'正在提取 {len(frames)} 张照片的物体特征')
    extract(config, frames)
    update('regions', '正在整理物体区域')
    pipeline.regions(config, frames)
    if not (workspace/'autoencoder/checkpoint.pt').exists():
        update('autoencoder', '正在压缩场景特征')
        pipeline.fit_autoencoder(config, prepared)
    update('encode', '正在编码物体区域')
    pipeline.encode(config, prepared, frames)
    if not (workspace/'distilled/checkpoint.pt').exists():
        update('distill', '正在将语义特征学习到三维模型')
        pipeline.distill(config, prepared)
    write_json(workspace/'index-source.json', source_identity(config, prepared))
    return dict(training_views=len(frames), gaussian_count=prepared['gaussian_count'])


def source_identity(config, prepared):
    w = Path(config['workspace'])
    return dict(rgb=prepared['rgb_checkpoint_sha256'], dataset=prepared['dataset_sha256'],
                model_manifest=sha256(config['model_manifest']),
                source_model=prepared['source_model_sha256'],
                codec=sha256(w/'autoencoder/checkpoint.pt'), field=sha256(w/'distilled/checkpoint.pt'))


def query(config, scene_id, text, update):
    import numpy as np
    import torch
    from . import pipeline, algorithms
    from .encoders import ClipEncoder
    from .selection import largest_spatial_component
    prepared = project(config)
    workspace = Path(config['workspace'])
    identity = source_identity(config, prepared)
    if json.loads((workspace/'index-source.json').read_text()) != identity:
        raise ValueError('场景模型已变化，请重新生成语义索引')
    clip = ClipEncoder(config)
    embeddings = clip.texts([text, *config['query']['negatives']])
    frames = pipeline.choose_frames(prepared, 'train')
    ranked = []
    update('search', '正在查找最相关的观察位置')
    with torch.no_grad():
        for frame in frames:
            labels, features = pipeline.read_regions(config, frame['image_id'])
            used = np.unique(labels[labels >= 0])
            if not len(used): continue
            scores = algorithms.text_relevancy(torch.as_tensor(features[used], device='cuda'), embeddings[0], embeddings[1:], temperature=config['query']['temperature'])
            ranked.append((float(scores.max()), frame))
        if not ranked: raise ValueError('未找到有效的物体区域')
        _, anchor = max(ranked, key=lambda row: row[0])
        update('render', '正在渲染三维语义响应')
        result = pipeline.query(config, prepared, anchor, text, 'distilled')
        decoder = pipeline.load_autoencoder(config, prepared)
        latents = pipeline.load_features(config, prepared, 'distilled')
        pieces = []
        for begin in range(0, len(latents), 8192):
            decoded = decoder.decode(latents[begin:begin+8192])
            pieces.append(algorithms.text_relevancy(decoded, embeddings[0], embeddings[1:], temperature=config['query']['temperature']).cpu())
        probabilities = torch.cat(pieces).numpy()
    checkpoint = torch.load(Path(config['rgb_run'])/'checkpoint.pt', map_location='cpu', weights_only=True)
    state, norm = checkpoint['state'], checkpoint['normalization']
    points = state['means'].numpy()*norm['scale']+np.asarray(norm['center'])
    # Restrict candidates to the chosen camera and its rendered positive mask.
    from .rendering import FrozenGaussians
    from .project import image_frame
    renderer = FrozenGaussians(config, prepared)
    _, camera = image_frame(anchor, config['distill']['max_edge'])
    with torch.no_grad():
        depth, alpha = renderer.splat(torch.zeros((len(points), 3), device='cuda'), camera, depth=True)
    p = points@np.asarray(anchor['R']).T+np.asarray(anchor['t'])
    q = p@np.asarray(camera['K']).T
    uv = np.rint(q[:,:2]/np.maximum(q[:,2:],1e-8)).astype(np.int64)
    inside = (p[:,2]>0)&(uv[:,0]>=0)&(uv[:,0]<camera['width'])&(uv[:,1]>=0)&(uv[:,1]<camera['height'])
    ids = np.flatnonzero(inside)
    x,y = uv[ids,0],uv[ids,1]
    with np.load(Path(result['directory'])/'scores.npz',allow_pickle=False) as f: mask=f['mask'].copy()
    depth, alpha = depth.cpu().numpy()*norm['scale'], alpha[...,0].cpu().numpy()
    visible = mask[y,x] & (alpha[y,x]>.1) & (np.abs(p[ids,2]-depth[y,x]) < .05*np.maximum(p[ids,2],1e-6))
    selected = ids[visible & (probabilities[ids]>.6) & (state['opacity_logits'].sigmoid().numpy().reshape(-1)[ids]>.01)]
    if len(selected) >= 20:
        kept,_ = largest_spatial_component(points[selected], max(float(np.linalg.norm(np.ptp(points[selected],axis=0)))*.03, 1e-5))
        selected=selected[kept]
    response = dict(image_id=anchor['image_id'], text=text,candidate_gaussians=len(selected),source_gaussians=len(points), overlay_url=f"/api/scenes/{scene_id}/artifacts/queries/{Path(result['directory']).name}/overlay.png",
                    selection=None, status='not_found', message='未找到足够集中的三维区域，可换一个描述。')
    if 20 <= len(selected) <= .3*len(points):
        import hashlib
        sid='scene-'+hashlib.sha256(json.dumps([identity,text,anchor['image_id']],sort_keys=True).encode()).hexdigest()[:24]
        directory=Path(config['_root'])/'data/object_selections'/sid
        directory.mkdir(parents=True,exist_ok=True)
        np.savez_compressed(directory/'selection.npz',indices=selected.astype(np.int64))
        lower,upper=points[selected].min(0),points[selected].max(0)
        metadata=dict(id=sid,name=text,text=text,source_scene=scene_id,scene_config=Path(config['_root'])/'configs'/f'semantic_{scene_id}.json',
            source_identity=identity,supervision='scene-distilled-v1',status='candidate',normalization=norm,
            rgb_checkpoint_sha256=identity['rgb'],source_gaussians=len(points),selected_gaussians=len(selected),
            selection_sha256=sha256(directory/'selection.npz'),anchor_image_id=anchor['image_id'],view_ids=[anchor['image_id']],
            center=((lower+upper)/2).tolist(),bounds=dict(min=lower.tolist(),max=upper.tolist()),
            scope='Unlabelled scene, camera-visible semantic candidate; not multiview validated or calibrated confidence.')
        metadata['scene_config']=str(Path(metadata['scene_config']).relative_to(Path(config['_root'])))
        write_json(directory/'selection.json',metadata)
        data=io.BytesIO();np.savez_compressed(data,indices=selected.astype(np.int64),**{k:v[selected].numpy() for k,v in state.items()})
        with zipfile.ZipFile(directory/'selection.zip','w',zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('selection.json',json.dumps(metadata,ensure_ascii=False));archive.writestr('gaussians.npz',data.getvalue())
        response.update(selection={**metadata, 'export_url':f'/api/scenes/{scene_id}/selections/{sid}/export','inspection_url':''},status='candidate',message='候选区域，请旋转场景确认。')
    return response


def main(path):
    path=Path(path);job=json.loads(path.read_text());root=Path.cwd()
    job['pid']=os.getpid();write_json(path,job)
    def update(stage,message):
        job.update(stage=stage,message=message);write_json(path,job)
    try:
        config=load_config(root/'configs'/f"{job['config']}.json",root=root)
        from .encoders import gpu_session
        with gpu_session(config,reservation_token=os.environ.get('MINICOLMAP_QUERY_RESERVATION')):
            result=train(config,update) if job['action']=='train' else query(config,job['scene_id'],job['text'],update)
        job.update(status='completed',stage='done',message='语义索引已就绪' if job['action']=='train' else result['message'],result=result)
    except Exception as exc:
        traceback.print_exc();job.update(status='failed',message=str(exc))
    finally:
        from ..ui.gpu_lease import release
        release(root,os.environ.get('MINICOLMAP_QUERY_RESERVATION'))
    write_json(path,job)
    return 0 if job['status']=='completed' else 1

if __name__=='__main__':raise SystemExit(main(sys.argv[1]))
