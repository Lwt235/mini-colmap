"""Measure actual CUDA/JPEG/WebSocket frames; this is not browser display FPS.

Uses an existing completed checkpoint through the normal workbench service.
Never trains or modifies the saved model. Run the browser e2e for UI validation.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
import urllib.request
from urllib.parse import urlsplit
from pathlib import Path

import cv2
import numpy as np


def get_json(base, suffix):
    with urllib.request.urlopen(base + suffix, timeout=15) as response:
        return json.load(response)


async def measure(args):
    from websockets.asyncio.client import connect

    scene = get_json(args.base, f'/api/runs/{args.run_id}/scene')
    source = next(camera for camera in scene['cameras'] if camera['registered'])
    R0, t0 = np.array(source['R']), np.array(source['t'])
    center0 = -R0.T @ t0
    points = np.array(scene['points']['positions']).reshape(-1, 3)
    target = np.median(points, axis=0) if len(points) else center0 + R0[2]
    K = np.array(source['K']) / source['K'][2][2]
    ratio = args.height / source['height']
    K[0, 0] *= ratio
    K[1, 1] *= ratio
    K[0, 2] = (K[0, 2] - source['width'] / 2) * ratio + args.width / 2
    K[1, 2] *= ratio
    url = args.base.replace('http://', 'ws://', 1) + f'/api/rendering/runs/{args.run_id}/live'
    samples, first_image = [], None
    async with connect(url, origin=args.base, compression=None, max_size=16 * 1024 * 1024,
                       open_timeout=15, close_timeout=5) as socket:
        ready = json.loads(await asyncio.wait_for(socket.recv(), 65))
        if ready.get('type') != 'ready':
            raise RuntimeError(ready)
        print(json.dumps(ready, ensure_ascii=False), flush=True)
        started, seq = time.perf_counter(), 0
        previous_display = None
        while time.perf_counter() - started < args.warmup + args.duration:
            elapsed = time.perf_counter() - started
            angle = .08 * math.sin(elapsed * .8)
            Q = cv2.Rodrigues(-R0[1] * angle)[0]
            center = target + Q @ (center0 - target)
            R = R0 @ Q.T
            camera = {'width': args.width, 'height': args.height, 'K': K.tolist(),
                      'R': R.tolist(), 't': (-R @ center).tolist()}
            before = time.perf_counter()
            await socket.send(json.dumps({'type': 'camera', 'seq': seq, 'camera': camera}))
            metadata = json.loads(await asyncio.wait_for(socket.recv(), 25))
            if metadata.get('type') != 'frame' or metadata.get('seq') != seq:
                raise RuntimeError(metadata)
            jpeg = await asyncio.wait_for(socket.recv(), 25)
            decoded = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
            if decoded is None or decoded.shape[:2] != (args.height, args.width):
                raise RuntimeError('Invalid actual JPEG dimensions')
            now = time.perf_counter()
            if first_image is None:
                first_image = jpeg
            if elapsed >= args.warmup:
                samples.append({**metadata, 'client_receive_decode_ms': (now - before) * 1000,
                                'interval_ms': (now - previous_display) * 1000 if previous_display else None,
                                'sample_elapsed_seconds': elapsed - args.warmup})
            previous_display = now
            seq += 1
    intervals = [s['interval_ms'] for s in samples if s['interval_ms'] is not None]
    metrics = {}
    for key in ('render_ms', 'render_wall_ms', 'readback_ms', 'encode_ms', 'client_receive_decode_ms'):
        values = [s[key] for s in samples]
        metrics[key] = {'mean': float(np.mean(values)), 'p50': float(np.median(values)),
                        'p95': float(np.percentile(values, 95))}
    report = {'run_id': args.run_id, 'ready': ready, 'resolution': [args.width, args.height],
              'warmup_seconds': args.warmup, 'measurement_seconds': args.duration,
              'actual_frames': len(samples), 'transport_decode_fps': 1000 / float(np.mean(intervals)),
              'interval_p95_ms': float(np.percentile(intervals, 95)), 'metrics': metrics,
              'scope': 'Sequential WebSocket receive + JPEG CPU decode; not browser presentation FPS',
              'samples': samples}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    if first_image:
        args.output.with_suffix('.jpg').write_bytes(first_image)
    print(json.dumps({k: v for k, v in report.items() if k != 'samples'}, indent=2, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', default='http://127.0.0.1:8765')
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--width', type=int, default=1280)
    parser.add_argument('--height', type=int, default=720)
    parser.add_argument('--warmup', type=float, default=10)
    parser.add_argument('--duration', type=float, default=60)
    parser.add_argument('--output', type=Path, default=Path('.cache/gsplat-live-benchmark.json'))
    args = parser.parse_args()
    try:
        base = urlsplit(args.base)
        valid = (base.scheme == 'http' and base.hostname in ('127.0.0.1', 'localhost')
                 and base.username is None and base.password is None
                 and base.path in ('', '/') and not base.query and not base.fragment
                 and (base.port is None or 1 <= base.port <= 65535))
    except ValueError:
        valid = False
    if not valid:
        parser.error('Use an http loopback workbench URL, including an SSH-forwarded local port')
    args.base = args.base.rstrip('/')
    if not (0 <= args.warmup <= 60 and 1 <= args.duration <= 300):
        parser.error('warmup must be 0..60s and duration 1..300s')
    asyncio.run(measure(args))


if __name__ == '__main__':
    main()
