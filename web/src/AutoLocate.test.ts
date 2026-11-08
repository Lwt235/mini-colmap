import { expect, it } from 'vitest';
import { autoLocationReady, matchingAutoLocation, type AutoLocateJob } from './AutoLocate';
const job: AutoLocateJob = {
  id: 'a',
  status: 'completed',
  request: { model: 'raw3', text: 'sink' },
  message: 'done',
  report_url: '',
};
it('only auto locates a completed result matching the current text and model', () => {
  expect(autoLocationReady(job, ' SINK ', 'raw3')).toBe(true);
  expect(autoLocationReady(job, 'knife', 'raw3')).toBe(false);
  expect(autoLocationReady(job, 'sink', 'reference')).toBe(false);
  for (const status of ['running', 'needs_choice', 'rejected', 'failed'] as const)
    expect(autoLocationReady({ ...job, status }, 'sink', 'raw3')).toBe(false);
});

it('keeps previous status and candidates scoped to the current query and model', () => {
  const choice = { ...job, status: 'needs_choice' as const };
  expect(matchingAutoLocation(choice, ' SINK ', 'raw3')).toBe(true);
  expect(matchingAutoLocation(choice, 'knife', 'raw3')).toBe(false);
  expect(matchingAutoLocation(choice, 'sink', 'reference')).toBe(false);
  expect(
    autoLocationReady({ ...job, request: { model: 'reference', text: 'sink' } }, 'sink', 'raw3'),
  ).toBe(false);
});
