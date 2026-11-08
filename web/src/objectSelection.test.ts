import { describe, it, expect } from 'vitest';
import { selectionOverlay, type ObjectSelection } from './objectSelection';
const selection: ObjectSelection = {
  id: 'sink',
  name: '水槽',
  text: 'sink',
  selected_gaussians: 3,
  center: [0, 0, 3],
  bounds: { min: [-1, -1, 2], max: [1, 1, 4] },
  export_url: '',
  inspection_url: '',
  view_ids: [],
  anchor_image_id: 124,
  status: 'prototype',
};
const camera = {
  width: 100,
  height: 80,
  K: [
    [50, 0, 49.5],
    [0, 50, 39.5],
    [0, 0, 1],
  ],
  R: [
    [1, 0, 0],
    [0, 1, 0],
    [0, 0, 1],
  ],
  t: [0, 0, 0],
};
describe('3D selection overlays', () => {
  it('projects all twelve edges and the center with integer pixel centers', () => {
    const overlay = selectionOverlay(selection, camera);
    expect(overlay.lines).toHaveLength(12);
    expect(overlay.center).toEqual([0.5, 0.5]);
  });
  it('uses the returned frame camera even after the user has moved', () => {
    const older = selectionOverlay(selection, camera);
    const moved = selectionOverlay(selection, { ...camera, t: [1, 0, 0] });
    expect(older.center?.[0]).toBe(0.5);
    expect(moved.center?.[0]).toBeGreaterThan(0.5);
    expect(selectionOverlay(selection, { ...camera, t: [0, 0, -10] }).lines).toHaveLength(0);
  });
});
