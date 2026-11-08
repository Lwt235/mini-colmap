"""Finite, resumable GPU-7 campaign for the selected raw 3D codec."""
import json
import os
from pathlib import Path
import subprocess
import sys
from minicolmap.semantics.project import write_json

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT/'reports/langsplat-raw3-20261021'


def main():
    REPORT.mkdir(parents=True,exist_ok=True)
    stages = [['encode'],['train','--steps','100']]
    stages += [['train','--level',str(i)] for i in (1,2,3)]
    stages += [['evaluate','--split',split] for split in ('dev','test')]
    stages += [['query','--image-id',str(i),'--text',text] for i,text in ((124,'dark cup'),(62,'knife'),(124,'sink'))]
    for index,stage in enumerate(stages):
        if (REPORT/'cancel').exists():
            write_json(REPORT/'controller-status.json',dict(status='cancelled',stage=stage))
            return
        write_json(REPORT/'controller-status.json',dict(status='running',stage=stage,index=index,total=len(stages),pid=os.getpid()))
        with (REPORT/f'stage-{index:02d}.log').open('ab') as log:
            result = subprocess.run([sys.executable,'-u',str(ROOT/'scripts/langsplat_raw3.py'),*stage],cwd=ROOT,
                env=dict(os.environ,CUDA_VISIBLE_DEVICES='7',OMP_NUM_THREADS='4',OPENBLAS_NUM_THREADS='4'),
                stdout=log,stderr=subprocess.STDOUT)
        if result.returncode:
            write_json(REPORT/'controller-status.json',dict(status='failed',stage=stage,returncode=result.returncode,log=f'stage-{index:02d}.log'))
            return
    comparisons = {}
    for split in ('dev','test'):
        comparisons[split] = {name:json.loads((folder/f'evaluation/{split}-field/summary.json').read_text())['results']
            for name,folder in [('reference',ROOT/'reports/langsplat-reference-20261021'),('raw3',REPORT)]}
    write_json(REPORT/'comparison.json',comparisons)
    write_json(REPORT/'controller-status.json',dict(status='completed',stages=len(stages)))
    write_json(REPORT/'completed.json',dict(gpu=7,levels=3,steps=30000,comparison=comparisons))


if __name__=='__main__':
    main()
