"""Coarse-to-fine native language-field search followed by guarded 3D selection."""
import argparse
import gc
import hashlib
import json
import os
import time
import traceback
from pathlib import Path

import cv2
import numpy as np
import torch
import langsplat_raw3 as raw
from langsplat_reference_metrics import query_mask
from generate_object_selection import Pipeline,InsufficientEvidence,PIPE,OPT
from geometry_visible_selection import GeometryPipeline,GEOMETRY_POLICY
from check_semantic_multiview import reproject
from minicolmap.rendering.data import scaled_intrinsics
from minicolmap.semantics.auto_locate import SEARCH_POLICY,coarse_views,refine_views,response_score,compatible_support,search_key
from minicolmap.semantics.project import project,write_json
from minicolmap.semantics.selection_policy import source_identity,selection_key
from minicolmap.semantics.selection import selection_directory,load_selection
from minicolmap.semantics.encoders import gpu_session

ROOT=raw.ROOT


class Search:
    def __init__(self,path,value):
        self.path,self.value=path,value
        self.directory=ROOT/'data/auto_locate'/value['id']
        self.directory.mkdir(parents=True,exist_ok=True)
        self.started=time.monotonic();self.views={};self.rows=[]

    def progress(self,stage,progress,message,**kwargs):
        self.value.update(stage=stage,progress=progress,message=message,pid=os.getpid(),
                          seconds=round(time.monotonic()-self.started,2),**kwargs)
        # Publish a terminal status only after the GPU context and report finish.
        # Otherwise a fast client can submit its choice before lease release.
        public=self.value if self.value['status']=='running' else {**self.value,'status':'running','stage':'finishing','progress':.99}
        write_json(self.path,public)

    def view(self,frame,models,codec,clip):
        from gaussian_renderer import render
        width=SEARCH_POLICY['max_edge'];height=round(frame['height']*width/frame['width'])
        small={**frame,'width':width,'height':height,
            'K':scaled_intrinsics(np.asarray(frame['K']),(frame['width'],frame['height']),(width,height)).tolist()}
        cache_key=hashlib.sha256(json.dumps(dict(source=self.value['source_identity'],max_edge=width),sort_keys=True).encode()).hexdigest()[:24]
        folder=ROOT/'data/auto_locate/cache'/cache_key;folder.mkdir(parents=True,exist_ok=True)
        cache=folder/f'{frame["image_id"]:06d}.npz'
        cached=False
        if cache.is_file():
            try:
                with np.load(cache,allow_pickle=False) as data:
                    latents=data['latents'].astype(np.float32);depth=data['depth'];alpha=data['alpha']
                if latents.shape!=(3,3,height,width) or not np.isfinite(latents).all():raise ValueError('Invalid cache')
                cached=True
            except (OSError,ValueError,KeyError):pass
        if not cached:
            camera=raw.ref.native_camera(small);latents=[]
            for model in models:
                latent=render(camera,model,PIPE,torch.ones(3,device='cuda'),OPT)['language_feature_image']
                latents.append(latent.cpu().numpy())
            latents=np.stack(latents)
            model=models[0];saved=model._language_feature;model._language_feature=torch.ones_like(saved)
            try:
                z=(model.get_xyz@torch.tensor(small['R'],device='cuda').T+torch.tensor(small['t'],device='cuda'))[:,2]
                output=render(camera,model,PIPE,torch.zeros(3,device='cuda'),OPT,override_color=z[:,None].expand(-1,3).contiguous())
            finally:model._language_feature=saved
            alpha=output['language_feature_image'][0].cpu().numpy()
            depth=output['render'][0].cpu().numpy()/np.maximum(alpha,1e-8)
            # Cache the exact float32 values: subsequent texts see identical fields.
            temporary=cache.with_suffix('.tmp.npz');np.savez_compressed(temporary,latents=latents,depth=depth,alpha=alpha);temporary.replace(cache)
        maps=[]
        for latent in latents:
            values=torch.as_tensor(latent,device='cuda').permute(1,2,0).reshape(-1,3)
            scores=[clip.get_relevancy(codec.decode(chunk),0)[:,0].cpu() for chunk in values.split(8192)]
            maps.append(torch.cat(scores).reshape(height,width).numpy())
        maps=np.stack(maps);level,mask=query_mask(maps)
        self.views[frame['image_id']]=(small,depth,alpha,mask)
        stats=response_score(maps,mask,level)
        if stats is None:return
        count,labels,components,_=cv2.connectedComponentsWithStats(mask,8)
        for component in sorted(range(1,count),key=lambda n:-components[n,cv2.CC_STAT_AREA])[:3]:
            region=(labels==component).astype(np.uint8)
            quality=response_score(maps,region,level)
            if quality is None:continue
            y,x=np.where((region>0)&(alpha>.95)&np.isfinite(depth)&(depth>.01))
            if len(x)<SEARCH_POLICY['min_pixels']:continue
            rays=np.stack([x,y,np.ones(len(x))],1)@np.linalg.inv(small['K']).T
            points=(rays*depth[y,x,None]-np.asarray(small['t']))@np.asarray(small['R'])
            lower,upper=np.quantile(points,[.02,.98],axis=0)
            center=np.median(points,axis=0)
            row=dict(image_id=frame['image_id'],id=f'{frame["image_id"]}-{component}',level=level+1,
                center=center.tolist(),bounds=dict(min=lower.tolist(),max=upper.tolist()),**quality)
            # Private arrays are retained only inside this GPU job.
            row['_mask']=region
            self.rows.append(row)

    def candidates(self,prepared):
        groups=[];scale=prepared['normalization']['scale']
        for row in sorted(self.rows,key=lambda r:(-r['score'],r['image_id'],r['id'])):
            frame,depth,alpha,_=self.views[row['image_id']]
            matched=None
            for group in groups:
                representative=group[0]
                if not compatible_support(row,representative,scale):continue
                f,d,a,_=self.views[representative['image_id']]
                evidence=reproject((f,d,a),(frame,depth,alpha),representative['_mask'],row['_mask'],scale)
                if (evidence['reprojection_iou'] is not None and evidence['reprojection_iou']>=SEARCH_POLICY['min_reprojection_iou']
                        and evidence['foreground_agreement']>=SEARCH_POLICY['min_foreground_agreement']):matched=group;break
            if matched is None:groups.append([row])
            elif row['image_id'] not in {r['image_id'] for r in matched}:matched.append(row)
        candidates=[]
        for group in groups:
            if len(group)<SEARCH_POLICY['min_support_views']:continue
            best=group[0];candidate={k:v for k,v in best.items() if not k.startswith('_')}
            candidate['support_views']=[r['image_id'] for r in group]
            candidate['ranking_score']=float(np.mean([r['score'] for r in group[:3]])*(1+.1*min(5,len(group))))
            candidate['thumbnail_url']=f'/api/demo/auto-locate/{self.value["id"]}/images/{candidate["id"]}'
            frame,_,_,_=self.views[best['image_id']]
            photo=cv2.resize(cv2.imread(frame['path']),(frame['width'],frame['height']))
            photo[best['_mask']>0]=(.4*photo[best['_mask']>0]+.6*np.array([100,220,140])).astype(np.uint8)
            cv2.imwrite(str(self.directory/f'{candidate["id"]}.jpg'),photo)
            candidates.append(candidate)
        return sorted(candidates,key=lambda c:(-c['ranking_score'],c['id']))[:3]

    def run(self):
        config=raw.configure();prepared=project(config)
        if source_identity(ROOT)!=self.value['source_identity']:raise ValueError('源模型已更新，请重新搜索')
        if self.value['search_key']!=search_key(self.value['request']['text'],self.value['source_identity']):raise ValueError('搜索策略已更新，请重新搜索')
        frames={f['image_id']:f for f in prepared['frames']}
        with gpu_session({**config,'workspace':str(self.directory)},reservation_token=os.environ.get('MINICOLMAP_QUERY_RESERVATION')):
            if not self.value.get('chosen_id'):
                raw.ref.seed();raw.ref.native_imports();raw.install_renderer()
                models=raw.ref.native_models(prepared,30000)
                codec=raw.ref.upstream_ae();codec.load_state_dict(torch.load(raw.WORK/'autoencoder/best_ckpt.pth',weights_only=True));codec.eval()
                clip=raw.ref.reference_clip(config);clip.set_positives([self.value['request']['text']])
                coarse=coarse_views(prepared);seen=[]
                with torch.inference_mode():
                    for n,i in enumerate(coarse):
                        self.view(frames[i],models,codec,clip);seen.append(i)
                        self.progress('coarse',.02+.2*(n+1)/len(coarse),f'正在粗搜视角：{n+1} / {len(coarse)}',searched_views=list(seen))
                    ranked=sorted(self.rows,key=lambda r:-r['score']);unique=[]
                    for row in ranked:
                        if row['image_id'] not in {r['image_id'] for r in unique}:unique.append(row)
                    refine=refine_views(prepared,unique,seen)
                    for n,i in enumerate(refine):
                        self.view(frames[i],models,codec,clip);seen.append(i)
                        self.progress('refine',.22+.16*(n+1)/max(1,len(refine)),f'正在细搜候选附近视角：{n+1} / {len(refine)}',searched_views=list(seen))
                candidates=self.candidates(prepared)
                del models,codec,clip;gc.collect();torch.cuda.empty_cache()
                write_json(self.directory/'search.json',dict(policy=SEARCH_POLICY,searched_views=seen,candidates=candidates,
                    scores=[{k:v for k,v in r.items() if not k.startswith('_')} for r in self.rows],test_labels_used=False,
                    scope='Coarse-to-fine camera search, not exhaustive search or annotated accuracy'))
                self.value['candidates']=candidates
                if not candidates:
                    self.progress('finished',1,'未找到具有至少三个一致视角的候选区域；请换一种英文描述或使用手动查询',status='rejected')
                    return
                if len(candidates)>1 and candidates[1]['ranking_score']>=SEARCH_POLICY['ambiguous_score_ratio']*candidates[0]['ranking_score']:
                    self.progress('choose',1,'发现多个分离的候选区域，请查看缩略图并确认目标',status='needs_choice')
                    return
                chosen=candidates[0]
            else:
                chosen=next(c for c in self.value['candidates'] if c['id']==self.value['chosen_id'])
            self.select(config,chosen)
        if source_identity(ROOT)!=self.value['source_identity']:raise ValueError('源模型在搜索期间更新')

    def select(self,config,chosen):
        text=self.value['request']['text'];anchor=chosen['image_id']
        # Ambiguous choices are bounded in 3D; conservative full-query coverage
        # checks still apply, so a partial instance may be rejected rather than merged.
        hint=None
        if self.value.get('chosen_id'):
            lo=np.asarray(chosen['bounds']['min']);hi=np.asarray(chosen['bounds']['max'])
            prepared=project(config);padding=.2*(hi-lo)+.005*prepared['normalization']['scale']
            hint={'min':(lo-padding).tolist(),'max':(hi+padding).tolist()}
        sid=selection_key(text,anchor,self.value['source_identity'],dict(visibility_policy=GEOMETRY_POLICY,hint_bounds=hint))
        directory=selection_directory(ROOT,sid);directory.mkdir(parents=True,exist_ok=True)
        self.progress('selection',.4,f'已选择相机 {anchor}，正在生成并验证三维选区',selection_id=sid,anchor_image_id=anchor,selected_candidate=chosen['id'])
        validation=directory/'validation.json'
        if (directory/'selection.json').is_file():
            load_selection(ROOT,sid)
            self.progress('finished',1,'已复用通过检查的选区，即将自动定位；请目视确认',status='completed')
            return
        if validation.is_file() and not json.loads(validation.read_text()).get('evidence',{}).get('accepted'):
            self.progress('finished',1,'候选视角已找到，但已有三维选区验证未通过；请查看诊断或选择其他候选',status='rejected')
            return
        child_id=self.value['id'];child_path=ROOT/f'.cache/object-selection-jobs/{child_id}.json'
        value=dict(id=child_id,selection_id=sid,status='running',request=dict(model='raw3',text=text,image_id=anchor),pid=os.getpid(),
                   report_url=f'/api/demo/selections/{sid}/report')
        write_json(child_path,value)
        write_json(directory/'request.json',dict(**value['request'],source_identity=self.value['source_identity'],selection_id=sid,hint_bounds=hint,supervision='field-visible-v1',supervision_policy=GEOMETRY_POLICY))
        parent=self
        class LinkedPipeline(GeometryPipeline):
            def progress(self,stage,progress,message,**kwargs):
                super().progress(stage,progress,message,**kwargs)
                parent.progress('selection_'+stage,.4+.59*progress,message)
        pipeline=LinkedPipeline(child_path,value)
        try:pipeline.run(gpu_reserved=True)
        except InsufficientEvidence as exc:
            (directory/'README.md').write_text(f'# 三维选区证据不足\n\n{exc}\n')
            pipeline.progress('finished',1,str(exc),status='rejected')
        except Exception:
            pipeline.progress('failed',0,'三维选区生成失败，可重新提交',status='failed');raise
        self.progress('finished',1,pipeline.value['message'],status=pipeline.value['status'],
            evidence=pipeline.value.get('evidence'),selected_gaussians=pipeline.value.get('selected_gaussians'))

    def report(self):
        rows=self.value.get('candidates',[])
        lines=[f'# 自动查找：{self.value["request"]["text"]}', '',self.value.get('message',''),'',
            f'状态：{self.value["status"]}；耗时 {self.value.get("seconds",0):.1f} 秒；搜索 {len(self.value.get("searched_views",[]))} 个训练视角。',
            '先按相机位姿分散粗搜，再搜索高分视角附近。不是遍历全部相机，不保证发现所有物体。',
            '排序分数不是概率；候选是几何一致的区域，不保证对应不同真实实例。三维选区仍需独立视角检查和目视确认。', '',
            '| 候选 | 相机 | 支持视角数 | 排序分数 |','|---|---:|---:|---:|']
        for c in rows:lines.append(f'| {c["id"]} | {c["image_id"]} | {len(c["support_views"])} | {c["ranking_score"]:.4f} |')
        if self.value.get('selection_id'):lines+=['',f'选区：{self.value["selection_id"]}；详细指标见对应选区报告。']
        lines+=['','原 RGB 与语言场保持不变。使用显式指定的单张显卡，不读取 Test 人工标注。',
            '同视角多个同类物体可能无法通过完整查询区域覆盖检查；本版本不承诺实例级分割。']
        (self.directory/'README.md').write_text('\n'.join(lines)+'\n')


def main():
    parser=argparse.ArgumentParser(__doc__);parser.add_argument('--job-id',required=True);args=parser.parse_args()
    if len(args.job_id)!=32 or any(c not in '0123456789abcdef' for c in args.job_id):parser.error('Invalid job id')
    path=ROOT/f'.cache/auto-locate-jobs/{args.job_id}.json';worker=Search(path,json.loads(path.read_text()))
    try:
        worker.progress('starting',0,'正在启动自动查找');worker.run()
    except Exception:
        traceback.print_exc();worker.progress('failed',worker.value.get('progress',0),'自动查找失败，日志已保留，可重试',status='failed')
        raise
    finally:
        worker.report()
        from minicolmap.ui.gpu_lease import release
        if os.environ.get('MINICOLMAP_QUERY_RESERVATION'):release(ROOT,os.environ['MINICOLMAP_QUERY_RESERVATION'])
        write_json(path,worker.value)
    print(json.dumps(worker.value,ensure_ascii=False))


if __name__=='__main__':main()
