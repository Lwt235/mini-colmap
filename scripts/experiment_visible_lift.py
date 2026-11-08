"""Fixed small-budget teacher-assisted lifting experiment, no manual labels in fit."""
import json,time,uuid,traceback
from pathlib import Path
from refine_visible_selection import VisiblePipeline,InsufficientEvidence,ROOT
from minicolmap.semantics.project import write_json
from minicolmap.semantics.selection_policy import source_identity
from minicolmap.semantics.selection import selection_directory
from minicolmap.semantics.visible_lift import teacher_identity,visible_selection_key,VISIBLE_POLICY


def main():
    report=ROOT/'reports/portfolio-release-20261022';report.mkdir(parents=True,exist_ok=True)
    rows=[]
    for text,anchor in [('dark cup',118),('knife',62),('sink',126)]:
        source=source_identity(ROOT);teacher=teacher_identity(ROOT);sid=visible_selection_key(text,anchor,source,teacher)
        directory=selection_directory(ROOT,sid);directory.mkdir(parents=True,exist_ok=True)
        job_id=uuid.uuid4().hex;path=ROOT/f'.cache/object-selection-jobs/{job_id}.json'
        request=dict(model='raw3',text=text,image_id=anchor)
        value=dict(id=job_id,selection_id=sid,status='running',request=request,pid=__import__('os').getpid(),
            report_url=f'/api/demo/selections/{sid}/report')
        if (directory/'validation.json').exists():
            saved=json.loads((directory/'validation.json').read_text());rows.append(dict(text=text,anchor=anchor,selection_id=sid,status='completed' if saved['evidence']['accepted'] else 'rejected',cached=True));continue
        write_json(path,value);write_json(directory/'request.json',dict(**request,source_identity=source,teacher_identity=teacher,
            supervision='teacher-visible-v1',supervision_policy=VISIBLE_POLICY,selection_id=sid,hint_bounds=None))
        worker=VisiblePipeline(path,value);worker.progress('starting',0,'正在验证二维校验增强选区')
        try:worker.run()
        except InsufficientEvidence as exc:
            (directory/'README.md').write_text(f'# 证据不足\n\n{exc}\n')
            worker.progress('finished',1,str(exc),status='rejected')
        except Exception:
            traceback.print_exc();worker.progress('failed',0,'实验失败，日志已保留',status='failed')
        rows.append(dict(text=text,anchor=anchor,selection_id=sid,status=worker.value['status'],message=worker.value.get('message'),
            selected_gaussians=worker.value.get('selected_gaussians'),seconds=worker.value.get('seconds'),evidence=worker.value.get('evidence')))
        write_json(report/'visible-lift-experiment.json',rows)
        print(json.dumps(rows[-1],ensure_ascii=False),flush=True)
    write_json(report/'visible-lift-experiment.json',rows)


if __name__=='__main__':main()
