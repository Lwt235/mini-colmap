"""One GPU query per process; only model IDs from the fixed catalogue are accepted."""
import argparse
import json
import os
import traceback
import langsplat_reference as ref
from minicolmap.ui.demo import MODELS, model_report, query_result
from minicolmap.ui.worker import atomic_json
from minicolmap.semantics.project import load_config, project
from minicolmap.semantics.encoders import gpu_session


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--model',choices=MODELS,required=True)
    parser.add_argument('--image-id',type=int,required=True)
    parser.add_argument('--text',required=True)
    parser.add_argument('--job-id',required=True)
    args = parser.parse_args()
    if len(args.job_id)!=32 or any(c not in '0123456789abcdef' for c in args.job_id):
        parser.error('Invalid job ID')
    job = ref.ROOT/f'.cache/demo-queries/{args.job_id}.json'
    try:
        config = load_config(ref.ROOT/'configs/semantic_waldo.json')
        prepared = project(config)
        report = model_report(ref.ROOT,args.model)
        before = set((report/'queries').glob('*/query.json'))
        with gpu_session({**config,'workspace':str(report)},reservation_token=os.environ.get('MINICOLMAP_QUERY_RESERVATION')):
            ref.seed()
            if args.model=='raw3':
                import langsplat_raw3 as raw
                raw.configure()
                ref.native_imports()
                raw.install_renderer()
            ref.query(config,prepared,args.image_id,args.text)
        paths = set((report/'queries').glob('*/query.json'))-before
        if len(paths)!=1:
            raise ValueError('Query output identity is ambiguous')
        result = query_result(ref.ROOT,args.model,paths.pop())
        atomic_json(job,dict(id=args.job_id,status='completed',cached=False,result=result))
    except Exception:
        traceback.print_exc()
        atomic_json(job,dict(id=args.job_id,status='failed',message='查询未完成，请检查 GPU 状态与模型文件。'))
        raise
    finally:
        from minicolmap.ui.gpu_lease import release
        if os.environ.get('MINICOLMAP_QUERY_RESERVATION'):
            release(ref.ROOT,os.environ['MINICOLMAP_QUERY_RESERVATION'])


if __name__=='__main__':
    main()
