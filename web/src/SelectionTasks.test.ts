import { describe, expect, it } from 'vitest';
import { canonicalText, matchingSelection } from './SelectionTasks';
import type { ObjectSelection } from './objectSelection';

const sample: ObjectSelection = {
  id: 'old',
  name: '水槽',
  text: 'sink',
  anchor_image_id: 124,
  selected_gaussians: 100,
  center: [0, 0, 1],
  bounds: { min: [0, 0, 0], max: [1, 1, 1] },
  export_url: '',
  inspection_url: '',
  view_ids: [],
  status: 'prototype',
};
describe('text-to-3D query binding', () => {
  it('matches normalized text and the exact anchor, never another object or camera', () => {
    expect(canonicalText(' Dark   CUP ')).toBe('dark cup');
    expect(matchingSelection([sample], ' SINK ', 124)?.id).toBe('old');
    expect(matchingSelection([sample], 'sink', 62)).toBeUndefined();
    expect(matchingSelection([sample], 'knife', 124)).toBeUndefined();
  });
  it('prefers a generated matching selection over the historical preset', () => {
    expect(matchingSelection([sample, { ...sample, id: 'new' }], 'sink', 124)?.id).toBe('new');
  });
});
