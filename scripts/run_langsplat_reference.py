"""Finite reference campaign controller with explicit completion/failure markers."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from minicolmap.semantics.project import write_json,sha256

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT/'reports/langsplat-reference-20261021'
WORK = ROOT/'data/langsplat_reference'
UP = ROOT/'.cache/langsplat-official'


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--preprocessing-pid',type=int)
    args = parser.parse_args()
    REPORT.mkdir(parents=True,exist_ok=True)
    if (REPORT/'campaign-completed.json').exists():
        raise ValueError('Campaign already completed; use another workspace for another experiment')
    write_json(REPORT/'controller.json',dict(pid=os.getpid(),preprocessing_pid=args.preprocessing_pid))
    revision = subprocess.check_output(['git','-C',str(UP),'rev-parse','HEAD'],text=True).strip()
    write_json(REPORT/'source-manifest.json',dict(revision=revision,
               submodules=subprocess.check_output(['git','-C',str(UP),'submodule','status','--recursive'],text=True).splitlines(),
               files={str(p.relative_to(UP)):sha256(p) for p in
                      [UP/'preprocess.py',UP/'autoencoder/model.py',UP/'autoencoder/train.py',
                       UP/'train.py',UP/'gaussian_renderer/__init__.py',UP/'eval/evaluate_iou_loc.py']},
               native_libraries={str(p):sha256(p) for p in (ROOT/'.cache/langsplat-runtime/site').rglob('*.so')},
               gpu=7, protocol='frozen original RGB; 173 language train, 2 dev, 3 test; native resolution; official semantic model/method and metric functions through explicit data adapters'))
    if args.preprocessing_pid:
        while not (REPORT/'preprocessing.json').exists():
            if (REPORT/'cancel').exists():
                write_json(REPORT/'controller-status.json',dict(status='cancelled_before_training'))
                return
            try:
                os.kill(args.preprocessing_pid,0)
            except ProcessLookupError:
                break
            write_json(REPORT/'controller-status.json',dict(status='waiting_for_preprocessing',pid=args.preprocessing_pid))
            time.sleep(10)
    stages = []
    if not (REPORT/'preprocessing.json').exists():
        stages.append(['preprocess'])
    stages += [['autoencoder'],['encode'],['legacy-evaluate'],
               ['evaluate','--kind','teacher','--split','dev'],
               ['evaluate','--kind','reconstructed','--split','dev'],
               ['train','--level','1','--steps','100']]
    stages += [['train','--level',str(level)] for level in (1,2,3)]
    stages += [['evaluate','--kind','field','--split','dev']]
    stages += [['evaluate','--kind','field','--split','dev','--checkpoint-step',str(n)] for n in (3000,7000)]
    # Fixed upstream recipe, no dev-driven hyperparameter changes before final test.
    stages += [['evaluate','--kind',kind,'--split','test'] for kind in ('teacher','reconstructed','field')]
    for i,stage in enumerate(stages):
        if (REPORT/'cancel').exists():
            write_json(REPORT/'controller-status.json',dict(status='cancelled',stage=stage))
            return
        name = f'{i:02d}-'+('-'.join(stage)).replace('--','')
        write_json(REPORT/'controller-status.json',dict(status='running',stage=stage,index=i,total=len(stages)))
        print('STAGE',stage,flush=True)
        with (REPORT/f'{name}.log').open('ab') as stream:
            child = subprocess.Popen([sys.executable,'-u',str(ROOT/'scripts/langsplat_reference.py'),*stage],
                       cwd=ROOT,stdout=stream,stderr=subprocess.STDOUT,start_new_session=True,
                       env=dict(os.environ,CUDA_VISIBLE_DEVICES='7',OMP_NUM_THREADS='4',OPENBLAS_NUM_THREADS='4'))
            write_json(REPORT/'active-process.json',dict(pid=child.pid,stage=stage,log=name+'.log'))
            while child.poll() is None:
                if (REPORT/'cancel').exists():
                    os.killpg(child.pid,signal.SIGTERM)
                    child.wait()
                    write_json(REPORT/'controller-status.json',dict(status='cancelled',stage=stage))
                    return
                time.sleep(5)
        if child.returncode:
            write_json(REPORT/'controller-status.json',dict(status='failed',stage=stage,returncode=child.returncode,log=name+'.log'))
            return
    write_json(REPORT/'controller-status.json',dict(status='completed',stages=len(stages)))
    write_json(REPORT/'campaign-completed.json',dict(status='completed',levels=3,iterations_per_level=30000,
               autoencoder_epochs=100,gpu=7,source_revision=revision,test_policy='One final fixed-recipe evaluation; not used for selection'))


if __name__=='__main__':
    main()
