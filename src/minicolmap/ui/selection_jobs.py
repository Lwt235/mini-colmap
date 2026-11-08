"""Persistent, process-isolated text-to-3D jobs sharing the GPU lease."""
import os
from pathlib import Path
import subprocess
import threading
import uuid

from .demo import catalogue as demo_catalogue, read
from .gpu_lease import acquire, release, process_identity
from .worker import atomic_json
from .gpu import worker_environment
from ..semantics.selection import selection_directory
from ..semantics.selection_policy import normalized_text, selection_key, source_identity


def validate_payload(root, payload):
    if not isinstance(payload, dict) or set(payload) != {'model', 'image_id', 'text'}:
        raise ValueError('选区请求需要模型、相机与物体名称')
    if payload['model'] != 'raw3':
        raise ValueError('三维选区目前使用新编码模型')
    text = normalized_text(payload['text'])
    image_id = payload['image_id']
    prepared = read(Path(root)/'data/semantic_waldo/project.json', {})
    if (isinstance(image_id, bool) or not isinstance(image_id, int)
            or image_id not in {f['image_id'] for f in prepared.get('frames', [])}):
        raise ValueError('请选择场景中的相机')
    if image_id in prepared.get('splits', {}).get('test', []):
        raise ValueError('Test 相机仅用于评测，请选择训练或 Dev 相机生成选区')
    return dict(model='raw3', image_id=image_id, text=text)


class SelectionJobs:
    def __init__(self, root, busy):
        self.root, self.busy = Path(root), busy
        self.lock = threading.Lock()
        self.process = None
        self.job_id = None
        self.reservation = None
        self.folder = self.root/'.cache/object-selection-jobs'

    def path(self, job_id):
        if not isinstance(job_id, str) or len(job_id) != 32 or any(c not in '0123456789abcdef' for c in job_id):
            raise ValueError('无效的选区任务编号')
        return self.folder/f'{job_id}.json'

    def reap(self):
        if self.process and self.process.poll() is not None:
            release(self.root, self.reservation)
        # Preserve finished results across UI restarts, fail orphaned jobs.
        for path in self.folder.glob('*.json'):
            value = read(path, {})
            exited = path.stem==self.job_id and self.process and self.process.poll() is not None
            if value.get('status') == 'running' and (exited or process_identity(value.get('pid')) is None):
                value.update(status='failed', message='选区进程已退出；可重试，诊断日志已保留')
                atomic_json(path, value)

    def history(self):
        self.reap()
        return [read(p) for p in sorted(self.folder.glob('*.json'), key=lambda p:p.stat().st_mtime, reverse=True)[:20]]

    def submit(self, payload):
        request = validate_payload(self.root, payload)
        if not next(m for m in demo_catalogue(self.root)['models'] if m['id']=='raw3')['ready']:
            raise ValueError('新编码三维模型尚未就绪')
        identity = source_identity(self.root)
        sid = selection_key(request['text'], request['image_id'], identity)
        with self.lock:
            self.reap()
            for value in self.history():
                if value.get('selection_id') == sid and value['status'] in ('completed','rejected','running'):
                    return {**value, 'cached': True}
            if (self.process and self.process.poll() is None) or self.busy() or demo_catalogue(self.root)['gpu_busy']:
                raise RuntimeError('GPU 正在执行任务。请关闭实时渲染，或等待当前任务结束后再生成选区。')
            job_id = uuid.uuid4().hex
            directory = selection_directory(self.root, sid)
            directory.mkdir(parents=True, exist_ok=True)
            atomic_json(directory/'request.json', dict(**request, source_identity=identity, selection_id=sid))
            self.folder.mkdir(parents=True, exist_ok=True)
            self.reservation = acquire(self.root, directory)
            value = dict(id=job_id, selection_id=sid, status='running', stage='starting', progress=0,
                         request=request, message='正在启动三维选区任务', pid=os.getpid(),
                         report_url=f'/api/demo/selections/{sid}/report')
            atomic_json(self.path(job_id), value)
            try:
                with (self.folder/f'{job_id}.log').open('wb') as log:
                    self.process = subprocess.Popen([str(self.root/'.venv/bin/python'),
                        str(self.root/'scripts/generate_object_selection.py'), '--job-id', job_id],
                        cwd=self.root, stdout=log, stderr=subprocess.STDOUT,
                        env=dict(worker_environment(self.root), OMP_NUM_THREADS='4', OPENBLAS_NUM_THREADS='4',
                                 MINICOLMAP_QUERY_RESERVATION=self.reservation))
                # Do not overwrite a worker's newer progress JSON.
                self.job_id = job_id
            except Exception:
                release(self.root, self.reservation)
                value.update(status='failed', message='选区进程启动失败')
                atomic_json(self.path(job_id), value)
                raise
            return value

    def job(self, job_id):
        path = self.path(job_id)
        self.reap()
        value = read(path)
        if value is None:
            raise KeyError('选区任务不存在')
        return value

    def close(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill(); self.process.wait()
            value = read(self.path(self.job_id), {})
            if value.get('status') == 'running':
                value.update(status='failed', message='工作台关闭，选区任务已停止；可重试')
                atomic_json(self.path(self.job_id), value)
        if self.reservation:release(self.root, self.reservation)
