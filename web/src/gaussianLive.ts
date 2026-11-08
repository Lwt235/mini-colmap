import * as THREE from 'three';
import type { Camera, Scene } from './types';
import type { RenderCamera } from './rendering';

export type LiveCalibration = {
  fx: number;
  fy: number;
  skew: number;
  offsetX: number;
  offsetY: number;
};

/** Store focal lengths in image-height units; resizing preserves vertical field of view. */
export function cameraCalibration(source: Pick<Camera, 'K' | 'width' | 'height'>): LiveCalibration {
  const { K, width, height } = source;
  return {
    fx: K[0][0] / height,
    fy: K[1][1] / height,
    skew: K[0][1] / height,
    offsetX: (K[0][2] + 0.5 - width / 2) / height,
    offsetY: (K[1][2] + 0.5 - height / 2) / height,
  };
}

export function liveIntrinsics(calibration: LiveCalibration, width: number, height: number) {
  return [
    [
      calibration.fx * height,
      calibration.skew * height,
      (width - 1) / 2 + calibration.offsetX * height,
    ],
    [0, calibration.fy * height, (height - 1) / 2 + calibration.offsetY * height],
    [0, 0, 1],
  ];
}

/** Exact CV intrinsics, including integer pixel centres and an off-axis principal point. */
export function setLiveProjection(
  camera: THREE.PerspectiveCamera,
  K: number[][],
  width: number,
  height: number,
) {
  camera.aspect = width / height;
  camera.fov = THREE.MathUtils.radToDeg(2 * Math.atan(height / (2 * K[1][1])));
  const { near, far } = camera;
  camera.projectionMatrix.set(
    (2 * K[0][0]) / width,
    (-2 * K[0][1]) / width,
    1 - (2 * (K[0][2] + 0.5)) / width,
    0,
    0,
    (2 * K[1][1]) / height,
    (2 * (K[1][2] + 0.5)) / height - 1,
    0,
    0,
    0,
    -(far + near) / (far - near),
    (-2 * far * near) / (far - near),
    0,
    0,
    -1,
    0,
  );
  camera.projectionMatrixInverse.copy(camera.projectionMatrix).invert();
}

export function liveRenderCamera(
  camera: THREE.PerspectiveCamera,
  K: number[][],
  width: number,
  height: number,
): RenderCamera {
  camera.updateMatrixWorld(true);
  const e = camera.matrixWorldInverse.elements;
  return {
    width,
    height,
    K: K.map((row) => [...row]),
    R: [
      [e[0], e[4], e[8]],
      [-e[1], -e[5], -e[9]],
      [-e[2], -e[6], -e[10]],
    ],
    t: [e[12], -e[13], -e[14]],
  };
}

export function liveOutputSize(
  width: number,
  height: number,
  maxEdge: number,
  pixelRatio = 1,
  maxHeight = maxEdge,
) {
  width *= pixelRatio;
  height *= pixelRatio;
  const scale = Math.min(1, maxEdge / width, maxHeight / height);
  return {
    width: Math.max(1, Math.round(width * scale)),
    height: Math.max(1, Math.round(height * scale)),
  };
}

/** Preserve the final viewport; reduce pixels only during pointer interaction. */
export function liveQualityTarget(width: number, height: number, interactive: boolean) {
  const scale = interactive ? Math.min(0.6, 960 / width, 540 / height) : 1;
  return {
    width: Math.max(1, Math.round(width * scale)),
    height: Math.max(1, Math.round(height * scale)),
    quality: interactive ? ('interactive' as const) : ('final' as const),
  };
}

/** Robust initial orbit centre; isolated sparse points should not put the model out of view. */
export function liveSceneBounds(scene: Scene | null) {
  const values = scene?.points.positions ?? [];
  const axes: number[][] = [[], [], []];
  for (let i = 0; i + 2 < values.length; i += 3) {
    if (values.slice(i, i + 3).every(Number.isFinite)) {
      for (let axis = 0; axis < 3; axis++) axes[axis].push(values[i + axis]);
    }
  }
  if (!axes[0].length) return { center: new THREE.Vector3(), radius: 1 };
  axes.forEach((axis) => axis.sort((a, b) => a - b));
  const low = axes.map((axis) => axis[Math.floor((axis.length - 1) * 0.02)]);
  const high = axes.map((axis) => axis[Math.ceil((axis.length - 1) * 0.98)]);
  const center = new THREE.Vector3(...low).add(new THREE.Vector3(...high)).multiplyScalar(0.5);
  return {
    center,
    radius: Math.max(new THREE.Vector3(...high).distanceTo(new THREE.Vector3(...low)) / 2, 1e-4),
  };
}

/** At most two in-flight frames; pointer input replaces the single pending pose. */
export class LatestFrameScheduler<T> {
  private revision = 0;
  private sequence = 0;
  private generation = 0;
  private displayedSequence = 0;
  private pending: { revision: number; value: T } | null = null;
  private flights = new Map<number, { seq: number; revision: number; sentAt: number; value: T }>();

  constructor(private readonly capacity = 1) {
    if (!Number.isInteger(capacity) || capacity < 1 || capacity > 2)
      throw new Error('Live capacity must be 1 or 2');
  }
  get inFlight() {
    return this.flights.size;
  }

  queue(value: T) {
    this.pending = { revision: ++this.revision, value };
  }
  begin(now: number) {
    if (this.flights.size >= this.capacity || !this.pending) return null;
    const pending = this.pending;
    this.pending = null;
    const flight = {
      seq: ++this.sequence,
      revision: pending.revision,
      sentAt: now,
      value: pending.value,
    };
    this.flights.set(flight.seq, flight);
    return { ...flight, value: pending.value };
  }
  finish(seq: number, now: number) {
    const frame = this.flights.get(seq);
    if (!frame) return null;
    // The server keeps only its latest pending pose, so a newer reply also
    // acknowledges any older request it skipped. Never wait forever for it.
    for (const key of this.flights.keys()) if (key <= seq) this.flights.delete(key);
    return {
      ...frame,
      generation: this.generation,
      display: frame.seq > this.displayedSequence,
      latencyMs: now - frame.sentAt,
    };
  }
  /** New pointer input must not starve display; reject older decodes and previous connections only. */
  acceptDisplay(seq: number, generation: number) {
    if (generation !== this.generation || seq <= this.displayedSequence) return false;
    this.displayedSequence = seq;
    return true;
  }
  reset() {
    this.pending = null;
    this.flights.clear();
    ++this.revision;
    ++this.generation;
  }
}

/** Coalesce decoded network frames into at most one image update per browser paint. */
export class LatestPaintQueue<T extends { seq: number }> {
  private pending: T | null = null;
  private frame: number | null = null;
  private disposed = false;

  constructor(
    private readonly present: (value: T) => void,
    private readonly discard: (value: T) => void,
    private readonly requestFrame: (callback: () => void) => number,
    private readonly cancelFrame: (id: number) => void,
  ) {}

  queue(value: T) {
    if (this.disposed || (this.pending && value.seq <= this.pending.seq)) {
      this.discard(value);
      return;
    }
    if (this.pending) this.discard(this.pending);
    this.pending = value;
    if (this.frame === null) {
      this.frame = this.requestFrame(() => {
        this.frame = null;
        const latest = this.pending;
        this.pending = null;
        if (latest) this.present(latest);
      });
    }
  }

  clear() {
    if (this.frame !== null) this.cancelFrame(this.frame);
    this.frame = null;
    if (this.pending) this.discard(this.pending);
    this.pending = null;
  }

  dispose() {
    this.disposed = true;
    this.clear();
  }
}
