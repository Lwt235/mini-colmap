"""Bounded live transport tests. Protocol workers do not emulate CUDA maths."""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys

import numpy as np
import pytest

pytest.importorskip('fastapi')
pytest.importorskip('httpx')
from fastapi.testclient import TestClient

from minicolmap.rendering.live import normalize_camera
from minicolmap.ui.app import create_app
from minicolmap.ui.live import validate_request


def packet(seq=1):
    return {'type': 'camera', 'seq': seq, 'camera': {'width': 640, 'height': 480,
        'K': [[500, 0, 320], [0, 500, 240], [0, 0, 1]],
        'R': np.eye(3).tolist(), 't': [0, 0, 2]}}


def test_live_quality_protocol_is_optional_and_strict():
    body=packet()
    assert 'quality' not in validate_request(body)
    for quality in ('interactive','final'):
        assert validate_request({**body,'quality':quality})['quality']==quality
    for quality in (None,False,'fast',95):
        with pytest.raises(ValueError,match='quality'):
            validate_request({**body,'quality':quality})


def test_object_selection_protocol_rejects_arbitrary_files_and_modes():
    from minicolmap.semantics.selection import SELECTION_ID
    for mode in ('highlight','isolate'):
        value={'id':SELECTION_ID,'mode':mode}
        assert validate_request({**packet(),'selection':value})['selection']==value
    for value in (None,'sink',{'id':'../../other','mode':'highlight'},{'id':SELECTION_ID,'mode':'delete'},
                  {'id':SELECTION_ID,'mode':'highlight','path':'other.pt'}):
        with pytest.raises(ValueError):validate_request({**packet(),'selection':value})


class ProtocolWorker:
    instances = []
    def __init__(self, output):
        self.first, self.closed, self.request = True, False, None
        self.instances.append(self)

    async def receive(self, timeout):
        await asyncio.sleep(0)
        if self.first:
            self.first = False
            return {'type': 'ready', 'backend': 'gsplat', 'device': 'protocol-test',
                    'gaussian_count': 7, 'sh_degree': 3}
        request, self.request = self.request, None
        return ({'type': 'frame', 'seq': request['seq'], 'width': request['camera']['width'],
                 'height': request['camera']['height'], 'render_ms': 2, 'encode_ms': 1}, b'protocol-jpeg')

    def send(self, request):
        assert self.request is None
        self.request = request

    def close(self):
        self.closed = True


@pytest.fixture
def live_client(tmp_path):
    output = tmp_path / 'outputs' / 'trained'
    output.mkdir(parents=True)
    (output / 'checkpoint.pt').write_bytes(b'not-loaded-by-transport-test')
    app = create_app(tmp_path)
    app.state.store.runs['trained'] = {'id': 'trained', 'pipeline': '3dgs', 'status': 'completed',
            'output_path': str(output), 'name': 'trained'}
    app.state.live.worker_factory = ProtocolWorker
    with TestClient(app) as client:
        yield client


def test_camera_normalization_preserves_projection_for_rotated_world():
    R = np.array([[0, 0, 1], [0, 1, 0], [-1, 0, 0]], dtype=float)
    original = packet()['camera']
    original.update(R=R.tolist(), t=[3, -1, 2])
    normalization = {'center': [7, 8, 9], 'scale': 4}
    frame = normalize_camera(original, normalization)
    xyz = np.array([1., 4., 5.])
    normalized = (xyz - normalization['center']) / normalization['scale']
    np.testing.assert_allclose(R @ normalized + frame['t'], (R @ xyz + original['t']) / 4)
    assert frame['K'] == original['K']


@pytest.mark.parametrize('normalization,t', [
    ({'center': [0, 0, 0], 'scale': 0}, [0, 0, 1]),
    ({'center': [0, 0, 0], 'scale': float('nan')}, [0, 0, 1]),
    ({'center': [0, 0, 0], 'scale': 1e-100}, [0, 0, 1]),
    ({'center': [1e308, 0, 0], 'scale': 1e-200}, [0, 0, 1]),
    ({'center': [0, 0, 0], 'scale': 1}, [1e11, 0, 1]),
])
def test_reject_normalized_translation_outside_cuda_range(normalization, t):
    camera = packet()['camera']
    camera['t'] = t
    with pytest.raises(ValueError):
        normalize_camera(camera, normalization)


@pytest.mark.parametrize('change', [
    lambda p: p.update(seq=True), lambda p: p.update(seq=-1),
    lambda p: p.update(seq=float('nan')), lambda p: p.update(extra=1),
    lambda p: p['camera'].update(width=1921), lambda p: p['camera'].update(height=1081),
    lambda p: p['camera'].update(width=True), lambda p: p['camera'].update(K=np.eye(3).tolist()[:2]),
    lambda p: p['camera']['K'][0].__setitem__(0, -1),
    lambda p: p['camera']['K'][0].__setitem__(1, .5),
    lambda p: p['camera']['K'][0].__setitem__(0, 1e30),
    lambda p: p['camera']['K'][1].__setitem__(1, 1e-100),
    lambda p: p['camera']['K'][0].__setitem__(2, 1e8),
    lambda p: p['camera']['R'][0].__setitem__(0, -1),
    lambda p: p['camera']['t'].__setitem__(0, float('inf')),
    lambda p: p['camera']['t'].__setitem__(0, 1e300),
])
def test_reject_invalid_camera(change):
    body = packet()
    change(body)
    with pytest.raises(ValueError):
        validate_request(body)


def test_real_websocket_protocol_and_disconnect_cleanup(live_client):
    with live_client.websocket_connect('/api/rendering/runs/trained/live') as websocket:
        assert websocket.receive_json()['type'] == 'ready'
        websocket.send_json(packet(12))
        frame = websocket.receive_json()
        assert frame['type'] == 'frame' and frame['seq'] == 12
        assert frame['width'] == 640
        assert websocket.receive_bytes() == b'protocol-jpeg'
        websocket.send_text('{')
        assert websocket.receive_json()['type'] == 'error'
        websocket.send_json(packet(13))
        assert websocket.receive_json()['seq'] == 13
        websocket.receive_bytes()
    assert ProtocolWorker.instances[-1].closed
    assert live_client.app.state.live.active is None


def test_training_and_other_sessions_blocked_until_disconnect(live_client):
    with live_client.websocket_connect('/api/rendering/runs/trained/live') as websocket:
        websocket.receive_json()
        with live_client.websocket_connect('/api/rendering/runs/trained/live') as other:
            assert '实时' in other.receive_json()['message']
        for route, body in [('/api/runs', {'dataset_id': 'unused'}),
                            ('/api/rendering/runs', {'source_run_id': 'trained'})]:
            response = live_client.post(route, json=body)
            assert response.status_code == 409 and '实时' in response.text
    assert live_client.app.state.live.active is None


def test_reject_live_when_training_active(live_client):
    live_client.app.state.store.runs['busy'] = {'id': 'busy', 'status': 'running', 'output_path': '', 'name': 'busy'}
    # get() refreshes only records backed by actual metadata/processes.
    with live_client.websocket_connect('/api/rendering/runs/trained/live') as websocket:
        assert '任务' in websocket.receive_json()['message']
    assert live_client.app.state.live.active is None


@pytest.mark.parametrize('change', ['missing', 'nerf', 'checkpoint', 'status'])
def test_reject_inapplicable_run(live_client, change):
    store = live_client.app.state.store
    route = 'missing' if change == 'missing' else 'trained'
    if change == 'nerf':
        store.runs['trained']['pipeline'] = 'nerf'
    elif change == 'checkpoint':
        from pathlib import Path
        (Path(store.runs['trained']['output_path']) / 'checkpoint.pt').unlink()
    elif change == 'status':
        store.runs['trained']['status'] = 'failed'
    with live_client.websocket_connect(f'/api/rendering/runs/{route}/live') as websocket:
        assert websocket.receive_json()['type'] == 'error'
    assert live_client.app.state.live.active is None


def test_reject_cross_origin_websocket(live_client):
    with live_client.websocket_connect('/api/rendering/runs/trained/live', headers={'origin': 'https://evil.example'}) as websocket:
        assert '同源' in websocket.receive_json()['message']
    assert live_client.app.state.live.active is None


def test_worker_load_error_releases_reservation(live_client):
    class BrokenWorker(ProtocolWorker):
        async def receive(self, timeout):
            return {'type': 'error', 'message': 'gsplat load failed'}
    live_client.app.state.live.worker_factory = BrokenWorker
    with live_client.websocket_connect('/api/rendering/runs/trained/live') as websocket:
        assert 'gsplat load failed' in websocket.receive_json()['message']
    assert ProtocolWorker.instances[-1].closed
    assert live_client.app.state.live.active is None


def test_worker_timeout_releases_reservation(live_client):
    class TimeoutWorker(ProtocolWorker):
        async def receive(self, timeout):
            if self.first:
                return await super().receive(timeout)
            raise RuntimeError('响应超时')
    live_client.app.state.live.worker_factory = TimeoutWorker
    with live_client.websocket_connect('/api/rendering/runs/trained/live') as websocket:
        assert websocket.receive_json()['type'] == 'ready'
        websocket.send_json(packet())
        assert '超时' in websocket.receive_json()['message']
    assert ProtocolWorker.instances[-1].closed
    assert live_client.app.state.live.active is None


def test_latest_camera_overwrites_pending_work(live_client):
    from fastapi import WebSocketDisconnect
    from types import SimpleNamespace

    async def scenario():
        gate = asyncio.Event()
        rendered = []

        class SlowWorker(ProtocolWorker):
            async def receive(self, timeout):
                if self.first:
                    return await super().receive(timeout)
                await gate.wait()
                rendered.append(self.request['seq'])
                return await super().receive(timeout)

        class Socket:
            headers = {}
            url = SimpleNamespace(scheme='ws', netloc='testserver')
            def __init__(self):
                self.incoming, self.consumed, self.frames = asyncio.Queue(), 0, []
            async def accept(self):
                pass
            async def receive_text(self):
                body = await self.incoming.get()
                if body is None:
                    raise WebSocketDisconnect()
                self.consumed += 1
                return json.dumps(body)
            async def send_json(self, message):
                if message['type'] == 'frame':
                    self.frames.append(message['seq'])
            async def send_bytes(self, data):
                pass
            async def close(self, code):
                pass

        async def until(predicate):
            for _ in range(1000):
                if predicate():
                    return
                await asyncio.sleep(.001)
            raise AssertionError('protocol did not make expected progress')

        manager = live_client.app.state.live
        manager.worker_factory = SlowWorker
        socket = Socket()
        session = asyncio.create_task(manager.handle(socket, 'trained'))
        try:
            await socket.incoming.put(packet(1))
            await until(lambda: manager.active and manager.active['worker'] and manager.active['worker'].request is not None)
            await socket.incoming.put(packet(2))
            await socket.incoming.put(packet(3))
            await until(lambda: socket.consumed == 3)
            gate.set()
            await until(lambda: len(socket.frames) == 2)
            assert socket.frames == [1, 3] and rendered == [1, 3]
        finally:
            await socket.incoming.put(None)
            await asyncio.wait_for(session, 2)
        assert manager.active is None

    asyncio.run(scenario())


def test_import_live_transport_never_imports_torch():
    result = subprocess.run([sys.executable, '-c',
        'import sys; import minicolmap.ui.live; import minicolmap.rendering.live; assert "torch" not in sys.modules'],
        capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
