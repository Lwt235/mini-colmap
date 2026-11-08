"""Controlled AE experiments on the completed, fixed LangSplat regional features.

Only dev labels rank the predeclared variants; test is opened after selection.
Nothing writes to the learner, reference codec, or frozen RGB directories.
"""
from __future__ import annotations
import argparse
from contextlib import redirect_stdout
import csv
import io
import json
import os
from pathlib import Path
import time

os.environ.setdefault('CUDA_VISIBLE_DEVICES', '7')
os.environ.setdefault('OMP_NUM_THREADS', '4')
os.environ.setdefault('OPENBLAS_NUM_THREADS', '4')
import numpy as np
import torch
from torch.nn import functional as F
import langsplat_reference as ref
from langsplat_reference_metrics import evaluate_maps, query_mask, annotations, load_metrics
from minicolmap.semantics.project import load_config, project, sha256, write_json
from minicolmap.semantics.encoders import gpu_session

ROOT = ref.ROOT
WORK = ROOT/'data/semantic_codec_study'
REPORT = ROOT/'reports/semantic-codec-study-20261021'
FEATURES = ROOT/'data/langsplat_reference/language_features'
VARIANTS = [
    dict(name='stock3',dim=3,axis=0,weight=.001,latent_norm=True,output_norm=True,parent=None,factor='controlled baseline'),
    dict(name='row_cos001',dim=3,axis=-1,weight=.001,latent_norm=True,output_norm=True,parent='stock3',factor='cosine axis only'),
    dict(name='row_cos1',dim=3,axis=-1,weight=1.,latent_norm=True,output_norm=True,parent='row_cos001',factor='cosine weight only'),
    dict(name='no_latent_norm',dim=3,axis=-1,weight=1.,latent_norm=False,output_norm=True,parent='row_cos1',factor='latent normalization only'),
    dict(name='no_output_norm',dim=3,axis=-1,weight=1.,latent_norm=True,output_norm=False,parent='row_cos1',factor='output normalization during AE fit only; unit query calibration'),
    dict(name='row_dim8',dim=8,axis=-1,weight=1.,latent_norm=True,output_norm=True,parent='row_cos1',factor='latent capacity only'),
    dict(name='row_dim16',dim=16,axis=-1,weight=1.,latent_norm=True,output_norm=True,parent='row_cos1',factor='latent capacity only'),
    dict(name='stock_dim16',dim=16,axis=0,weight=.001,latent_norm=True,output_norm=True,parent='stock3',factor='latent capacity under stock loss'),
]


def codec(spec):
    source = ref.load_file('codec_study_upstream',ref.UP/'autoencoder/model.py')
    class Model(source.Autoencoder):
        def encode(self,x):
            for layer in self.encoder:
                x = layer(x)
            return x/x.norm(dim=-1,keepdim=True) if spec['latent_norm'] else x
        def decode(self,x):
            for layer in self.decoder:
                x = layer(x)
            return x/x.norm(dim=-1,keepdim=True) if spec['output_norm'] else x
        def forward(self,x):
            return self.decode(self.encode(x))
    with redirect_stdout(io.StringIO()):
        return Model([256,128,64,32,spec['dim']],[16,32,64,128,256,256,512])


def objective(pred,target,spec):
    return (pred-target).square().mean()+spec['weight']*(1-F.cosine_similarity(pred,target,dim=spec['axis']).mean())


def event(**kwargs):
    write_json(REPORT/'progress.json',kwargs)
    print(json.dumps(kwargs),flush=True)


def fit(spec,values):
    directory = WORK/spec['name']
    directory.mkdir(parents=True,exist_ok=True)
    marker = directory/'completed.json'
    if marker.exists():
        record = json.loads(marker.read_text())
        if record['spec']!=spec or record['checkpoint_sha256']!=sha256(directory/'best.pth'):
            raise ValueError('Study checkpoint identity changed')
        return
    if (directory/'best.pth').exists():
        raise ValueError('Partial study checkpoint exists; use an explicit recovery directory')
    ref.seed(0)
    model = codec(spec).cuda()
    optimizer = torch.optim.Adam(model.parameters(),lr=.0007)
    # Separate RNG: changing the architecture does not change the batch order.
    generator = torch.Generator(device='cuda').manual_seed(0)
    best,history,start = float('inf'),[],time.monotonic()
    for epoch in range(100):
        model.train()
        order = torch.randperm(len(values),generator=generator,device='cuda')
        losses = []
        for indices in order.split(64):
            target = values[indices]
            prediction = model(target)
            loss = objective(prediction,target,spec)
            if not torch.isfinite(loss):
                raise ValueError(f'Nonfinite loss: {spec["name"]}, epoch {epoch}')
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            optimizer.step()
            losses.append(loss.detach())
        row = dict(epoch=epoch,loss=float(torch.stack(losses).mean()))
        if epoch>95:
            model.eval()
            total = torch.zeros((),device='cuda')
            with torch.inference_mode():
                for target in values.split(256):
                    prediction = model(target)
                    # Identical train-only checkpoint criterion for every group.
                    score=(prediction-target).square().mean()+1-F.cosine_similarity(prediction,target,dim=0).mean()
                    total += score*len(target)
            row['selection_score']=float(total/len(values))
            if row['selection_score']<best:
                best=row['selection_score']
                torch.save(model.state_dict(),directory/'best.pth')
                write_json(directory/'selection.json',row)
        history.append(row)
        write_json(directory/'history.json',history)
        event(stage='fit',variant=spec['name'],epoch=epoch+1,epochs=100,loss=row['loss'],seconds=time.monotonic()-start)
    write_json(marker,dict(spec=spec,epochs=100,seed=0,batch_size=64,training_regions=len(values),
        seconds=time.monotonic()-start,checkpoint_sha256=sha256(directory/'best.pth'),
        checkpoint_selection='same stock train-only MSE+cos(dim=0), epochs96..99',
        shared_batch_rng='independent CUDA Generator seed0',parameters=sum(p.numel() for p in model.parameters())))


def evaluate(spec,prepared,clip,split,*,model_path=None,tag=None,save_images=False):
    import cv2
    model=codec(spec).cuda()
    model.load_state_dict(torch.load(model_path or WORK/spec['name']/'best.pth',map_location='cuda',weights_only=True))
    model.eval()
    frames={f['image_id']:f for f in prepared['frames']}
    records={a['image_id']:a for a in prepared['annotations']}
    directory=REPORT/(tag or spec['name'])/split
    directory.mkdir(parents=True,exist_ok=True)
    rows=[]
    region_totals=dict(regions=0,squared_error=0.,cosine_sum=0.,output_norm_sum=0.,latent_norm_sum=0.)
    source=load_metrics(ref.UP)
    with torch.inference_mode():
        for image_id in prepared['splits'][split]:
            frame,ann=frames[image_id],records[image_id]
            names=list(annotations(ann['annotation']))
            clip.set_positives(names)
            stem=Path(frame['name']).stem
            features=torch.from_numpy(np.load(FEATURES/f'{stem}_f.npy')).cuda()
            labels=np.load(FEATURES/f'{stem}_s.npy')
            latent=model.encode(features)
            prediction=model.decode(latent)
            calibrated=prediction if spec['output_norm'] else F.normalize(prediction,dim=-1)
            # Reconstruction statistics cover only regions used by s/m/l maps.
            used=np.unique(labels[1:][labels[1:]>=0])
            index=torch.from_numpy(used).cuda().long()
            target,pred=features[index],prediction[index]
            region_totals['regions']+=len(used)
            region_totals['squared_error']+=float((pred-target).square().sum())
            region_totals['cosine_sum']+=float(F.cosine_similarity(pred,target,dim=-1).sum())
            region_totals['output_norm_sum']+=float(pred.norm(dim=-1).sum())
            region_totals['latent_norm_sum']+=float(latent[index].norm(dim=-1).sum())
            scores=np.stack([clip.get_relevancy(calibrated,i)[:,0].cpu().numpy() for i in range(len(names))])
            heatmaps=[]
            for level in (1,2,3):
                valid=labels[level]>=0
                heat=np.zeros((len(names),*labels.shape[1:]),np.float32)
                heat[:,valid]=scores[:,labels[level][valid]]
                heatmaps.append(heat)
            maps=np.stack(heatmaps)
            metrics,masks=evaluate_maps(ref.UP,maps,names,ann['annotation'],source=source)
            rows.append(dict(image_id=image_id,**metrics))
            np.savez_compressed(directory/f'{image_id:06d}-maps.npz',maps=maps,names=np.array(names))
            if save_images:
                rgb=cv2.imread(frame['path'])
                for item in metrics['per_query']:
                    name=item['query'];level=item['official_level']-1
                    heat=maps[level,names.index(name)]
                    overlay=cv2.addWeighted(rgb,.55,cv2.applyColorMap((heat.clip(0,1)*255).astype(np.uint8),cv2.COLORMAP_TURBO),.45,0)
                    safe=''.join(c if c.isalnum() or c in '_-' else '_' for c in name)
                    cv2.imwrite(str(directory/f'{image_id:06d}-{safe}-heatmap.png'),overlay)
                    cv2.imwrite(str(directory/f'{image_id:06d}-{safe}-mask.png'),masks[name]*255)
            write_json(directory/f'{image_id:06d}.json',rows[-1])
    n=sum(len(r['per_query']) for r in rows)
    results={p:{k:sum(r[p][k]*len(r['per_query']) for r in rows)/n for k in
                ('mean_iou','bbox_localization_rate' if p=='official' else 'polygon_peak_rate')} for p in ('common','official')}
    count=region_totals['regions']
    reconstruction=dict(regions=count,mse=region_totals['squared_error']/(count*512),
        mean_cosine=region_totals['cosine_sum']/count,output_norm=region_totals['output_norm_sum']/count,
        latent_norm=region_totals['latent_norm_sum']/count)
    summary=dict(variant=tag or spec['name'],spec=spec,split=split,results=results,reconstruction=reconstruction,
        per_view=rows,queries=n,scope='2D codec diagnostics on identical fixed s/m/l maps, NOT a trained 3D field',
        query_calibration='unit decoder output; output-norm-off group explicitly normalized only for querying')
    write_json(directory/'summary.json',summary)
    event(stage='evaluate',variant=summary['variant'],split=split,results=results,reconstruction=reconstruction)
    return summary


def preflight(values):
    ref.seed(0)
    spec=VARIANTS[0]
    model=codec(spec).cuda()
    with redirect_stdout(io.StringIO()):
        original=ref.upstream_ae()
    original.load_state_dict(model.state_dict())
    batch=values[:32].clone()
    model.eval();original.eval()
    torch.testing.assert_close(model(batch),original(batch),atol=0,rtol=0)
    model.train()
    optimizer=torch.optim.Adam(model.parameters(),lr=.0007)
    history=[]
    for i in range(200):
        prediction=model(batch)
        loss=objective(prediction,batch,spec)
        optimizer.zero_grad(set_to_none=True)
        loss.backward()
        if i==0:
            gradient_ok=all(p.grad is not None and torch.isfinite(p.grad).all().item() for p in model.parameters())
        optimizer.step()
        if i in (0,199):history.append(float(loss.detach()))
    if not gradient_ok or not history[-1]<history[0]:
        raise ValueError('One-batch learning/gradient smoke failed')
    with torch.inference_mode():
        unit=F.normalize(batch,dim=-1)
        pred=F.normalize(torch.roll(unit,1,0),dim=-1)
        mse=float((pred-unit).square().mean())
        cosine=float(1-F.cosine_similarity(pred,unit,dim=-1).mean())
        centered=values-values.mean(0)
        eigenvalues=torch.linalg.eigvalsh(centered.T@centered).clamp_min(0).flip(0)
        variance={str(d):float(eigenvalues[:d].sum()/eigenvalues.sum()) for d in (3,8,16,32,224)}
    write_json(REPORT/'preflight.json',dict(source_forward_exact=True,finite_gradients=gradient_ok,
        one_batch_loss=history,input_shape=list(values.shape),input_norm_min=float(values.norm(dim=-1).min()),
        input_norm_max=float(values.norm(dim=-1).max()),unit_vector_mse=mse,
        unit_vector_row_cosine=cosine,mse_cosine_relation_error=abs(mse-2*cosine/512),
        linear_pca_explained_variance=variance,
        caveat='PCA variance is a linear diagnostic, not a lower bound on nonlinear AE quality'))


def report():
    rows=[];queries=[]
    for folder in sorted(REPORT.iterdir()):
        if not folder.is_dir():continue
        for split in ('dev','test'):
            path=folder/split/'summary.json'
            if not path.exists():continue
            s=json.loads(path.read_text())
            rows.append(dict(variant=s['variant'],split=split,dim=s['spec']['dim'],
                common_iou=s['results']['common']['mean_iou'],official_iou=s['results']['official']['mean_iou'],
                mean_cosine=s['reconstruction']['mean_cosine'],mse=s['reconstruction']['mse']))
            for view in s['per_view']:
                for q in view['per_query']:
                    queries.append(dict(variant=s['variant'],split=split,image_id=view['image_id'],query=q['query'],
                        common_iou=q['common']['iou'],official_iou=q['official_iou']))
    for name,values in [('comparison',rows),('per-query',queries)]:
        if values:
            with (REPORT/f'{name}.csv').open('w') as f:
                w=csv.DictWriter(f,fieldnames=list(values[0]));w.writeheader();w.writerows(values)
    lines=['# Waldo 压缩器单因素诊断','','所有数值是二维SAM区域特征经压缩重建后的查询结果，不是三维场成绩。',
        '固定173张训练图、同一39184个四层训练区域、s/m/l评测图、RGB、CLIP、100轮、batch64、lr0.0007、seed0。',
        '每组独立CUDA Generator固定批次顺序；不同维度会改变参数形状和初始化抽样，单种子不提供统计显著性结论。',
        '所有组按同一个训练集重建指标在96..99轮选检查点；只按dev官方IoU选候选，再评测test。',
        '所有组保持单位查询特征；output-norm-off只在AE训练时移除输出单位化，在查询时显式补回。','',
        '| 方案 | 划分 | 维数 | 原口径IoU | 官方口径IoU | 区域余弦 | MSE |','|---|---|---:|---:|---:|---:|---:|']
    for r in rows:
        lines.append(f"| {r['variant']} | {r['split']} | {r['dim']} | {r['common_iou']:.2%} | {r['official_iou']:.2%} | {r['mean_cosine']:.4f} | {r['mse']:.6f} |")
    lines+=['','## 单因素关系','']
    lines += [f"- {s['name']} 相对于 {s['parent'] or '已有官方参考'}：{s['factor']}。" for s in VARIANTS]
    selection=REPORT/'selection.json'
    if selection.exists():
        chosen=json.loads(selection.read_text())
        lines+=['','## 锁定候选','',f"开发集选择：总体候选 {chosen['best_overall']}；保持官方三维表示约束的候选 {chosen['best_native3']}。",
            '8/16维编码不能直接放进现有固定3通道原生渲染器；无单位化3维码也改变了三维表示约束。',
            '替换编码器会改变潜空间坐标，旧三维语言场不能与新解码器混用。当前不覆盖任何已训练三维模型。']
    lines+=['','## 解释限制','',
        '单位预测与单位目标下，MSE=2/512×逐区域余弦损失；只调逐区域余弦权重很大程度是在改变整体损失尺度，Adam对此近似不敏感。真实FP16特征范数有微小偏差。',
        'dim=0余弦沿批次比较每个特征通道，和dim=-1逐区域余弦并不等价。源码采用前者，本研究将后者视为工程变体，而非冒充源码。',
        '原已发布官方参考使用全局RNG；本研究独立固定批次RNG。stock3与existing_reference的差值体现重训/随机顺序敏感性，不应归因于本轮因素。',
        '同一原生表示只看3维且编码/解码均单位化的候选；整体最优与可直接接入原生三维的最优分开记录。',
        '训练原图、区域及RGB均固定，未用dev/test掩码拟合。region余弦与查询IoU均报告，不能只凭重建误差判断检索质量。']
    (REPORT/'README.md').write_text('\n'.join(lines)+'\n')


def run():
    if (REPORT/'completed.json').exists():
        report();return
    config=load_config(ROOT/'configs/semantic_waldo.json')
    prepared=project(config)
    frames={f['image_id']:f for f in prepared['frames']}
    hashes={str(p):sha256(p) for p in (ROOT/'src/minicolmap/semantics/algorithms.py',
        ROOT/config['rgb_run']/'checkpoint.pt',ROOT/'data/langsplat_reference/autoencoder/best_ckpt.pth')}
    rows=[]
    for i in sorted(prepared['splits']['train'],key=lambda i:frames[i]['name']):
        path=FEATURES/f"{Path(frames[i]['name']).stem}_f.npy"
        rows.append(np.load(path))
    values=torch.from_numpy(np.concatenate(rows)).cuda();del rows
    feature_hashes={str(p):sha256(p) for p in sorted(FEATURES.glob('*.npy'))}
    if set(prepared['splits']['train']) & set(prepared['splits']['dev']+prepared['splits']['test']):
        raise ValueError('Language split leakage')
    write_json(REPORT/'manifest.json',dict(variants=VARIANTS,seed=0,gpu=7,train_ids=prepared['splits']['train'],
        dev_ids=prepared['splits']['dev'],test_ids=prepared['splits']['test'],regions=len(values),
        preservation_hashes=hashes,feature_hashes=feature_hashes,primary_selection_metric='dev official.mean_iou',
        source_ae_sha256=sha256(ref.UP/'autoencoder/model.py'),script_sha256=sha256(Path(__file__))))
    preflight(values)
    clip=ref.reference_clip(config)
    evaluate(VARIANTS[0],prepared,clip,'dev',model_path=ROOT/'data/langsplat_reference/autoencoder/best_ckpt.pth',tag='existing_reference')
    for spec in VARIANTS:
        fit(spec,values)
        evaluate(spec,prepared,clip,'dev')
        report()
    summaries={s['name']:json.loads((REPORT/s['name']/'dev/summary.json').read_text()) for s in VARIANTS}
    rank=lambda s:summaries[s['name']]['results']['official']['mean_iou']
    best=max(VARIANTS,key=rank)
    native=max((s for s in VARIANTS if s['dim']==3 and s['latent_norm'] and s['output_norm']),key=rank)
    selection=dict(best_overall=best['name'],best_native3=native['name'],
        primary_metric='dev official.mean_iou',test_not_used_for_selection=True,
        note='codec-only candidates, not interchangeable with existing 3D language fields')
    write_json(REPORT/'selection.json',selection)
    # Lock selection before opening test for any candidate.
    for name in dict.fromkeys(['stock3',native['name'],best['name']]):
        spec=next(s for s in VARIANTS if s['name']==name)
        evaluate(spec,prepared,clip,'dev',save_images=True)
        evaluate(spec,prepared,clip,'test',save_images=True)
    if any(sha256(Path(p))!=value for p,value in hashes.items()):
        raise ValueError('An original learner/RGB/reference codec artifact changed')
    if any(sha256(Path(p))!=value for p,value in feature_hashes.items()):
        raise ValueError('Fixed input features or masks changed during the study')
    write_json(REPORT/'completed.json',dict(status='completed',variants=len(VARIANTS),gpu=7,
        all_epochs=100,original_artifacts_unchanged=True,selection=selection))
    report()
    event(stage='completed',**selection)


def query(variant,image_id,text):
    import cv2
    config=load_config(ROOT/'configs/semantic_waldo.json');prepared=project(config)
    spec=next(s for s in VARIANTS if s['name']==variant)
    frame=next(f for f in prepared['frames'] if f['image_id']==image_id)
    model=codec(spec).cuda()
    model.load_state_dict(torch.load(WORK/variant/'best.pth',map_location='cuda',weights_only=True));model.eval()
    clip=ref.reference_clip(config);clip.set_positives([text])
    stem=Path(frame['name']).stem
    with torch.inference_mode():
        features=torch.from_numpy(np.load(FEATURES/f'{stem}_f.npy')).cuda()
        prediction=model(features)
        if not spec['output_norm']:prediction=F.normalize(prediction,dim=-1)
        scores=clip.get_relevancy(prediction,0)[:,0].cpu().numpy()
    labels=np.load(FEATURES/f'{stem}_s.npy');maps=[]
    for level in (1,2,3):
        valid=labels[level]>=0;heat=np.zeros(labels.shape[1:],np.float32)
        heat[valid]=scores[labels[level][valid]];maps.append(heat)
    maps=np.stack(maps);level,mask=query_mask(maps)
    directory=REPORT/variant/'queries'/str(time.time_ns());directory.mkdir(parents=True)
    rgb=cv2.imread(frame['path'])
    cv2.imwrite(str(directory/'heatmap.png'),cv2.addWeighted(rgb,.55,cv2.applyColorMap((maps[level].clip(0,1)*255).astype(np.uint8),cv2.COLORMAP_TURBO),.45,0))
    cv2.imwrite(str(directory/'mask.png'),mask*255)
    result=dict(variant=variant,image_id=image_id,text=text,level=level+1,selected_pixels=int(mask.sum()),
        directory=str(directory),scope='2D SAM region reconstruction; NOT rendered 3D semantic field')
    write_json(directory/'query.json',result);print(json.dumps(result,indent=2))


def main():
    parser=argparse.ArgumentParser(__doc__)
    parser.add_argument('stage',choices=('run','report','query'),default='run',nargs='?')
    parser.add_argument('--variant',choices=[s['name'] for s in VARIANTS],default='row_cos1')
    parser.add_argument('--image-id',type=int,default=124)
    parser.add_argument('--text',default='dark cup')
    args=parser.parse_args();REPORT.mkdir(parents=True,exist_ok=True);WORK.mkdir(parents=True,exist_ok=True)
    if args.stage=='report':report();return
    config=load_config(ROOT/'configs/semantic_waldo.json')
    with gpu_session({**config,'workspace':str(REPORT)}):
        ref.seed(0)
        if args.stage=='run':run()
        else:query(args.variant,args.image_id,args.text)


if __name__=='__main__':main()
