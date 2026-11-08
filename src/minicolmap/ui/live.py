"""Bounded WebSocket camera streaming; the web process never imports torch."""
from __future__ import annotations

import asyncio
import json
import multiprocessing
from pathlib import Path

import numpy as np
from fastapi import WebSocket, WebSocketDisconnect

from ..models import Camera, Pose


def validate_request(body):
    if not isinstance(body, dict) or set(body)-{'quality','selection'} != {'type', 'seq', 'camera'} or body.get('type') != 'camera':
        raise ValueError('请发送 type=camera、seq 和 camera')
    seq, camera = body['seq'], body['camera']
    quality=body.get('quality','final')
    selection=body.get('selection')
    if 'selection' in body:
        from ..semantics.selection_policy import valid_selection_id
        if (not isinstance(selection,dict) or set(selection)!={'id','mode'}
                or not valid_selection_id(selection['id']) or selection['mode'] not in ('highlight','isolate')):
            raise ValueError('选区或显示模式无效')
    if quality not in ('interactive','final'):
        raise ValueError('quality 必须为 interactive 或 final')
    if isinstance(seq, bool) or not isinstance(seq, int) or not 0 <= seq <= 9007199254740991:
        raise ValueError('seq 必须是非负安全整数')
    if not isinstance(camera, dict) or set(camera) != {'width', 'height', 'K', 'R', 't'}:
        raise ValueError('camera 必须包含 width、height、K、R、t')
    try:
        calibration = Camera(1, camera['width'], camera['height'], camera['K'])
        pose = Pose(camera['R'], camera['t'])
    except (TypeError, OverflowError) as exc:
        raise ValueError('相机尺寸、内参或位姿无效') from exc
    if (calibration.width > 1920 or calibration.height > 1080
            or calibration.width * calibration.height > 2073600):
        raise ValueError('实时分辨率最多 1920×1080，2073600 像素')
    if not np.isclose(calibration.K[0, 1], 0, rtol=0, atol=1e-8):
        raise ValueError('gsplat 实时渲染不支持 skew 内参')
    if (not 1e-3 <= calibration.K[0, 0] <= 1e7
            or not 1e-3 <= calibration.K[1, 1] <= 1e7
            or np.abs(calibration.K[:2, 2]).max() > 1e7):
        raise ValueError('实时焦距须为 0.001 至 10000000 像素，主点绝对值不得超过 10000000 像素')
    if any(np.abs(value).max() > np.finfo(np.float32).max / 64 for value in (calibration.K, pose.t)):
        raise ValueError('相机数值超出 CUDA float32 可表示范围')
    result={'type': 'camera', 'seq': seq, 'camera': {'width': calibration.width, 'height': calibration.height,
            'K': calibration.K.tolist(), 'R': pose.R.tolist(), 't': pose.t.tolist()}}
    if 'quality' in body:
        result['quality']=quality
    if selection:result['selection']=dict(selection)
    return result


def _gpu_worker_entry(target,args,gpu):
    import os
    os.environ['CUDA_VISIBLE_DEVICES']=str(gpu)
    target(*args)


class ProcessWorker:
    def __init__(self, output, *, target=None, extra_args=()):
        from ..rendering.live import worker_main
        context = multiprocessing.get_context('spawn')
        self.connection, child = context.Pipe(duplex=True)
        from .gpu import selected_gpu
        self.process = context.Process(target=_gpu_worker_entry, args=(target or worker_main,(child,str(output),*extra_args),selected_gpu(Path(output).parent.parent)),daemon=True)
        try:
            self.process.start()
        except Exception:
            self.connection.close()
            child.close()
            raise
        child.close()

    async def receive(self, timeout):
        try:
            return await asyncio.wait_for(asyncio.to_thread(self.connection.recv), timeout)
        except asyncio.TimeoutError as exc:
            raise RuntimeError('实时渲染子进程响应超时，已释放 GPU 会话') from exc
        except (EOFError, OSError) as exc:
            raise RuntimeError('实时渲染子进程已退出') from exc

    def send(self, request):
        self.connection.send(request)

    def close(self):
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(timeout=1)
        if self.process.is_alive():
            self.process.kill()
            self.process.join(timeout=1)
        self.connection.close()


class LiveSessions:
    def __init__(self, store):
        self.store, self.active = store, None
        self.worker_factory = ProcessWorker

    def ensure_idle(self):
        # Called under the same store RLock as worker creation and training start.
        if self.active is not None:
            raise RuntimeError('实时高斯渲染正在占用 GPU，请先关闭实时视图再启动任务')

    def claim(self, run_id):
        from .gpu_lease import ensure_experiment_idle
        ensure_experiment_idle(self.store.root)
        with self.store.lock:
            self.ensure_idle()
            if any(run['status'] == 'running' for run in self.store.list_runs()):
                raise RuntimeError('请先等待当前任务结束或取消，再打开实时高斯渲染')
            run = self.store.get(run_id)
            if run.get('pipeline') != '3dgs':
                raise ValueError('实时渲染仅支持 3DGS 训练结果')
            if run['status'] not in ('completed', 'cancelled', 'interrupted'):
                raise ValueError('请先完成或停止训练，再打开实时高斯渲染')
            output = Path(run['output_path'])
            if not (output / 'checkpoint.pt').is_file():
                raise ValueError('该任务没有可用 checkpoint')
            lease = {'run_id': run_id, 'worker': None}
            self.active = lease
            return lease, output

    def release(self, lease):
        worker = lease.get('worker')
        try:
            if worker is not None:
                worker.close()
        finally:
            lease['worker'] = None
            with self.store.lock:
                if self.active is lease:
                    self.active = None

    def close(self):
        if self.active is not None:
            self.release(self.active)

    async def handle(self, websocket: WebSocket, run_id):
        await websocket.accept()
        origin = websocket.headers.get('origin')
        expected = ('https' if websocket.url.scheme == 'wss' else 'http') + '://' + websocket.url.netloc
        if origin and origin != expected:
            await websocket.send_json({'type': 'error', 'message': '只允许工作台同源请求'})
            await websocket.close(code=1008)
            return
        lease, tasks = None, []
        send_lock, changed = asyncio.Lock(), asyncio.Event()
        latest = None

        async def send_json(message):
            async with send_lock:
                await websocket.send_json(message)

        async def receive_cameras():
            nonlocal latest
            while True:
                text = await websocket.receive_text()
                try:
                    if len(text) > 65536:
                        raise ValueError('相机消息不得超过 64 KB')
                    latest = validate_request(json.loads(text))
                    changed.set()
                except (ValueError, KeyError) as exc:
                    await send_json({'type': 'error', 'message': str(exc)})

        async def render_frames(worker):
            nonlocal latest
            ready = await worker.receive(60)
            if not isinstance(ready, dict) or ready.get('type') != 'ready':
                raise RuntimeError(ready.get('message', '实时渲染初始化失败') if isinstance(ready, dict) else '初始化协议无效')
            await send_json(ready)
            while True:
                await changed.wait()
                request, latest = latest, None
                changed.clear()
                if request is None:
                    continue
                worker.send(request)
                frame = await worker.receive(20)
                if not isinstance(frame, tuple) or len(frame) != 2:
                    raise RuntimeError(frame.get('message', '实时渲染失败') if isinstance(frame, dict) else '实时帧协议无效')
                async with send_lock:
                    await websocket.send_json(frame[0])
                    await websocket.send_bytes(frame[1])

        try:
            lease, output = self.claim(run_id)
            lease['worker'] = self.worker_factory(output)
            tasks = [asyncio.create_task(receive_cameras()), asyncio.create_task(render_frames(lease['worker']))]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            try:
                await send_json({'type': 'error', 'message': str(exc).strip('"\'')})
                await websocket.close(code=1011)
            except (WebSocketDisconnect, RuntimeError, OSError):
                pass
        finally:
            for task in tasks:
                task.cancel()
            if lease is not None:
                # Kill before awaiting cancelled recv threads so a partial pipe
                # transfer cannot leave the web process waiting indefinitely.
                self.release(lease)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
