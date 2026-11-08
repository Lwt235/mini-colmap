"""Curated, source-checked portfolio entry. Never starts a GPU worker."""
from pathlib import Path
from .demo import read
from ..semantics.selection import catalogue as selections


def catalogue(root):
    root=Path(root);manifest=read(root/'configs/showcase.json',{})
    published={s['id']:s for s in selections(root)}
    cards=[]
    for row in manifest.get('selections',[]):
        selection=published.get(row['selection_id'])
        if selection:
            cards.append(dict(**row,selected_gaussians=selection['selected_gaussians'],
                anchor_image_id=selection['anchor_image_id'],preview_url=f"/api/showcase/preview/{row.get('id','case')}",
                source_checked=True))
    return dict(title='Mini-COLMAP',release=manifest.get('release','development'),selections=cards,
        scope='单场景研究型工程项目；语义定位为可见表面原型，非任意物体可靠实例分割。',
        evidence_url='/api/showcase/guide',ready=bool(cards))
