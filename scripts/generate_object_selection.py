"""Text masks -> frozen-compositor membership -> independent-view evidence gate.

No learner code, RGB/semantic parameters, or manual evaluation labels are changed.
Run via the workbench, or --text 'dark cup' --image-id 124 for a CLI job.
"""
import argparse
import gc
import json
import os
import time
import traceback
import uuid
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import torch
import langsplat_raw3 as raw
from langsplat_reference_metrics import query_mask
from check_semantic_multiview import select_views, reproject
from selection_exports import save_selection
from minicolmap.rendering.live import CudaRenderer, normalize_camera
from minicolmap.rendering.gaussian_backend import rasterize_gsplat
from minicolmap.semantics.encoders import gpu_session
from minicolmap.semantics.project import project, write_json, sha256
from minicolmap.semantics.selection import selection_directory, largest_spatial_component
from minicolmap.semantics.selection_policy import POLICY, assess_evidence, selection_key
from minicolmap.ui.selection_jobs import source_identity, validate_payload

ROOT = raw.ROOT
PIPE = SimpleNamespace(debug=False, convert_SHs_python=False, compute_cov3D_python=False)
OPT = SimpleNamespace(include_feature=True)


class InsufficientEvidence(ValueError):
    pass


def world_roi(frame, view, scale):
    mask, depth, alpha = view['mask'], view['depth'], view['alpha']
    y, x = np.where((mask > 0) & (alpha > .95) & np.isfinite(depth) & (depth > .01))
    if len(x) < POLICY['min_query_pixels']:
        raise InsufficientEvidence('锚点视角没有足够的可见目标表面；请换一个能清楚看到物体的相机')
    rays = np.stack([x, y, np.ones(len(x))], axis=1) @ np.linalg.inv(frame['K']).T
    surface = (rays * depth[y, x, None] - np.asarray(frame['t'])) @ np.asarray(frame['R'])
    lower, upper = np.quantile(surface, [.02, .98], axis=0)
    padding = .1 * (upper - lower) + .005 * scale
    return lower - padding, upper + padding


def validation_views(prepared, anchor_id, fit_ids, center):
    frames = {f['image_id']: f for f in prepared['frames']}
    anchor = frames[anchor_id]; rotation = np.asarray(anchor['R'])
    origin = -rotation.T @ np.asarray(anchor['t'])
    ranking = []
    for i in prepared['splits']['train']:
        if i in fit_ids:continue
        f = frames[i]; R, t = np.asarray(f['R']), np.asarray(f['t'])
        p = R @ center + t
        if p[2] <= .01:continue
        uv = np.asarray(f['K']) @ p; uv = uv[:2] / uv[2]
        if not (.15*f['width'] < uv[0] < .85*f['width'] and .15*f['height'] < uv[1] < .85*f['height']):continue
        distance = np.linalg.norm(-R.T@t - origin) / prepared['normalization']['scale']
        angle = np.arccos(np.clip(R[2]@rotation[2], -1, 1))
        ranking.append((distance + .2*angle, i))
    ordered = [i for _, i in sorted(ranking)]
    if len(ordered) < 9:raise InsufficientEvidence('目标附近可用于独立验证的训练视角不足')
    return [ordered[i] for i in (0, 1, 3, 5, 8)]


class Pipeline:
    def __init__(self, job_path, value):
        self.path, self.value = job_path, value
        self.directory = selection_directory(ROOT, value['selection_id'])
        self.started = time.monotonic()
        self.views = {}

    def progress(self, stage, progress, message, **kwargs):
        self.value.update(stage=stage, progress=progress, message=message, pid=os.getpid(),
                          seconds=round(time.monotonic()-self.started, 2), **kwargs)
        write_json(self.path, self.value)

    def prepare_fit_views(self, prepared, frames, anchor, ids, models, codec, clip):
        return ids

    def render_views(self, ids, frames, models, codec, clip, start, end):
        from gaussian_renderer import render
        with torch.inference_mode():
            for n, i in enumerate(ids):
                frame = frames[i]; camera = raw.ref.native_camera(frame); maps = []
                for model in models:
                    latent = render(camera, model, PIPE, torch.ones(3, device='cuda'), OPT)['language_feature_image']
                    latent = latent.permute(1, 2, 0).reshape(-1, 3)
                    values = [clip.get_relevancy(codec.decode(chunk), 0)[:, 0].cpu() for chunk in latent.split(8192)]
                    maps.append(torch.cat(values).reshape(frame['height'], frame['width']).numpy())
                maps = np.stack(maps); level, mask = query_mask(maps)
                model = models[0]; saved = model._language_feature
                model._language_feature = torch.ones_like(saved)
                try:
                    z = (model.get_xyz @ torch.tensor(frame['R'], device='cuda').T + torch.tensor(frame['t'], device='cuda'))[:, 2]
                    output = render(camera, model, PIPE, torch.zeros(3, device='cuda'), OPT,
                                    override_color=z[:, None].expand(-1, 3).contiguous())
                finally:model._language_feature = saved
                alpha = output['language_feature_image'][0].cpu().numpy()
                depth = output['render'][0].cpu().numpy()/np.maximum(alpha, 1e-8)
                self.views[i] = dict(mask=mask, depth=depth, alpha=alpha, level=level+1)
                np.savez_compressed(self.directory/f'view-{i:06d}.npz', mask=mask, depth=depth, alpha=alpha, maps=maps)
                self.progress('masks', start+(end-start)*(n+1)/len(ids), f'正在提取多视角证据：相机 {i}')

    def run(self, *, gpu_reserved=False):
        request = self.value['request']; text, anchor = request['text'], request['image_id']
        config = raw.configure(); prepared = project(config)
        stored = json.loads((self.directory/'request.json').read_text())
        if source_identity(ROOT) != stored['source_identity']:
            raise ValueError('模型或相机已更新，请重新提交选区任务')
        # Keep a content identity in the audit, not just file invalidation stamps.
        self.progress('checking', .01, '正在核对源模型与固定验证策略')
        sources = [Path(config['rgb_run'])/'checkpoint.pt', raw.WORK/'autoencoder/best_ckpt.pth',
                   *[raw.WORK/f'train/level{i}/chkpnt30000.pth' for i in (1,2,3)]]
        hashes = {str(p.relative_to(ROOT)): sha256(p) for p in sources}
        if hashes[str(sources[0].relative_to(ROOT))] != prepared['rgb_checkpoint_sha256']:
            raise ValueError('RGB 源模型与语义项目不一致')
        write_json(self.directory/'policy.json', dict(policy=POLICY, source_hashes=hashes,
            fitting='Anchor plus five training views selected by camera pose only',
            validation='Five training views excluded from membership fitting; pose and anchor-depth ROI center only',
            test_labels_used=False))
        frames = {f['image_id']: f for f in prepared['frames']}
        fit_ids = list(dict.fromkeys(select_views(prepared, anchor)))
        if anchor not in fit_ids:fit_ids.insert(0, anchor)
        fit_ids = fit_ids[:6]
        if len(fit_ids) != 6:raise InsufficientEvidence('附近拟合视角不足六个')
        checkpoint = torch.load(sources[0], map_location='cpu', weights_only=True)
        state, norm = checkpoint['state'], checkpoint['normalization']; scale = norm['scale']
        with (nullcontext() if gpu_reserved else gpu_session({**config, 'workspace':str(self.directory)}, reservation_token=os.environ.get('MINICOLMAP_QUERY_RESERVATION'))):
            raw.ref.seed(); raw.ref.native_imports(); raw.install_renderer()
            models = raw.ref.native_models(prepared, 30000)
            codec = raw.ref.upstream_ae(); codec.load_state_dict(torch.load(raw.WORK/'autoencoder/best_ckpt.pth', weights_only=True)); codec.eval()
            clip = raw.ref.reference_clip(config); clip.set_positives([text])
            fit_ids = self.prepare_fit_views(prepared,frames,anchor,fit_ids,models,codec,clip)
            self.render_views(fit_ids, frames, models, codec, clip, .03, .25)
            lower, upper = world_roi(frames[anchor], self.views[anchor], scale)
            validation_ids = validation_views(prepared, anchor, fit_ids, (lower+upper)/2)
            self.render_views(validation_ids, frames, models, codec, clip, .25, .43)
            del models, codec, clip
            gc.collect(); torch.cuda.empty_cache()
            consistency = []
            a = self.views[anchor]
            for i in fit_ids:
                if i == anchor:continue
                b = self.views[i]
                consistency.append(dict(image_id=i, **reproject((frames[anchor], a['depth'], a['alpha']),
                    (frames[i], b['depth'], b['alpha']), a['mask'], b['mask'], scale)))
            probabilities, history = self.fit(config, prepared, frames, fit_ids)
            points = state['means'].numpy()*scale+np.asarray(norm['center'])
            roi = (points >= lower).all(1) & (points <= upper).all(1)
            if stored.get('hint_bounds'):
                hint=stored['hint_bounds']
                roi &= (points>=np.asarray(hint['min'])).all(1)&(points<=np.asarray(hint['max'])).all(1)
            candidates = np.flatnonzero((probabilities >= POLICY['membership_threshold']) &
                (state['opacity_logits'].sigmoid().numpy().ravel() >= POLICY['min_opacity']) & roi)
            connected, components = largest_spatial_component(points[candidates], POLICY['voxel_size_scale']*scale)
            indices = candidates[connected].astype(np.int64)
            np.savez_compressed(self.directory/'candidate.npz', indices=indices, membership=probabilities[indices])
            if not len(indices):raise InsufficientEvidence('没有形成连贯的三维目标表面；已保留诊断证据')
            self.progress('validating', .82, '正在检查五个未用于拟合的视角')
            rows = self.validate(config, frames, validation_ids, indices)
            evidence = assess_evidence(rows, consistency, len(indices), len(points))
            # Verify protected sources even for rejected candidates.
            for p in sources:
                if sha256(p) != hashes[str(p.relative_to(ROOT))]:raise ValueError('源模型在任务期间发生变化；不发布选区')
            if source_identity(ROOT) != stored['source_identity']:raise ValueError('模型或相机在任务期间发生变化')
        report = dict(status='completed', selection_id=self.value['selection_id'], text=text,
            fit_ids=fit_ids, validation_ids=validation_ids, per_view=rows, fit_consistency=consistency,
            selected_gaussians=len(indices), candidates=len(candidates), component_sizes=components,
            evidence=evidence, source_hashes=hashes, original_geometry_frozen=True,
            original_rgb_preserved=True, test_labels_used=False)
        report['supervision']=stored.get('supervision','field')
        write_json(self.directory/'validation.json', report)
        write_json(self.directory/'fit-history.json', history)
        self.report(report)
        if evidence['accepted']:
            selected = points[indices]
            metadata = dict(schema_version=1, id=self.value['selection_id'], name=text, text=text, model='raw3',
                anchor_image_id=anchor, status='prototype', rgb_checkpoint_sha256=prepared['rgb_checkpoint_sha256'],
                source_gaussians=len(points), selected_gaussians=len(indices),
                center=np.average(selected, axis=0, weights=probabilities[indices]).tolist(),
                bounds=dict(min=selected.min(0).tolist(), max=selected.max(0).tolist()),
                coordinate_system='original_sfm_world', units='reconstruction units; not meters', view_ids=fit_ids,
                method='Independent membership fit through frozen Gaussian compositor; depth ROI and largest connected component',
                parameters=POLICY, original_geometry_frozen=True, evidence=evidence,
                source_hashes=hashes, validation_ids=validation_ids,
                hint_bounds=stored.get('hint_bounds'),
                source_identity=stored['source_identity'],
                supervision=stored.get('supervision','field'),teacher_identity=stored.get('teacher_identity'),
                supervision_policy=stored.get('supervision_policy'),
                scope='Visible-surface prototype. Language consistency does not prove correct object identity.')
            save_selection(self.directory, state, norm, indices, metadata, membership=probabilities[indices])
        self.progress('finished', 1, '选区已生成并通过一致性检查；仍需目视确认' if evidence['accepted'] else '；'.join(evidence['reasons']),
            status='completed' if evidence['accepted'] else 'rejected', evidence=evidence,
            selected_gaussians=len(indices), validation_ids=validation_ids)

    def fit(self, config, prepared, frames, ids):
        from gaussian_renderer import render
        _, GaussianModel = raw.ref.native_imports(); model = GaussianModel(3)
        params, _ = torch.load(raw.ref.rgb_checkpoint(config, prepared), map_location='cuda', weights_only=True)
        model.restore(params, OPT, mode='eval')
        for name in ('_xyz','_features_dc','_features_rest','_scaling','_rotation','_opacity'):
            getattr(model, name).requires_grad_(False)
        logits = torch.full((len(model.get_xyz),1), -2., device='cuda', requires_grad=True)
        optimizer = torch.optim.Adam([logits], lr=.03)
        targets, alphas, cameras, valid_ids = [], [], [], []
        for i in ids:
            view = self.views[i]; target = torch.as_tensor(cv2.erode(view['mask'], np.ones((5,5),np.uint8)), device='cuda', dtype=torch.float32)
            alpha = torch.as_tensor(view['alpha'], device='cuda')
            if int(((target>.5)&(alpha>.95)).sum()) < 25 or int(((target<.5)&(alpha>.95)).sum()) < 25:continue
            targets.append(target); alphas.append(alpha); cameras.append(raw.ref.native_camera(frames[i])); valid_ids.append(i)
        if len(valid_ids) < 4:raise InsufficientEvidence('至少四个拟合视角需要同时包含可见前景与背景；当前语义证据不足')
        history = []
        for step in range(POLICY['steps']):
            v = step % len(cameras)
            model._language_feature = logits.sigmoid().expand(-1,3).contiguous()
            prediction = render(cameras[v], model, PIPE, torch.zeros(3,device='cuda'), OPT)['language_feature_image'][0]
            alpha, target = alphas[v], targets[v]; valid = alpha>.95
            prediction = (prediction/alpha.clamp_min(1e-6)).clamp(1e-5,1-1e-5)
            fg = valid & (target>.5); bg = valid & ~fg
            loss = .5*(-prediction[fg].log().mean()-(1-prediction[bg]).log().mean())
            if not torch.isfinite(loss):raise ValueError('选区归属优化出现非有限损失')
            optimizer.zero_grad(set_to_none=True); loss.backward(); optimizer.step()
            if step % 100 == 0 or step == POLICY['steps']-1:
                history.append(dict(step=step+1, loss=float(loss.detach())))
                self.progress('fitting', .43+.38*(step+1)/POLICY['steps'], f'正在融合三维选区：{step+1} / {POLICY["steps"]}',
                    step=step+1, loss=history[-1]['loss'])
        for expected, actual in zip(params[1:7], (model._xyz,model._features_dc,model._features_rest,model._scaling,model._rotation,model._opacity)):
            torch.testing.assert_close(expected, actual, atol=0, rtol=0)
        probabilities = logits.detach().sigmoid().cpu().numpy().ravel()
        torch.save(logits.detach().cpu(), self.directory/'membership_logits.pt')
        del model, params, optimizer, logits, targets, alphas, cameras, prediction, loss
        gc.collect(); torch.cuda.empty_cache()
        return probabilities, history

    def validate(self, config, frames, ids, indices):
        renderer = CudaRenderer(config['rgb_run'])
        indicator = torch.zeros((len(renderer.state['means']),3),device='cuda')
        indicator[torch.as_tensor(indices,device='cuda')] = 1
        rows, tiles = [], []
        with torch.inference_mode():
            for n, i in enumerate(ids):
                frame = frames[i]; camera = {k:frame[k] for k in ('width','height','K','R','t')}
                normalized = normalize_camera(camera, renderer.normalization)
                K,R,t = [torch.tensor(normalized[k],device='cuda',dtype=torch.float32) for k in ('K','R','t')]
                influence = rasterize_gsplat(renderer.state['means'], renderer.covariance, indicator, renderer.opacities,
                    K,R,t,frame['width'],frame['height'],torch.zeros(3,device='cuda'))[...,0].cpu().numpy()
                mask, reference = influence>.2, self.views[i]['mask']>0
                intersection = int((mask&reference).sum()); union = int((mask|reference).sum())
                rows.append(dict(image_id=i, query_pixels=int(reference.sum()), selected_pixels=int(mask.sum()),
                    selected_query_precision=intersection/max(1,int(mask.sum())), query_coverage=intersection/max(1,int(reference.sum())),
                    query_iou=intersection/max(1,union)))
                rgb = (renderer.render_rgb(camera).clamp(0,1).cpu().numpy()*255).round().astype(np.uint8)
                rgb[mask] = (.35*rgb[mask]+.65*np.array([65,240,128])).astype(np.uint8)
                tile = cv2.resize(cv2.cvtColor(rgb,cv2.COLOR_RGB2BGR),(394,290))
                caption = np.zeros((48,394,3),np.uint8)
                cv2.putText(caption,f'camera {i} | precision {rows[-1]["selected_query_precision"]:.2f}',(8,19),cv2.FONT_HERSHEY_SIMPLEX,.47,(220,235,225),1)
                cv2.putText(caption,f'coverage {rows[-1]["query_coverage"]:.2f} | IoU {rows[-1]["query_iou"]:.2f}',(8,40),cv2.FONT_HERSHEY_SIMPLEX,.47,(220,235,225),1)
                tiles.append(np.concatenate([tile,caption]))
                cv2.imwrite(str(self.directory/f'validation-{i:06d}.jpg'),np.concatenate([tile,caption]))
                self.progress('validating', .82+.13*(n+1)/len(ids), f'正在验证独立视角：{n+1} / {len(ids)}')
        tiles.append(np.zeros_like(tiles[0]))
        cv2.imwrite(str(self.directory/'validation-views.jpg'),np.concatenate([np.concatenate(tiles[:3],1),np.concatenate(tiles[3:6],1)],0))
        return rows

    def report(self, report):
        evidence = report['evidence']
        lines = [f'# 文本三维选区：{report["text"]}', '',
            f'选区：{report["selection_id"]}；候选高斯：{report["selected_gaussians"]:,}。', '',
            '**一致性检查通过，仍需目视确认。**' if evidence['accepted'] else '**证据不足：不发布、不开放三维选区导出。**', '',
            *evidence['reasons'], '', '原 RGB、语义场与学习者代码保持不变；仅拟合独立归属权重。',
            '六个相机提供拟合证据，五个未参与拟合的训练相机作验证；不读取 Test 人工标注。',
            '下表与二维语言查询比较，不是人工三维准确率；持续选错同一物体仍可能通过。', '',
            '| 相机 | 查询内比例 | 查询覆盖率 | 查询 IoU |','|---|---:|---:|---:|']
        for r in report['per_view']:
            lines.append(f'| {r["image_id"]} | {r["selected_query_precision"]:.3f} | {r["query_coverage"]:.3f} | {r["query_iou"]:.3f} |')
        lines += ['', f'独立视角通过 {evidence["good_validation_views"]}/5；重投影一致视角 {evidence["consistent_fit_views"]}/5。',
            '固定门槛与源模型哈希见 policy.json，完整数据见 validation.json。',
            '可见表面原型，可能存在误选与遮挡空洞；坐标单位不是米，PLY 仅为高斯中心点云。']
        (self.directory/'README.md').write_text('\n'.join(lines)+'\n')


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--job-id'); parser.add_argument('--text'); parser.add_argument('--image-id',type=int)
    args = parser.parse_args(); folder = ROOT/'.cache/object-selection-jobs'; folder.mkdir(parents=True,exist_ok=True)
    if args.job_id:
        if len(args.job_id)!=32 or any(c not in '0123456789abcdef' for c in args.job_id):parser.error('Invalid job ID')
        path = folder/f'{args.job_id}.json'; value = json.loads(path.read_text())
    else:
        request = validate_payload(ROOT, dict(model='raw3',image_id=args.image_id,text=args.text))
        identity = source_identity(ROOT); sid = selection_key(request['text'],request['image_id'],identity)
        directory = selection_directory(ROOT,sid); directory.mkdir(parents=True,exist_ok=True)
        if (directory/'selection.json').exists():parser.error('Selection exists; use the workbench cache')
        job_id = uuid.uuid4().hex; path = folder/f'{job_id}.json'
        value = dict(id=job_id,selection_id=sid,status='running',request=request,pid=os.getpid(),
            report_url=f'/api/demo/selections/{sid}/report')
        write_json(directory/'request.json',dict(**request,source_identity=identity,selection_id=sid))
        write_json(path,value)
    pipeline = Pipeline(path,value)
    pipeline.progress('starting',0,'正在启动三维选区任务')
    try:pipeline.run()
    except InsufficientEvidence as exc:
        (pipeline.directory/'README.md').write_text(f'# 三维选区证据不足\n\n{exc}\n\n未发布选区；原模型保持不变。\n')
        pipeline.progress('finished',1,str(exc),status='rejected')
    except Exception:
        traceback.print_exc()
        pipeline.progress('failed',pipeline.value.get('progress',0),'选区生成失败，诊断日志已保留；可重试',status='failed')
        raise
    finally:
        from minicolmap.ui.gpu_lease import release
        if os.environ.get('MINICOLMAP_QUERY_RESERVATION'):release(ROOT,os.environ['MINICOLMAP_QUERY_RESERVATION'])
    print(json.dumps(pipeline.value,ensure_ascii=False))


if __name__=='__main__':main()
