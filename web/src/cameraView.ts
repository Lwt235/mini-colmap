import * as THREE from 'three';
import type { Camera } from './types';

/** A finite transition; only the view changes, never the reconstructed camera. */
export class CameraTransition {
  private readonly from: THREE.PerspectiveCamera;
  private readonly to: THREE.PerspectiveCamera;
  readonly target = new THREE.Vector3();

  constructor(
    camera: THREE.PerspectiveCamera,
    destination: THREE.PerspectiveCamera,
    private readonly fromDistance: number,
    private readonly toDistance: number,
    private readonly startedAt: number,
    private readonly duration = 750,
  ) {
    // Capture the current pose so another alignment can replace an unfinished one.
    this.from = camera.clone();
    this.to = destination.clone();
  }

  /** Apply one frame, returning true at the exact destination. Keep the live aspect. */
  update(camera: THREE.PerspectiveCamera, now: number) {
    const progress = THREE.MathUtils.clamp((now - this.startedAt) / this.duration, 0, 1);
    const eased = progress * progress * (3 - 2 * progress);
    camera.position.lerpVectors(this.from.position, this.to.position, eased);
    camera.quaternion.slerpQuaternions(this.from.quaternion, this.to.quaternion, eased);
    camera.up.set(0, 1, 0).applyQuaternion(camera.quaternion);
    camera.fov = THREE.MathUtils.lerp(this.from.fov, this.to.fov, eased);
    camera.zoom = THREE.MathUtils.lerp(this.from.zoom, this.to.zoom, eased);
    camera.near = THREE.MathUtils.lerp(this.from.near, this.to.near, eased);
    camera.far = THREE.MathUtils.lerp(this.from.far, this.to.far, eased);
    camera.updateProjectionMatrix();
    camera.updateMatrixWorld(true);
    this.target
      .set(0, 0, -1)
      .applyQuaternion(camera.quaternion)
      .multiplyScalar(THREE.MathUtils.lerp(this.fromDistance, this.toDistance, eased))
      .add(camera.position);
    return progress === 1;
  }
}

/** Align the free viewer to a saved pose, using its vertical field of view. */
export function alignedView(source: Camera, aspect: number, near: number, far: number) {
  if (!source.registered || !source.R || !source.t) return null;
  const { R, t, K, height } = source;
  const camera = new THREE.PerspectiveCamera();
  const center = [0, 1, 2].map((i) => -R.reduce((sum, row, j) => sum + row[i] * t[j], 0));
  // OpenCV +Y down/+Z forward -> Three +Y up/-Z forward. Columns are world axes.
  const world = new THREE.Matrix4().set(
    R[0][0],
    -R[1][0],
    -R[2][0],
    center[0],
    R[0][1],
    -R[1][1],
    -R[2][1],
    center[1],
    R[0][2],
    -R[1][2],
    -R[2][2],
    center[2],
    0,
    0,
    0,
    1,
  );
  camera.position.fromArray(center);
  camera.quaternion.setFromRotationMatrix(world);
  camera.up.set(-R[1][0], -R[1][1], -R[1][2]);
  camera.near = near;
  camera.far = far;
  camera.aspect = aspect;
  camera.fov = THREE.MathUtils.radToDeg(2 * Math.atan((height * K[2][2]) / (2 * K[1][1])));
  camera.updateProjectionMatrix();
  camera.updateMatrixWorld(true);
  return camera;
}
