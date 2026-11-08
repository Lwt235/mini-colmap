"""Wall-clock accounting only; no numerical work or scheduling policy."""
from contextlib import contextmanager
from time import perf_counter


class PipelineTimer:
    def __init__(self, clock=perf_counter):
        self.clock = clock
        self.started = self.stage_started = clock()
        self.active_stage = 'dataset_loading'
        self.stages = {}
        self.details = {}
        self.ended = None
        self.status = 'running'

    def stage(self, name):
        now = self.clock()
        self.stages[self.active_stage] = self.stages.get(self.active_stage, 0.) + now - self.stage_started
        self.active_stage, self.stage_started = name, now

    @contextmanager
    def measure(self, name):
        started = self.clock()
        try:
            yield
        finally:
            item = self.details.setdefault(name, {'seconds': 0., 'calls': 0})
            item['seconds'] += self.clock() - started
            item['calls'] += 1

    def finish(self, status):
        if self.ended is None:
            self.ended = self.clock()
            self.stages[self.active_stage] = self.stages.get(self.active_stage, 0.) + self.ended - self.stage_started
            self.active_stage = None
        self.status = status

    def report(self):
        now = self.clock() if self.ended is None else self.ended
        stages = self.stages.copy()
        if self.active_stage is not None:
            stages[self.active_stage] = stages.get(self.active_stage, 0.) + now - self.stage_started
        return {'status': self.status, 'unit': 'seconds', 'total_seconds': now - self.started,
                'stages_seconds': stages,
                'details': {name: dict(item) for name, item in self.details.items()}}
