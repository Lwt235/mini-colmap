"""Per-RGB-run semantic workspaces and process-isolated GPU jobs."""
import json
import os
from pathlib import Path
import re
import subprocess
import uuid

from .store import read_json
from .worker import atomic_json
from .gpu import worker_environment
from .gpu_lease import acquire, active_lease, release, process_identity


class SceneJobs:
    def __init__(self, root, store, live):
        self.root, self.store, self.live = Path(root), store, live
        self.processes = {}
        self.tokens = {}
        self.folder = self.root / '.cache/scene-jobs'

    def scene(self, scene_id):
        if not isinstance(scene_id, str) or not re.fullmatch(r'scene-[0-9a-f]{16}', scene_id):
            raise ValueError('无效的场景编号')
        folder = (self.root / 'data/semantic_scenes' / scene_id).resolve()
        if not folder.is_relative_to(self.root.resolve()) or not folder.is_relative_to((self.root / 'data/semantic_scenes').resolve()):
            raise ValueError('场景目录越界')
        record = read_json(folder / 'scene.json')
        if not record: raise KeyError('场景不存在')
        if record.get('id')!=scene_id: raise ValueError('场景编号不匹配')
        return record, folder

    def job(self, job_id):
        if not re.fullmatch(r'[0-9a-f]{32}', job_id): raise ValueError('无效的任务编号')
        value = read_json(self.folder / f'{job_id}.json')
        if not value: raise KeyError('任务不存在')
        process = self.processes.get(job_id)
        if value['status'] == 'running' and ((process is not None and process.poll() is not None) or (process is None and process_identity(value.get('pid')) is None)):
            value.update(status='failed', message='处理进程已退出，可重新生成索引')
            atomic_json(self.folder / f'{job_id}.json', value)
            release(self.root, self.tokens.get(job_id))
        if value['status']=='running' and value.get('stage')=='distill':
            _,folder=self.scene(value['scene_id'])
            value={**value,'progress':read_json(folder/'distilled/progress.json',{})}
        return value

    def catalogue(self):
        values = []
        for p in sorted((self.root / 'data/semantic_scenes').glob('scene-*/scene.json')):
            try:
                item, folder = self.scene(p.parent.name)
                run = self.store.get(item['rgb_run_id'])
                if run['status'] not in ('completed', 'imported'): continue
                ready = (folder / 'distilled/checkpoint.pt').is_file() and (folder / 'index-source.json').is_file()
                item = {**item, 'ready': ready, 'preview_url': f"/api/scenes/{item['id']}/photo"}
                jobs = [self.job(p.stem) for p in sorted(self.folder.glob('*.json'), key=lambda p:p.stat().st_mtime, reverse=True)
                        if read_json(p, {}).get('scene_id') == item['id']]
                item['job'] = jobs[0] if jobs else None
                values.append(item)
            except (OSError, ValueError, KeyError): continue
        return values

    def create(self, rgb_run_id, name=None):
        from ..io import load_reconstruction
        from ..semantics.project import sha256
        if name is not None and (not isinstance(name,str) or not 1 <= len(name.strip()) <= 80):
            raise ValueError('场景名称需为 1–80 字符')
        run = self.store.get(rgb_run_id)
        if run.get('pipeline') != '3dgs' or run['status'] not in ('completed', 'imported'):
            raise ValueError('请先完成 3DGS 训练')
        output = Path(run['output_path']).resolve()
        dataset = self.store.image_dataset(run)
        if not dataset: raise ValueError('未找到训练所用的原始照片')
        manifest, _ = dataset
        for path in (output, Path(manifest).resolve()):
            if not path.is_relative_to(self.root.resolve()): raise ValueError('场景源文件必须位于项目内')
        model = load_reconstruction(output / 'source_model.json')
        if len(model.poses) < 2: raise ValueError('场景至少需要两个已注册视角')
        source_hash = sha256(output / 'checkpoint.pt')
        scene_id = 'scene-' + source_hash[:16]
        folder = self.root / 'data/semantic_scenes' / scene_id
        if not folder.resolve().is_relative_to(self.root.resolve()): raise ValueError('场景目录越界')
        if (folder / 'scene.json').exists():
            record=self.scene(scene_id)[0]
            if name:
                record['name']=name.strip();atomic_json(folder/'scene.json',record)
            return record
        template = read_json(self.root / 'configs/semantic_waldo.json')
        if not template: raise ValueError('缺少语义基础配置')
        # Read train/holdout IDs from the RGB checkpoint inside the worker, never import torch here.
        config = {**template, 'schema_version': 2, 'name': scene_id,
                  'dataset': str(Path(manifest).resolve().relative_to(self.root.resolve())),
                  'rgb_run': str(output.relative_to(self.root.resolve())),
                  'workspace': str(folder.relative_to(self.root)),
                  'annotations': str((folder / 'annotations.json').relative_to(self.root)),
                  'dev_image_ids': [], 'smoke_image_ids': [min(model.poses)],
                  'autoencoder': {**template['autoencoder'], 'codec': 'pca', 'latent_dim': 32},
                  'extract': {**template['extract'], 'max_edge': 640, 'points_per_side': 24, 'crop_n_layers': 0},
                  'distill': {**template['distill'], 'max_edge': 512, 'steps': 3000}}
        config_path = self.root / 'configs' / f'semantic_{scene_id}.json'
        atomic_json(folder / 'annotations.json', {'frames': []})
        atomic_json(config_path, config)
        record = dict(id=scene_id, name=(name.strip() if name else run.get('name') or output.name),
                      rgb_run_id=rgb_run_id, rgb_checkpoint_sha256=source_hash,
                      config=config_path.stem, anchor_image_id=min(model.poses))
        atomic_json(folder / 'scene.json', record)
        return record

    def start(self, scene_id, action, text=None):
        from ..semantics.selection_policy import normalized_text
        record, folder = self.scene(scene_id)
        if action not in ('train', 'query'): raise ValueError('未知操作')
        if action == 'query':
            text = normalized_text(text)
            if not (folder / 'distilled/checkpoint.pt').is_file(): raise ValueError('请先生成此场景的语义索引')
        with self.store.lock:
            self.live.ensure_idle()
            if active_lease(self.root) or any(r['status']=='running' for r in self.store.list_runs()):
                raise RuntimeError('请先暂停浏览，或等待当前任务结束')
            job_id = uuid.uuid4().hex
            token = acquire(self.root, folder)
            value = dict(id=job_id, scene_id=scene_id, action=action, text=text,
                         config=record['config'], status='running', stage='starting', message='正在启动')
            path = self.folder / f'{job_id}.json'
            atomic_json(path, value)
            try:
                with path.with_suffix('.log').open('wb') as log:
                    process = subprocess.Popen([str(self.root / '.venv/bin/python'), '-m', 'minicolmap.semantics.scene_worker', str(path)],
                        cwd=self.root, stdout=log, stderr=subprocess.STDOUT,
                        env=dict(worker_environment(self.root), OMP_NUM_THREADS='4', OPENBLAS_NUM_THREADS='4', MINICOLMAP_QUERY_RESERVATION=token))
                self.processes[job_id], self.tokens[job_id] = process, token
            except Exception:
                release(self.root, token)
                value.update(status='failed', message='未能启动处理进程'); atomic_json(path, value)
                raise
            return value

    def cancel(self, job_id):
        value = self.job(job_id)
        process = self.processes.get(job_id)
        if value['status']=='running' and process:
            process.terminate()
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
            release(self.root, self.tokens.get(job_id))
            value.update(status='cancelled', message='已停止，可稍后重新生成')
            atomic_json(self.folder / f'{job_id}.json', value)
        return value

    def close(self):
        for job_id in list(self.processes):
            if (self.folder/f'{job_id}.json').exists(): self.cancel(job_id)
