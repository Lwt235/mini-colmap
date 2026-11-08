"""Clock-controlled accounting tests; no sleeps or hardware speed assertions."""
import pytest

from minicolmap.timing import PipelineTimer


class Clock:
    def __init__(self):
        self.now = 0.

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def test_stages_partition_total_and_running_reports_do_not_close_stages():
    clock = Clock()
    timer = PipelineTimer(clock)
    clock.advance(2)
    timer.stage('features')
    clock.advance(3)
    first = timer.report()
    assert first['status'] == 'running'
    assert first['stages_seconds'] == {'dataset_loading': 2, 'features': 3}
    first['stages_seconds']['features'] = 999
    clock.advance(1)
    timer.stage('export')
    clock.advance(5)
    timer.finish('completed')
    report = timer.report()
    assert report['stages_seconds'] == {'dataset_loading': 2, 'features': 4, 'export': 5}
    assert report['total_seconds'] == sum(report['stages_seconds'].values()) == 11
    clock.advance(100)
    assert timer.report() == report


def test_details_accumulate_success_and_failure_as_subsets_of_stage_time():
    clock = Clock()
    timer = PipelineTimer(clock)
    timer.stage('pair_geometry')
    with timer.measure('fundamental_ransac'):
        clock.advance(2)
    with pytest.raises(ValueError, match='degenerate'):
        with timer.measure('fundamental_ransac'):
            clock.advance(3)
            raise ValueError('degenerate')
    clock.advance(1)
    timer.finish('failed')
    report = timer.report()
    assert report['details']['fundamental_ransac'] == {'calls': 2, 'seconds': 5}
    assert report['stages_seconds']['pair_geometry'] == report['total_seconds'] == 6
    assert report['status'] == 'failed'


def test_reentered_stage_accumulates_and_reports_do_not_alias_details():
    clock = Clock()
    timer = PipelineTimer(clock)
    timer.stage('features')
    clock.advance(2)
    timer.stage('pair_geometry')
    clock.advance(4)
    timer.stage('features')
    with timer.measure('sift'):
        clock.advance(3)
    timer.report()['details']['sift']['seconds'] = 999
    timer.finish('completed')
    assert timer.report()['stages_seconds']['features'] == 5
    assert timer.report()['details']['sift']['seconds'] == 3
