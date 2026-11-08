"""After the matched campaign, quantify the source default all-view protocol."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from minicolmap.semantics.project import write_json

ROOT = Path(__file__).resolve().parents[1]
PARENT = ROOT/'reports/langsplat-reference-20261021'
REPORT = PARENT/'full-view-protocol'


def main():
    REPORT.mkdir(parents=True,exist_ok=True)
    write_json(REPORT/'controller.json',dict(pid=os.getpid()))
    while not (PARENT/'campaign-completed.json').exists():
        if (PARENT/'cancel').exists():
            write_json(REPORT/'controller-status.json',dict(status='cancelled'))
            return
        status = json.loads((PARENT/'controller-status.json').read_text())
        if status['status']=='failed':
            write_json(REPORT/'controller-status.json',dict(status='waiting_for_matched_failure_resolution',failure=status))
        else:
            write_json(REPORT/'controller-status.json',dict(status='waiting_for_matched_campaign'))
        time.sleep(10)
    stages = [['autoencoder'],['encode']]
    stages += [['train','--level',str(i)] for i in (1,2,3)]
    stages += [['evaluate','--kind',kind,'--split',split] for split in ('dev','test') for kind in ('teacher','reconstructed','field')]
    for i,stage in enumerate(stages):
        if (PARENT/'cancel').exists():
            write_json(REPORT/'controller-status.json',dict(status='cancelled',stage=stage))
            return
        write_json(REPORT/'controller-status.json',dict(status='running',stage=stage,index=i,total=len(stages)))
        name = f'{i:02d}-'+('-'.join(stage)).replace('--','')
        with (REPORT/f'{name}.log').open('ab') as log:
            result = subprocess.run([sys.executable,'-u',str(ROOT/'scripts/langsplat_reference.py'),*stage,'--protocol','full-view'],
                       cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,
                       env=dict(os.environ,CUDA_VISIBLE_DEVICES='7',OMP_NUM_THREADS='4',OPENBLAS_NUM_THREADS='4'))
        if result.returncode:
            write_json(REPORT/'controller-status.json',dict(status='failed',stage=stage,returncode=result.returncode,log=name+'.log'))
            return
    write_json(REPORT/'controller-status.json',dict(status='completed',stages=len(stages)))
    write_json(REPORT/'campaign-completed.json',dict(status='completed',levels=3,iterations_per_level=30000,
               autoencoder_epochs=100,gpu=7,scope='All187 SAM/CLIP views including evaluation images; no manual labels used in fit; not unseen-view generalization'))


if __name__=='__main__':
    main()
