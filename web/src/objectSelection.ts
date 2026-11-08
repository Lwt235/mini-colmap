import type { RenderCamera } from './rendering';

export type ObjectSelection = {
  id: string;
  name: string;
  text: string;
  selected_gaussians: number;
  center: number[];
  bounds: { min: number[]; max: number[] };
  export_url: string;
  inspection_url: string;
  view_ids: number[];
  anchor_image_id: number;
  status: string;
};
export type SelectionMode = 'none' | 'highlight' | 'isolate';
export type BoxOverlay = { lines: number[][]; center: number[] | null };

/** Project with the camera belonging to the displayed frame, never the newest mouse pose. */
export function selectionOverlay(selection: ObjectSelection, camera: RenderCamera): BoxOverlay {
  const project = (point: number[]) => {
    const p = camera.R.map((row, i) => row.reduce((sum, v, j) => sum + v * point[j], camera.t[i]));
    if (p[2] <= 0.001) return null;
    const q = camera.K.map((row) => row.reduce((sum, v, i) => sum + v * p[i], 0));
    return [(q[0] / q[2] + 0.5) / camera.width, (q[1] / q[2] + 0.5) / camera.height];
  };
  const corners = Array.from({ length: 8 }, (_, index) =>
    project(
      [0, 1, 2].map((axis) =>
        index & (1 << axis) ? selection.bounds.max[axis] : selection.bounds.min[axis],
      ),
    ),
  );
  const lines: number[][] = [];
  for (let i = 0; i < 8; i++)
    for (let axis = 0; axis < 3; axis++) {
      const other = i ^ (1 << axis);
      if (i > other) continue;
      const a = corners[i],
        b = corners[other];
      if (a && b) lines.push([...a, ...b]);
    }
  return { lines, center: project(selection.center) };
}
