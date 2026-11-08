import type { Camera } from './types';
export function cameraCenter(R: number[][], t: number[]): number[] {
  return [0, 1, 2].map((i) => -R.reduce((sum, row, j) => sum + row[i] * t[j], 0));
}
export function frustumCorners(
  camera: Pick<Camera, 'K' | 'R' | 't' | 'width' | 'height'>,
  depth: number,
): number[][] {
  if (!camera.R || !camera.t) return [];
  const { K, R, t, width, height } = camera,
    center = cameraCenter(R, t);
  // K may contain skew: solve the upper-triangular calibration matrix exactly.
  return [
    [0, 0],
    [width, 0],
    [width, height],
    [0, height],
  ].map(([u, v]) => {
    const z = 1 / K[2][2],
      y = (v - K[1][2] * z) / K[1][1],
      x = (u - K[0][1] * y - K[0][2] * z) / K[0][0],
      ray = [x, y, z];
    return center.map((c, i) => c + depth * R.reduce((sum, row, j) => sum + row[i] * ray[j], 0));
  });
}
export function visiblePointIndices(
  errors: (number | null)[],
  tracks: number[],
  length: number,
  maxError: number,
  minTrack: number,
): number[] {
  const ids: number[] = [];
  for (let i = 0; i < length; i++) {
    const e = errors[i];
    if ((maxError <= 0 || (e != null && e <= maxError)) && (tracks[i] ?? 0) >= minTrack)
      ids.push(i);
  }
  return ids;
}
