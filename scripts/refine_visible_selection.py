"""Independent membership fit from frozen 2D teacher masks in visible cameras.

Uses no manual labels. Original RGB/codec/3D language checkpoints are untouched.
This is teacher-assisted lifting, not an improvement to the trained language field.
"""
import json
import numpy as np
import cv2
import torch
from generate_object_selection import Pipeline,InsufficientEvidence,ROOT,PIPE,OPT
from langsplat_reference_metrics import query_mask
from check_semantic_multiview import reproject
from minicolmap.semantics.project import write_json,sha256
from minicolmap.semantics.visible_lift import VISIBLE_POLICY,teacher_identity,surface_points,in_frame_fraction


class VisiblePipeline(Pipeline):
    def render_views(self,ids,frames,models,codec,clip,start,end):
        from gaussian_renderer import render
        import langsplat_raw3 as raw
        with torch.inference_mode():
            for n,i in enumerate(ids):
                if i not in self.views:
                    f=frames[i];stem=__import__('pathlib').Path(f['name']).stem
                    folder=ROOT/'data/langsplat_reference/language_features'
                    feature_path,label_path=folder/f'{stem}_f.npy',folder/f'{stem}_s.npy'
                    features=torch.as_tensor(np.load(feature_path),device='cuda');labels=np.load(label_path)
                    scores=clip.get_relevancy(features,0)[:,0].float().cpu().numpy()
                    maps=[]
                    for level in (1,2,3):
                        grid=labels[level];valid=grid>=0;heat=np.zeros(grid.shape,np.float32)
                        heat[valid]=scores[grid[valid]];maps.append(heat)
                    maps=np.stack(maps);level,mask=query_mask(maps)
                    model=models[0];saved=model._language_feature;model._language_feature=torch.ones_like(saved)
                    try:
                        z=(model.get_xyz@torch.tensor(f['R'],device='cuda').T+torch.tensor(f['t'],device='cuda'))[:,2]
                        out=render(raw.ref.native_camera(f),model,PIPE,torch.zeros(3,device='cuda'),OPT,override_color=z[:,None].expand(-1,3).contiguous())
                    finally:model._language_feature=saved
                    alpha=out['language_feature_image'][0].cpu().numpy();depth=out['render'][0].cpu().numpy()/np.maximum(alpha,1e-8)
                    self.views[i]=dict(mask=mask,depth=depth,alpha=alpha,level=level+1)
                    np.savez_compressed(self.directory/f'view-{i:06d}.npz',mask=mask,depth=depth,alpha=alpha,maps=maps)
                    if not hasattr(self,'teacher_hashes'):self.teacher_hashes={}
                    for path in (feature_path,label_path):self.teacher_hashes[str(path.relative_to(ROOT))]=sha256(path)
                self.progress('teacher_masks',start+(end-start)*(n+1)/max(1,len(ids)),f'正在校验二维区域证据：相机 {i}')

    def prepare_fit_views(self,prepared,frames,anchor,ids,models,codec,clip):
        stored=json.loads((self.directory/'request.json').read_text())
        if stored.get('teacher_identity')!=teacher_identity(ROOT):raise ValueError('二维特征已更新，请重新生成')
        self.render_views([anchor],frames,models,codec,clip,.02,.04)
        a=self.views[anchor]
        if a['mask'].mean()>VISIBLE_POLICY['max_mask_fraction']:raise InsufficientEvidence('锚点查询扩散到过大区域，不能可靠定位目标')
        points=surface_points(frames[anchor],a['depth'],a['alpha'],a['mask'])
        if len(points)<100:raise InsufficientEvidence('锚点缺少可见表面证据')
        f=frames[anchor];R=np.asarray(f['R']);C=-R.T@np.asarray(f['t']);scale=prepared['normalization']['scale']
        ranked=[];audit=[]
        for i in prepared['splits']['train']:
            if i==anchor:continue
            fraction=in_frame_fraction(points,frames[i])
            if fraction<VISIBLE_POLICY['min_in_frame']:
                audit.append(dict(image_id=i,accepted=False,reason='out_of_frame',in_frame_fraction=fraction));continue
            r=np.asarray(frames[i]['R']);c=-r.T@np.asarray(frames[i]['t'])
            angle=np.arccos(np.clip(R[2]@r[2],-1,1))
            if angle>np.deg2rad(40):continue
            ranked.append((np.linalg.norm(c-C)/scale+.2*angle,i,fraction))
        eligible=[]
        for n,(_,i,fraction) in enumerate(sorted(ranked)[:VISIBLE_POLICY['max_view_candidates']]):
            self.render_views([i],frames,models,codec,clip,.04,.16)
            b=self.views[i];check=reproject((frames[anchor],a['depth'],a['alpha']),(frames[i],b['depth'],b['alpha']),a['mask'],b['mask'],scale)
            passed=(check['reprojection_iou'] is not None and check['reprojection_iou']>=VISIBLE_POLICY['min_reprojection_iou'] and
                    check['foreground_agreement']>=VISIBLE_POLICY['min_foreground_agreement'] and b['mask'].mean()<=VISIBLE_POLICY['max_mask_fraction'])
            audit.append(dict(image_id=i,accepted=bool(passed),in_frame_fraction=fraction,**check))
            if passed:eligible.append(i)
            if len(eligible)>=5:break
        write_json(self.directory/'visible-view-audit.json',dict(policy=VISIBLE_POLICY,anchor=anchor,fit_ids=[anchor,*eligible],views=audit))
        if len(eligible)<5:raise InsufficientEvidence('目标入画且具有一致二维证据的训练视角不足五个')
        return [anchor,*eligible]

    def report(self,report):
        super().report(report)
        path=self.directory/'README.md'
        path.write_text(path.read_text()+'\n## 二维校验增强\n\n此选区使用原始二维 SAM/CLIP 区域特征及几何入画筛选，属于二维教师辅助的三维提升；没有更新原三维语言场。发布门槛保持不变，五个非拟合视角仍是语言自一致性检查，不代表人工准确率。\n')
        write_json(self.directory/'teacher-sources.json',getattr(self,'teacher_hashes',{}))

    def run(self,**kwargs):
        super().run(**kwargs)
        stored=json.loads((self.directory/'request.json').read_text())
        if teacher_identity(ROOT)!=stored['teacher_identity']:raise ValueError('二维特征在生成期间更新')
        for path,digest in getattr(self,'teacher_hashes',{}).items():
            if sha256(ROOT/path)!=digest:raise ValueError('二维特征内容在生成期间更新')
