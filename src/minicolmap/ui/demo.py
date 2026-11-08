"""Small product-facing catalogue and process-isolated text query jobs."""
from pathlib import Path
import json
import os
import subprocess
import threading
import uuid

from .gpu_lease import active_lease, acquire, release, process_identity
from .worker import atomic_json
from .gpu import worker_environment

MODELS = {
    'reference': ('官方参考 · 三维语言场', 'langsplat_reference', 'langsplat-reference-20261021'),
    'raw3': ('新编码 · 三维语言场', 'langsplat_raw3', 'langsplat-raw3-20261021'),
}


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def model_report(root, model):
    if not isinstance(model,str) or model not in MODELS:
        raise ValueError('请选择已配置的三维模型')
    return Path(root)/'reports'/MODELS[model][2]


def query_result(root, model, path):
    value = read(path)
    if not value:
        return None
    folder = path.parent.resolve()
    report = model_report(root, model).resolve()
    if not folder.is_relative_to(report) or not all((folder/f).is_file() for f in ('heatmap.png','mask.png')):
        return None
    prefix = '/api/demo/artifacts/'+model+'/'+folder.relative_to(report).as_posix()
    return {**{k:value[k] for k in ('image_id','text','level','selected_pixels')},
            'model':model,'heatmap_url':prefix+'/heatmap.png','mask_url':prefix+'/mask.png',
            'photo_url':f"/api/demo/images/{value['image_id']}"}


def catalogue(root):
    root = Path(root)
    prepared = read(root/'data/semantic_waldo/project.json',{})
    models = []
    for key,(name,workspace,_) in MODELS.items():
        report = model_report(root,key)
        levels = []
        for level in (1,2,3):
            directory = root/'data'/workspace/'train'/f'level{level}'
            done = read(directory/'completed.json')
            trace = read(directory/'progress.json',{}).get('trace',[])
            levels.append({'level':level,'iteration':done['iterations'] if done else trace[-1]['iteration'] if trace else 0,
                'ready':bool(done and (directory/'chkpnt30000.pth').is_file())})
        summary = read(report/'evaluation/dev-field/summary.json',{})
        models.append({'id':key,'name':name,'ready':all(row['ready'] for row in levels),'levels':levels,
            'evaluation_ready':bool(summary),'dev_iou':summary.get('results',{}).get('official',{}).get('mean_iou'),
            'controller':read(report/'controller-status.json'),'progress':read(report/'progress.json')})
    gallery = []
    for model in MODELS:
        report = model_report(root,model)
        for path in sorted((report/'queries').glob('*/query.json'), reverse=True)[:12]:
            item = query_result(root,model,path)
            if item:
                gallery.append(item)
    campaign = read(root/'reports/langsplat-raw3-20261021/controller-status.json',{})
    campaign_busy = campaign.get('status')=='running' and process_identity(campaign.get('pid')) is not None
    evaluated = [m for m in models if m['ready'] and m['evaluation_ready'] and m['dev_iou'] is not None]
    recommended = max(evaluated,key=lambda m:m['dev_iou'])['id'] if evaluated else 'reference'
    return {'available':bool(prepared),'scene':'Waldo Kitchen','views':len(prepared.get('frames',[])),
        'recommended_model':recommended,'selection_metric':'Dev official-protocol IoU; test not used for selection',
        'gaussians':prepared.get('gaussian_count',0),'gpu_busy':active_lease(root) is not None or campaign_busy,
        'models':models,'gallery':gallery,
        'frames':[{'id':f['image_id'],'name':f['name']} for f in prepared.get('frames',[])],
        'training_views':len(prepared.get('splits',{}).get('train',[]))}


class DemoQueries:
    def __init__(self, root, busy):
        self.root, self.busy = Path(root), busy
        self.lock = threading.Lock()
        self.process = None
        self.job_id = None
        self.reservation = None

    def reap(self):
        with self.lock:
            if self.process and self.process.poll() is not None:
                release(self.root,self.reservation)

    def submit(self, payload):
        self.reap()
        model = payload.get('model')
        report = model_report(self.root,model)
        image_id,text = payload.get('image_id'),payload.get('text')
        frames = read(self.root/'data/semantic_waldo/project.json',{}).get('frames',[])
        if isinstance(image_id,bool) or not isinstance(image_id,int) or image_id not in {f['image_id'] for f in frames}:
            raise ValueError('请选择场景中的相机')
        if not isinstance(text,str) or not 1<=len(text.strip())<=160 or any(ord(c)<32 for c in text):
            raise ValueError('请输入 1–160 字符的物体名称')
        text = text.strip()
        model_status = next(m for m in catalogue(self.root)['models'] if m['id']==model)
        if not model_status['ready']:
            raise ValueError('该模型仍在训练，请选择已就绪的模型')
        with self.lock:
            # Identical saved queries can be replayed while the GPU is training.
            for path in sorted((report/'queries').glob('*/query.json'),reverse=True):
                result = query_result(self.root,model,path)
                if result and result['image_id']==image_id and result['text']==text:
                    return {'status':'completed','cached':True,'result':result}
            if (self.process and self.process.poll() is None) or self.busy() or catalogue(self.root)['gpu_busy']:
                raise RuntimeError('GPU 正在训练或渲染。可查看已保存的结果，待任务结束后再查询。')
            job_id = uuid.uuid4().hex
            directory = self.root/'.cache/demo-queries'
            directory.mkdir(parents=True,exist_ok=True)
            atomic_json(directory/f'{job_id}.json',{'status':'running','id':job_id,'model':model})
            self.reservation = acquire(self.root,report)
            try:
                with (directory/f'{job_id}.log').open('wb') as stream:
                    self.process = subprocess.Popen([str(self.root/'.venv/bin/python'),str(self.root/'scripts/demo_query.py'),
                        '--model',model,'--image-id',str(image_id),'--text',text,'--job-id',job_id],cwd=self.root,
                        stdout=stream,stderr=subprocess.STDOUT,
                        env=dict(worker_environment(self.root),OMP_NUM_THREADS='4',OPENBLAS_NUM_THREADS='4',
                            MINICOLMAP_QUERY_RESERVATION=self.reservation))
            except Exception:
                release(self.root,self.reservation)
                raise
            self.job_id = job_id
            return {'status':'running','id':job_id}

    def job(self, job_id):
        if len(job_id)!=32 or any(c not in '0123456789abcdef' for c in job_id):
            raise ValueError('无效的查询编号')
        value = read(self.root/f'.cache/demo-queries/{job_id}.json')
        if value is None:
            raise KeyError('查询不存在')
        if value['status']=='running' and job_id==self.job_id and self.process.poll() is not None:
            release(self.root,self.reservation)
            value.update(status='failed',message='查询进程退出，请检查查询日志后重试')
        return value

    def close(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if self.reservation:
            release(self.root,self.reservation)
