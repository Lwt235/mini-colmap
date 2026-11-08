"""Fixed raw3 teacher -> codec -> field diagnostics on Dev and failed nearby views.

No model fitting, prompt search, threshold changes or Test annotations. Original
GT is used ONLY for diagnostic measurements/oracles on Dev cameras 62 and 124.
"""
import gc,json,time
from pathlib import Path
from types import SimpleNamespace
import cv2
import numpy as np
import torch
import langsplat_raw3 as raw
from langsplat_reference_metrics import query_mask,annotations,evaluate_maps,load_metrics
from check_semantic_multiview import reproject
from minicolmap.semantics.project import project,write_json,sha256
from minicolmap.semantics.encoders import gpu_session
from minicolmap.semantics.diagnostics import mask_metrics,sam_union_oracle,pool_region_codes,projected_support

ROOT=raw.ROOT;REPORT=ROOT/'reports/semantic-stage-diagnostics-20261022'
EXTRA={63:['knife'],94:['knife'],118:['dark cup'],136:['dark cup'],126:['sink']}
FOCUS={62:['knife'],124:['dark cup','sink'],**EXTRA}
STAGES=['teacher','reconstructed','field','region_pooled']


def scatter(scores,labels):
    output=np.zeros((len(scores),*labels.shape),np.float32);valid=labels>=0
    output[:,valid]=scores[:,labels[valid]]
    return output


def query_scores(clip,features,names):
    if not torch.isfinite(features).all():raise ValueError('Non-finite semantic features')
    return torch.stack([clip.get_relevancy(features,i)[:,0] for i in range(len(names))]).float().cpu().numpy()


def overlay(photo,mask,gt=None):
    result=photo.copy();result[mask>0]=(.4*result[mask>0]+.6*np.array([85,220,135])).astype(np.uint8)
    if gt is not None:
        contours,_=cv2.findContours(gt.astype(np.uint8),cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(result,contours,-1,(50,90,255),2)
    return result


def main():
    if (REPORT/'results.json').exists():raise ValueError('Completed diagnosis exists; preserve it and use a new explicit report path')
    REPORT.mkdir(parents=True,exist_ok=True);started=time.monotonic()
    config=raw.configure();prepared=project(config);frames={f['image_id']:f for f in prepared['frames']}
    ann={a['image_id']:a for a in prepared['annotations'] if a['image_id'] in prepared['splits']['dev']}
    ids=sorted(set(prepared['splits']['dev'])|set(EXTRA))
    sources=[Path(config['rgb_run'])/'checkpoint.pt',raw.WORK/'autoencoder/best_ckpt.pth',ROOT/'src/minicolmap/semantics/algorithms.py',
        *[raw.WORK/f'train/level{i}/chkpnt30000.pth' for i in (1,2,3)]]
    protected={str(p.relative_to(ROOT)):sha256(p) for p in sources}
    write_json(REPORT/'protocol.json',dict(model='raw3',views=ids,dev_ids=prepared['splits']['dev'],extra_queries=EXTRA,
        focus=FOCUS,stages=STAGES,source_hashes=protected,threshold=.4,filter_size=30,majority_window=7,
        negatives=['object','things','stuff','texture'],test_labels_used=False,models_updated=False,
        view_selection='Pre-existing failed viewpoints plus Dev anchors; reused development data, not blind validation',
        controls=dict(fixed_head='Same teacher-selected head across stages; no GT head selection',
            oracle='GT-only SAM union upper bound and best-head diagnostic; never deploy or use to select parameters',
            region_pooled='Teacher SAM labels replace rendered pixel codes by per-region means; changes spatial prior, not a pure training ablation')))
    rows=[];diagnostics=[];consistency_views={};metric_source=load_metrics(raw.ref.UP)
    with gpu_session({**config,'workspace':str(REPORT)}):
        raw.ref.seed();raw.ref.native_imports();raw.install_renderer()
        from gaussian_renderer import render
        models=raw.ref.native_models(prepared,30000)
        codec=raw.ref.upstream_ae();codec.load_state_dict(torch.load(raw.WORK/'autoencoder/best_ckpt.pth',weights_only=True));codec.eval()
        clip=raw.ref.reference_clip(config)
        pipe=SimpleNamespace(debug=False,convert_SHs_python=False,compute_cov3D_python=False);opt=SimpleNamespace(include_feature=True)
        with torch.inference_mode():
            for n,image_id in enumerate(ids):
                f=frames[image_id];stem=Path(f['name']).stem;gt=annotations(ann[image_id]['annotation']) if image_id in ann else {}
                names=list(gt) if gt else EXTRA[image_id];clip.set_positives(names)
                feature_path=raw.WORK/'language_features'/f'{stem}_f.npy';label_path=raw.WORK/'language_features'/f'{stem}_s.npy'
                features=torch.as_tensor(np.load(feature_path),device='cuda');labels=np.load(label_path)
                if labels.shape!=(4,f['height'],f['width']) or features.shape[1]!=512:raise ValueError('Feature/label/calibration grid mismatch')
                if labels.max()>=len(features) or labels.min()<-1 or not torch.isfinite(features).all():raise ValueError('Invalid region inputs')
                codes=codec.encode(features);restored=codec.decode(codes)
                saved=np.load(raw.WORK/'language_features_dim3'/f'{stem}_f.npy')
                np.testing.assert_allclose(codes.cpu().numpy(),saved,atol=2e-5,rtol=2e-5)
                raw_scores=query_scores(clip,features,names);rec_scores=query_scores(clip,restored,names)
                maps={s:[] for s in STAGES};residual=[]
                for level,model in enumerate(models,1):
                    grid=labels[level];valid=grid>=0
                    maps['teacher'].append(scatter(raw_scores,grid));maps['reconstructed'].append(scatter(rec_scores,grid))
                    latent=render(raw.ref.native_camera(f),model,pipe,torch.ones(3,device='cuda'),opt)['language_feature_image']
                    flat=latent.permute(1,2,0).reshape(-1,3)
                    scores=[query_scores(clip,codec.decode(chunk),names) for chunk in flat.split(8192)]
                    maps['field'].append(np.concatenate(scores,1).reshape(len(names),f['height'],f['width']))
                    averages,counts=pool_region_codes(latent.cpu().numpy(),grid,len(features))
                    pooled_scores=query_scores(clip,codec.decode(torch.as_tensor(averages,device='cuda')),names)
                    maps['region_pooled'].append(scatter(pooled_scores,grid))
                    index=torch.as_tensor(grid[valid],device='cuda').long();target=codes[index];actual=latent[:,torch.as_tensor(valid,device='cuda')].T
                    code_l1=float((actual-target).abs().mean());code_cos=float(torch.nn.functional.cosine_similarity(actual,target).mean())
                    residual.append(dict(level=level,latent_l1=code_l1,latent_cosine=code_cos,
                        raw_feature_norm_mean=float(features.norm(dim=-1).mean()),reconstruction_cosine=float(torch.nn.functional.cosine_similarity(restored,features).mean()),
                        valid_fraction=float(valid.mean())))
                maps={s:np.stack(v) for s,v in maps.items()}
                if not all(np.isfinite(v).all() for v in maps.values()):raise ValueError('Non-finite maps')
                np.savez_compressed(REPORT/f'{image_id:06d}-maps.npz',names=np.array(names),**maps)
                for stage in STAGES:
                    if gt:
                        metrics,_=evaluate_maps(raw.ref.UP,maps[stage],names,ann[image_id]['annotation'],source=metric_source)
                        rows.append(dict(image_id=image_id,stage=stage,**metrics))
                view_record=dict(image_id=image_id,split='dev' if gt else 'train',residual=residual,
                    feature_sha256=sha256(feature_path),labels_sha256=sha256(label_path))
                write_json(REPORT/f'{image_id:06d}-features.json',view_record)
                photo=cv2.imread(f['path']);stage_masks={}
                for name in FOCUS[image_id]:
                    q=names.index(name);truth=gt.get(name,{}).get('mask');teacher_head,_=query_mask(maps['teacher'][:,q])
                    result=dict(image_id=image_id,query=name,annotated=truth is not None,stages={},teacher_head=teacher_head+1,
                        oracle_sam=None if truth is None else [dict(level=l,**sam_union_oracle(labels[l],truth)) for l in (1,2,3)])
                    tiles=[];stage_masks[name]={}
                    for stage in STAGES:
                        head,mask=query_mask(maps[stage][:,q]);_,fixed=query_mask(maps[stage][teacher_head:teacher_head+1,q])
                        stage_masks[name][stage]=mask
                        item=dict(level=head+1,prediction_pixels=int(mask.sum()),peak=float(maps[stage][:,q].max()))
                        if truth is not None:
                            item.update(mask_metrics(mask,truth));item['fixed_teacher_head']=mask_metrics(fixed,truth)
                            item['per_level']=[dict(level=l+1,**mask_metrics(query_mask(maps[stage][l:l+1,q])[1],truth)) for l in range(3)]
                        result['stages'][stage]=item
                        tile=cv2.resize(overlay(photo,mask,truth),(394,290));caption=np.zeros((48,394,3),np.uint8)
                        cv2.putText(caption,f'{stage} | L{head+1}',(7,19),cv2.FONT_HERSHEY_SIMPLEX,.46,(230,235,230),1)
                        label=f'IoU {item["iou"]:.3f} | precision {item["precision"]:.3f}' if truth is not None else 'No manual GT; inspect consistency only'
                        cv2.putText(caption,label,(7,40),cv2.FONT_HERSHEY_SIMPLEX,.42,(175,210,185),1)
                        tiles.append(np.concatenate([tile,caption]))
                        cv2.imwrite(str(REPORT/f'{image_id:06d}-{name.replace(" ","_")}-{stage}.jpg'),overlay(photo,mask,truth))
                    montage=np.concatenate([np.concatenate(tiles[:2],1),np.concatenate(tiles[2:],1)],0)
                    filename=f'{image_id:06d}-{name.replace(" ","_")}-comparison.jpg'
                    cv2.imwrite(str(REPORT/filename),montage);result['comparison_url']='/api/semantic/stage-diagnostics/artifacts/'+filename
                    diagnostics.append(result)
                # Use the same frozen RGB expected-depth convention as prior multiview diagnostics.
                model=models[0];saved_feature=model._language_feature;model._language_feature=torch.ones_like(saved_feature)
                try:
                    z=(model.get_xyz@torch.tensor(f['R'],device='cuda').T+torch.tensor(f['t'],device='cuda'))[:,2]
                    out=render(raw.ref.native_camera(f),model,pipe,torch.zeros(3,device='cuda'),opt,override_color=z[:,None].expand(-1,3).contiguous())
                finally:model._language_feature=saved_feature
                alpha=out['language_feature_image'][0].cpu().numpy();depth=out['render'][0].cpu().numpy()/np.maximum(alpha,1e-8)
                consistency_views[image_id]=(f,depth,alpha,stage_masks)
                write_json(REPORT/'progress.json',dict(status='running',completed=n+1,total=len(ids),image_id=image_id,seconds=time.monotonic()-started))
                print('completed view',image_id,flush=True)
    crossview=[]
    for name,anchor,neighbors in [('knife',62,[63,94]),('dark cup',124,[118,136]),('sink',124,[126])]:
        a=consistency_views[anchor]
        for i in neighbors:
            b=consistency_views[i]
            for stage in STAGES:
                crossview.append(dict(query=name,anchor_id=anchor,image_id=i,stage=stage,
                    **reproject(a[:3],b[:3],a[3][name][stage],b[3][name][stage],prepared['normalization']['scale'])))
    for p in sources:
        if sha256(p)!=protected[str(p.relative_to(ROOT))]:raise ValueError('Protected source changed during diagnosis')
    gt_knife=annotations(ann[62]['annotation'])['knife']['mask']
    source=consistency_views[62]
    visibility=[dict(anchor_id=62,image_id=i,**projected_support(frames[62],frames[i],source[1],source[2],gt_knife)) for i in (63,94)]
    write_json(REPORT/'visibility.json',visibility)
    summary={s:{protocol:float(np.mean([r[protocol]['mean_iou'] for r in rows if r['stage']==s])) for protocol in ['official','common']} for s in STAGES}
    # Weight query counts, rather than treating differently sized Dev views equally.
    for s in STAGES:
        for protocol in ('official','common'):
            subset=[r for r in rows if r['stage']==s];count=sum(len(r['per_query']) for r in subset)
            summary[s][protocol]=sum(r[protocol]['mean_iou']*len(r['per_query']) for r in subset)/count
    write_json(REPORT/'results.json',dict(status='completed',model='raw3',dev_summary=summary,per_view=rows,focus=diagnostics,
        crossview=crossview,protected_files_unchanged=True,seconds=time.monotonic()-started,
        scope='Reused Dev and diagnostic train viewpoints; not independent benchmark or causal isolation of geometry versus training'))
    write_json(REPORT/'progress.json',dict(status='completed',completed=len(ids),total=len(ids),seconds=time.monotonic()-started))
    print(json.dumps(summary,indent=2))


if __name__=='__main__':main()
