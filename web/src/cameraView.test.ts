import { describe, expect, it } from 'vitest';
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import type { Camera } from './types';
import { alignedView, CameraTransition } from './cameraView';

const source: Camera = {
  image_id: 13,
  camera_id: 1,
  name: 'frame.png',
  width: 640,
  height: 480,
  registered: true,
  observations: 20,
  K: [
    [730, 17, 287],
    [0, 810, 253],
    [0, 0, 1],
  ],
  R: [
    [1, 0, 0],
    [0, 1, 0],
    [0, 0, 1],
  ],
  t: [0, 0, 0],
};

describe('camera alignment for a free viewpoint', () => {
  it.each([
    source,
    {
      ...source,
      R: [
        [0, 0, 1],
        [0, 1, 0],
        [-1, 0, 0],
      ],
      t: [-3, -2, 1],
    },
    {
      ...source,
      R: [
        [1, 0, 0],
        [0, 0, -1],
        [0, 1, 0],
      ],
      t: [2, -1, 3],
      K: source.K.map((row) => row.map((x) => x * 2)),
    },
  ])('preserves the saved position, direction and roll for R=$R', (model) => {
    const view = alignedView(model, 16 / 9, 0.001, 1000)!;
    for (const p of [
      [-0.3, 0.2, 3],
      [0.4, -0.1, 4],
      [0, 0, 2],
    ]) {
      // Start from a known point in camera coordinates and transform into world coordinates.
      const world = [0, 1, 2].map((i) =>
        model.R!.reduce((sum, row, j) => sum + row[i] * (p[j] - model.t![j]), 0),
      );
      const local = new THREE.Vector3(...world).applyMatrix4(view.matrixWorldInverse);
      [local.x, local.y, local.z].forEach((value, i) =>
        expect(value).toBeCloseTo(p[i] * (i === 0 ? 1 : -1), 8),
      );
      const ndc = new THREE.Vector3(...world).project(view);
      expect(ndc.z).toBeGreaterThan(-1);
      expect(ndc.z).toBeLessThan(1);
    }
    const centerInCamera = model.R!.map((row, i) =>
      row.reduce((sum, value, j) => sum + value * view.position.getComponent(j), model.t![i]),
    );
    centerInCamera.forEach((x) => expect(x).toBeCloseTo(0));
  });
  it('does not invent a viewpoint for an unregistered photo', () => {
    expect(
      alignedView(
        { ...source, registered: false, R: undefined, t: undefined },
        16 / 9,
        0.001,
        1000,
      ),
    ).toBeNull();
  });
  it('uses a responsive full-panel perspective without changing the aligned pose on resize', () => {
    const view = alignedView(source, 16 / 9, 0.001, 1000)!;
    const position = view.position.clone(),
      rotation = view.quaternion.clone();
    expect(view.aspect).toBe(16 / 9);
    view.aspect = 4 / 3;
    view.updateProjectionMatrix();
    expect(view.position.equals(position)).toBe(true);
    expect(view.quaternion.equals(rotation)).toBe(true);
    const pixelAspect = view.projectionMatrix.elements[5] / view.projectionMatrix.elements[0];
    expect(pixelAspect).toBeCloseTo(4 / 3);
  });
});

describe('smooth camera alignment', () => {
  function freeView() {
    const camera = new THREE.PerspectiveCamera(45, 16 / 9, 0.001, 1000);
    camera.up.set(0, -1, 0);
    camera.position.set(3, -2, -4);
    camera.lookAt(0, 0, 0);
    return camera;
  }

  it('moves and rotates through intermediate poses, easing at both ends', () => {
    const camera = freeView(),
      initial = camera.clone(),
      destination = alignedView(source, camera.aspect, 0.01, 2000)!,
      motion = new CameraTransition(camera, destination, 5, 2, 100);
    expect(motion.update(camera, 100)).toBe(false);
    expect(camera.position.distanceTo(initial.position)).toBeCloseTo(0, 12);
    expect(camera.quaternion.angleTo(initial.quaternion)).toBeLessThan(1e-7);
    motion.update(camera, 175);
    const firstDistance = camera.position.distanceTo(initial.position);
    expect(firstDistance).toBeGreaterThan(0);
    expect(firstDistance).toBeLessThan(initial.position.distanceTo(destination.position) * 0.1);
    motion.update(camera, 475);
    expect(camera.position.distanceTo(initial.position)).toBeCloseTo(
      initial.position.distanceTo(destination.position) / 2,
      10,
    );
    expect(camera.quaternion.angleTo(initial.quaternion)).toBeGreaterThan(0.1);
    expect(camera.quaternion.angleTo(destination.quaternion)).toBeGreaterThan(0.1);
    motion.update(camera, 775);
    expect(camera.position.distanceTo(destination.position)).toBeCloseTo(firstDistance, 10);
    expect(motion.update(camera, 850)).toBe(true);
    expect(camera.position.distanceTo(destination.position)).toBeCloseTo(0, 12);
    expect(camera.quaternion.angleTo(destination.quaternion)).toBeLessThan(1e-7);
    expect(camera.fov).toBeCloseTo(destination.fov, 12);
    expect(camera.near).toBeCloseTo(destination.near, 12);
    expect(camera.far).toBeCloseTo(destination.far, 12);
    expect(camera.position.distanceTo(motion.target)).toBeCloseTo(2, 12);
  });

  it('takes the short rotation across the angle wrap', () => {
    const camera = freeView(),
      destination = camera.clone(),
      axis = new THREE.Vector3(0, 1, 0);
    camera.quaternion.setFromAxisAngle(axis, THREE.MathUtils.degToRad(179));
    destination.quaternion.setFromAxisAngle(axis, THREE.MathUtils.degToRad(-179));
    const motion = new CameraTransition(camera, destination, 2, 2, 0);
    motion.update(camera, 375);
    const halfway = new THREE.Quaternion().setFromAxisAngle(axis, Math.PI);
    expect(camera.quaternion.angleTo(halfway)).toBeLessThan(1e-7);
  });

  it('preserves live resizing while the transition is running', () => {
    const camera = freeView(),
      motion = new CameraTransition(
        camera,
        alignedView(source, camera.aspect, 0.01, 2000)!,
        5,
        2,
        0,
      );
    motion.update(camera, 100);
    camera.aspect = 4 / 3;
    motion.update(camera, 750);
    expect(camera.aspect).toBe(4 / 3);
    expect(camera.projectionMatrix.elements[5] / camera.projectionMatrix.elements[0]).toBeCloseTo(
      4 / 3,
    );
  });

  it('starts a replacement transition at the visible pose without moving backwards', () => {
    const camera = freeView(),
      destination = alignedView(source, camera.aspect, 0.01, 2000)!,
      first = new CameraTransition(camera, destination, 5, 2, 0);
    first.update(camera, 250);
    const visible = camera.clone();
    const nextDestination = destination.clone();
    nextDestination.position.set(-2, 3, 1);
    nextDestination.rotateZ(0.7);
    const replacement = new CameraTransition(camera, nextDestination, 4, 3, 250);
    replacement.update(camera, 250);
    expect(camera.position.distanceTo(visible.position)).toBeCloseTo(0, 12);
    expect(camera.quaternion.angleTo(visible.quaternion)).toBeLessThan(1e-7);
    replacement.update(camera, 1000);
    expect(camera.position.distanceTo(nextDestination.position)).toBeCloseTo(0, 12);
    expect(camera.quaternion.angleTo(nextDestination.quaternion)).toBeLessThan(1e-7);
    // The saved destination is independent of all viewer mutations.
    expect(destination.position.length()).toBe(0);
  });

  it.each([300, 750])('hands off to free orbit without a jump or roll drift at %d ms', (time) => {
    const camera = freeView(),
      destination = alignedView(
        {
          ...source,
          R: [
            [0, 0, 1],
            [1, 0, 0],
            [0, 1, 0],
          ],
          t: [-3, 2, 1],
        },
        camera.aspect,
        0.01,
        2000,
      )!,
      motion = new CameraTransition(camera, destination, 5, 2, 0);
    motion.update(camera, time);
    const position = camera.position.clone(),
      rotation = camera.quaternion.clone();
    // Match the production handoff: cache the current up axis in fresh controls.
    const controls = new OrbitControls(camera);
    camera.position.copy(position);
    controls.target.copy(motion.target);
    for (let i = 0; i < 120; i++) controls.update();
    expect(camera.position.distanceTo(position)).toBeLessThan(1e-10);
    expect(camera.quaternion.angleTo(rotation)).toBeLessThan(1e-7);
  });
});
