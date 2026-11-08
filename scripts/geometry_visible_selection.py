"""Visibility filtering for frozen 3D-field lifting; no teacher fallback or labels."""
import numpy as np
from generate_object_selection import Pipeline,InsufficientEvidence
from minicolmap.semantics.visible_lift import surface_points,in_frame_fraction
from minicolmap.semantics.project import write_json

GEOMETRY_POLICY=dict(version='field-visible-v1',min_in_frame=.3,max_angle_degrees=40)


class GeometryPipeline(Pipeline):
    def prepare_fit_views(self,prepared,frames,anchor,ids,models,codec,clip):
        self.render_views([anchor],frames,models,codec,clip,.02,.05)
        a=self.views[anchor];points=surface_points(frames[anchor],a['depth'],a['alpha'],a['mask'])
        if len(points)<100:raise InsufficientEvidence('锚点缺少足够可见目标表面')
        R=np.asarray(frames[anchor]['R']);C=-R.T@np.asarray(frames[anchor]['t'])
        ranked=[];audit=[]
        for i in prepared['splits']['train']:
            if i==anchor:continue
            f=frames[i];r=np.asarray(f['R']);c=-r.T@np.asarray(f['t'])
            angle=float(np.arccos(np.clip(R[2]@r[2],-1,1)))
            fraction=in_frame_fraction(points,f)
            accepted=fraction>=GEOMETRY_POLICY['min_in_frame'] and angle<=np.deg2rad(GEOMETRY_POLICY['max_angle_degrees'])
            audit.append(dict(image_id=i,in_frame_fraction=fraction,eligible=bool(accepted)))
            if accepted:ranked.append((np.linalg.norm(c-C)/prepared['normalization']['scale']+.2*angle,i))
        eligible={i for _,i in ranked}
        chosen=[i for i in ids if i!=anchor and i in eligible]
        for _,i in sorted(ranked):
            if i not in chosen:chosen.append(i)
        selected=[anchor,*chosen[:5]]
        write_json(self.directory/'visible-view-audit.json',dict(policy=GEOMETRY_POLICY,anchor=anchor,original_fit_ids=ids,fit_ids=selected,views=audit,
            scope='Anchor query surface projected with frozen RGB expected depth; in-frame test, not target occlusion proof'))
        if len(selected)!=6:raise InsufficientEvidence('目标有足够入画表面的拟合视角不足六个')
        return selected

    def report(self,report):
        super().report(report)
        path=self.directory/'README.md'
        path.write_text(path.read_text()+'\n## 几何入画检查\n\n拟合前，将锚点查询表面投影到训练相机，要求至少 30% 表面样本落在画内；不读取人工标签。入画不等于未被遮挡，最终仍使用相同五视角发布门槛。此版本仍使用三维语言场监督，未接入未通过实验的二维教师增强。\n')
