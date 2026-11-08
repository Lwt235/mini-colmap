import { describe, it, expect } from 'vitest';
import { cameraCenter, frustumCorners, visiblePointIndices } from './math';
describe('camera geometry uses world-to-camera convention', () => {
  it('keeps +Z forward and +Y down for the identity pose', () => {
    const R = [
        [1, 0, 0],
        [0, 1, 0],
        [0, 0, 1],
      ],
      t = [0, 0, 0],
      K = [
        [100, 0, 50],
        [0, 100, 40],
        [0, 0, 1],
      ];
    expect(cameraCenter(R, t)).toEqual([-0, -0, -0]);
    expect(frustumCorners({ K, R, t, width: 100, height: 80 }, 2)).toEqual([
      [-1, -0.8, 2],
      [1, -0.8, 2],
      [1, 0.8, 2],
      [-1, 0.8, 2],
    ]);
  });
  it('uses the inverse rotation rather than a mirrored camera orientation', () => {
    const R = [
        [0, 0, 1],
        [0, 1, 0],
        [-1, 0, 0],
      ],
      t = [-3, -2, 1],
      K = [
        [100, 0, 50],
        [0, 100, 40],
        [0, 0, 1],
      ],
      C = cameraCenter(R, t);
    expect(C).toEqual([1, 2, 3]);
    const p = frustumCorners({ K, R, t, width: 100, height: 80 }, 2)[0];
    const q = R.map((row, i) => row.reduce((sum, x, j) => sum + x * p[j], t[i]));
    expect(q).toEqual([-1, -0.8, 2]);
  });
  it('supports skewed intrinsics', () => {
    const R = [
        [1, 0, 0],
        [0, 1, 0],
        [0, 0, 1],
      ],
      t = [0, 0, 0],
      K = [
        [100, 10, 50],
        [0, 100, 40],
        [0, 0, 1],
      ];
    expect(frustumCorners({ K, R, t, width: 100, height: 80 }, 2)[0][0]).toBeCloseTo(-0.92);
  });
});
it('display filtering preserves original point indices and excludes unknown errors only for an enabled error filter', () => {
  expect(visiblePointIndices([0.2, 8, null], [2, 4, 7], 3, 1, 2)).toEqual([0]);
  expect(visiblePointIndices([0.2, 8, null], [2, 4, 7], 3, 0, 4)).toEqual([1, 2]);
});
