import { describe, expect, it } from 'vitest';
import * as THREE from 'three';
import { alignedView } from './cameraView';
import type { Camera } from './types';
import {
  cameraCalibration,
  LatestFrameScheduler,
  LatestPaintQueue,
  liveIntrinsics,
  liveOutputSize,
  liveQualityTarget,
  liveRenderCamera,
  setLiveProjection,
} from './gaussianLive';

const source: Camera = {
  image_id: 7,
  camera_id: 1,
  name: 'saved.png',
  registered: true,
  observations: 10,
  width: 640,
  height: 480,
  K: [
    [610, 0, 312.1],
    [0, 620, 242.7],
    [0, 0, 1],
  ],
  R: [
    [1, 0, 0],
    [0, 1, 0],
    [0, 0, 1],
  ],
  t: [-2, 3, -4],
};

function checkPose(saved: Camera) {
  const camera = alignedView(saved, saved.width / saved.height, 0.01, 1000)!;
  setLiveProjection(camera, saved.K, saved.width, saved.height);
  const result = liveRenderCamera(camera, saved.K, saved.width, saved.height);
  for (let i = 0; i < 3; i++) {
    expect(result.t[i]).toBeCloseTo(saved.t![i], 10);
    for (let j = 0; j < 3; j++) expect(result.R[i][j]).toBeCloseTo(saved.R![i][j], 10);
  }
  const point = new THREE.Vector3(0.2, -0.3, 2);
  // Put a CV-space point in world space; Three's projection must match K exactly.
  const cv = point.clone();
  const R = new THREE.Matrix3().set(
    ...(saved.R!.flat() as [
      number,
      number,
      number,
      number,
      number,
      number,
      number,
      number,
      number,
    ]),
  );
  point.sub(new THREE.Vector3(...saved.t!)).applyMatrix3(R.transpose());
  const projected = point.project(camera);
  const pixel = [
    ((projected.x + 1) * saved.width) / 2 - 0.5,
    ((1 - projected.y) * saved.height) / 2 - 0.5,
  ];
  expect(pixel[0]).toBeCloseTo((saved.K[0][0] * cv.x) / cv.z + saved.K[0][2], 8);
  expect(pixel[1]).toBeCloseTo((saved.K[1][1] * cv.y) / cv.z + saved.K[1][2], 8);
}

describe('live Gaussian camera', () => {
  it('round trips a translated identity CV pose without a mirrored image', () => checkPose(source));
  it('round trips a rotated CV pose and preserves the off-axis principal point', () => {
    const rotation = new THREE.Matrix4().makeRotationFromEuler(new THREE.Euler(0.4, -0.65, 0.17));
    const e = rotation.elements;
    checkPose({
      ...source,
      R: [
        [e[0], e[4], e[8]],
        [e[1], e[5], e[9]],
        [e[2], e[6], e[10]],
      ],
    });
  });
  it('scales integer pixel centres and preserves vertical field of view on resize', () => {
    const calibration = cameraCalibration(source);
    const half = liveIntrinsics(calibration, 320, 240);
    expect(half[0][0]).toBe(305);
    expect(half[0][2]).toBeCloseTo((source.K[0][2] + 0.5) / 2 - 0.5);
    const wide = liveIntrinsics(calibration, 1280, 480);
    expect(wide[1][1]).toBe(source.K[1][1]);
    expect(wide[0][2]).toBeCloseTo(source.K[0][2] + 320);
    expect(liveIntrinsics(calibration, 640, 480)).toEqual(source.K);
  });
  it('bounds output dimensions without upscaling a small viewport', () => {
    expect(liveOutputSize(1600, 900, 1280)).toEqual({ width: 1280, height: 720 });
    expect(liveOutputSize(320, 240, 640)).toEqual({ width: 320, height: 240 });
  });
  it('uses physical pixels on HiDPI displays within both server limits', () => {
    expect(liveOutputSize(640, 360, 1280, 2, 720)).toEqual({ width: 1280, height: 720 });
    expect(liveOutputSize(960, 540, 1920, 2, 1080)).toEqual({ width: 1920, height: 1080 });
    expect(liveOutputSize(600, 1000, 1920, 2, 1080)).toEqual({ width: 648, height: 1080 });
  });
});

describe('decoded Gaussian frame presentation', () => {
  function fixture() {
    const presented: number[] = [];
    const discarded: number[] = [];
    const callbacks = new Map<number, () => void>();
    let next = 0;
    const painter = new LatestPaintQueue<{ seq: number }>(
      (value) => presented.push(value.seq),
      (value) => discarded.push(value.seq),
      (callback) => {
        callbacks.set(++next, callback);
        return next;
      },
      (id) => {
        callbacks.delete(id);
      },
    );
    const paint = () => {
      const current = [...callbacks.values()];
      callbacks.clear();
      current.forEach((callback) => callback());
    };
    return { painter, presented, discarded, callbacks, paint };
  }

  it('counts only the latest decoded image once per paint', () => {
    const { painter, presented, discarded, callbacks, paint } = fixture();
    painter.queue({ seq: 1 });
    painter.queue({ seq: 2 });
    painter.queue({ seq: 3 });
    expect(callbacks.size).toBe(1);
    expect(presented).toEqual([]);
    expect(discarded).toEqual([1, 2]);
    paint();
    expect(presented).toEqual([3]);
    paint();
    expect(presented).toEqual([3]);
    painter.queue({ seq: 4 });
    paint();
    expect(presented).toEqual([3, 4]);
  });

  it('does not replace a newer pending decode with an older image', () => {
    const { painter, presented, discarded, paint } = fixture();
    painter.queue({ seq: 2 });
    painter.queue({ seq: 1 });
    paint();
    expect(presented).toEqual([2]);
    expect(discarded).toEqual([1]);
  });

  it('cancels paint callbacks and releases pending images on close', () => {
    const { painter, presented, discarded, callbacks, paint } = fixture();
    painter.queue({ seq: 1 });
    painter.clear();
    expect(callbacks.size).toBe(0);
    painter.queue({ seq: 2 });
    painter.dispose();
    painter.queue({ seq: 3 });
    paint();
    expect(presented).toEqual([]);
    expect(discarded).toEqual([1, 2, 3]);
  });
});

describe('latest camera frame scheduling', () => {
  it('bounds pipelined requests and acknowledges poses the server skipped', () => {
    const gate = new LatestFrameScheduler<string>(2);
    gate.queue('first');
    const first = gate.begin(0)!;
    gate.queue('second');
    const second = gate.begin(1)!;
    gate.queue('stale');
    gate.queue('latest');
    expect(gate.begin(2)).toBeNull();
    expect(gate.inFlight).toBe(2);
    expect(gate.finish(second.seq, 30)?.display).toBe(true);
    expect(gate.inFlight).toBe(0);
    expect(gate.finish(first.seq, 31)).toBeNull();
    expect(gate.begin(32)?.value).toBe('latest');
    gate.reset();
    expect(gate.inFlight).toBe(0);
  });
  it('keeps one request in flight and replaces pending views', () => {
    const gate = new LatestFrameScheduler<string>();
    gate.queue('first');
    const first = gate.begin(10)!;
    gate.queue('second');
    gate.queue('latest');
    expect(gate.begin(20)).toBeNull();
    expect(gate.finish(first.seq, 30)?.display).toBe(true);
    const latest = gate.begin(31)!;
    expect(latest.value).toBe('latest');
    expect(gate.finish(latest.seq, 42)).toMatchObject({ display: true, latencyMs: 11 });
    expect(gate.begin(50)).toBeNull();
  });
  it('ignores unrelated responses and rejects decodes older than the displayed sequence', () => {
    const gate = new LatestFrameScheduler<number>();
    gate.queue(1);
    const first = gate.begin(10)!;
    expect(gate.finish(first.seq + 1, 12)).toBeNull();
    const completion = gate.finish(first.seq, 15)!;
    gate.queue(2);
    expect(gate.acceptDisplay(completion.seq, completion.generation)).toBe(true);
    expect(gate.acceptDisplay(completion.seq, completion.generation)).toBe(false);
    const second = gate.begin(16)!;
    const newer = gate.finish(second.seq, 18)!;
    expect(gate.acceptDisplay(newer.seq, newer.generation)).toBe(true);
    expect(gate.acceptDisplay(completion.seq, completion.generation)).toBe(false);
    expect(gate.finish(first.seq, 20)).toBeNull();
  });
  it('drops all pending poses on disconnect and does not reuse a sequence', () => {
    const gate = new LatestFrameScheduler<number>();
    gate.queue(1);
    const first = gate.begin(0)!;
    gate.queue(2);
    gate.reset();
    expect(gate.begin(1)).toBeNull();
    expect(gate.finish(first.seq, 2)).toBeNull();
    gate.queue(3);
    expect(gate.begin(3)!.seq).toBeGreaterThan(first.seq);
  });
  it('continues displaying while every response has a newer camera waiting', () => {
    const gate = new LatestFrameScheduler<number>();
    gate.queue(0);
    for (let index = 0; index < 10; index++) {
      const request = gate.begin(index * 20)!;
      gate.queue(index + 1);
      const frame = gate.finish(request.seq, index * 20 + 15)!;
      expect(frame.display).toBe(true);
      expect(gate.acceptDisplay(frame.seq, frame.generation)).toBe(true);
    }
  });
});

describe('interaction image quality', () => {
  it('reduces motion traffic and restores the exact selected final limit', () => {
    expect(liveQualityTarget(1280, 720, true)).toEqual({
      width: 768,
      height: 432,
      quality: 'interactive',
    });
    expect(liveQualityTarget(1920, 1080, true)).toEqual({
      width: 960,
      height: 540,
      quality: 'interactive',
    });
    expect(liveQualityTarget(1920, 1080, false)).toEqual({
      width: 1920,
      height: 1080,
      quality: 'final',
    });
    expect(liveQualityTarget(640, 480, true)).toEqual({
      width: 384,
      height: 288,
      quality: 'interactive',
    });
  });
});
