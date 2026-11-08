"""Persistent automatic-location jobs use the same GPU lease as selection jobs."""
import os
import subprocess
import uuid
from .demo import catalogue, read
from .selection_jobs import SelectionJobs
from .gpu_lease import acquire,release
from .worker import atomic_json
from .gpu import worker_environment
from ..semantics.auto_locate import search_key
from ..semantics.selection_policy import source_identity,normalized_text


class AutoLocateJobs(SelectionJobs):
    def __init__(self,root,busy):
        super().__init__(root,busy)
        self.folder=self.root/'.cache/auto-locate-jobs'

    def history(self):
        values=super().history()
        return [v for v in values if v.get('request') and v.get('source_identity')
                and v.get('search_key')==search_key(v['request']['text'],v['source_identity'])]

    def submit(self,payload):
        if not isinstance(payload,dict) or set(payload)!={'model','text'} or payload['model']!='raw3':
            raise ValueError('自动定位需要新编码模型与物体名称，不需要指定相机')
        request=dict(model='raw3',text=normalized_text(payload['text']))
        if not next(m for m in catalogue(self.root)['models'] if m['id']=='raw3')['ready']:
            raise ValueError('新编码三维模型尚未就绪')
        identity=source_identity(self.root);key=search_key(request['text'],identity)
        with self.lock:
            for value in self.history():
                if value.get('search_key')==key and value['status'] in ('running','completed','rejected','needs_choice') and not value.get('chosen_id'):
                    return {**value,'cached':True}
            return self.start(dict(request=request,source_identity=identity,search_key=key))

    def choose(self,job_id,payload):
        value=self.job(job_id)
        if value['status'] not in ('needs_choice','rejected') or not value.get('candidates'):
            raise ValueError('此任务没有可确认的候选区域')
        if (not isinstance(payload,dict) or set(payload)!={'candidate_id'} or not isinstance(payload['candidate_id'],str) or
                payload['candidate_id'] not in {c['id'] for c in value['candidates']}):
            raise ValueError('请选择任务中的候选区域')
        if source_identity(self.root)!=value['source_identity']:
            raise ValueError('源模型已更新，请重新自动搜索')
        if value['search_key']!=search_key(value['request']['text'],value['source_identity']):
            raise ValueError('搜索策略已更新，请重新自动搜索')
        with self.lock:
            for previous in self.history():
                if (previous.get('search_key')==value['search_key'] and previous.get('chosen_id')==payload['candidate_id']
                        and previous['status'] in ('running','completed','rejected')):return {**previous,'cached':True}
            return self.start({k:value[k] for k in ('request','source_identity','search_key','candidates')}|
                              dict(chosen_id=payload['candidate_id'],parent_id=job_id))

    def start(self,value):
        if (self.process and self.process.poll() is None) or self.busy() or catalogue(self.root)['gpu_busy']:
            raise RuntimeError('GPU 正在执行任务，请关闭实时渲染或等待任务结束')
        job_id=uuid.uuid4().hex
        self.folder.mkdir(parents=True,exist_ok=True)
        directory=self.root/'data/auto_locate'/job_id;directory.mkdir(parents=True,exist_ok=True)
        self.reservation=acquire(self.root,directory)
        value.update(id=job_id,status='running',stage='starting',progress=0,pid=os.getpid(),
            message='正在启动自动搜索',report_url=f'/api/demo/auto-locate/{job_id}/report')
        atomic_json(self.path(job_id),value)
        try:
            with (self.folder/f'{job_id}.log').open('wb') as log:
                self.process=subprocess.Popen([str(self.root/'.venv/bin/python'),str(self.root/'scripts/auto_locate.py'),'--job-id',job_id],
                    cwd=self.root,stdout=log,stderr=subprocess.STDOUT,
                    env=dict(worker_environment(self.root),OMP_NUM_THREADS='4',OPENBLAS_NUM_THREADS='4',
                             MINICOLMAP_QUERY_RESERVATION=self.reservation))
            self.job_id=job_id
        except Exception:
            release(self.root,self.reservation)
            value.update(status='failed',message='自动搜索进程启动失败');atomic_json(self.path(job_id),value)
            raise
        return value
