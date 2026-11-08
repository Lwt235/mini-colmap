"""UTF-8 project records work independently of the Windows system code page."""
from pathlib import Path

from minicolmap.ui.demo import read
from minicolmap.ui.worker import atomic_json


def ascii_default(monkeypatch):
    original = Path.read_text

    def read_text(path, *args, **kwargs):
        if not args:
            kwargs.setdefault('encoding', 'ascii')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', read_text)


def test_demo_reads_utf8_written_by_worker(tmp_path, monkeypatch):
    path = tmp_path / 'progress.json'
    value = {'message': '正在生成三维选区'}
    atomic_json(path, value)
    ascii_default(monkeypatch)
    assert read(path) == value


def test_exercise_status_reads_utf8_source(tmp_path, monkeypatch):
    from minicolmap.semantics import algorithms
    from minicolmap.semantics.project import exercise_status

    path = tmp_path / 'algorithms.py'
    path.write_text('def region_features():\n    """L1: 区域特征。"""\n    return 1\n', encoding='utf-8')
    monkeypatch.setattr(algorithms, '__file__', str(path))
    ascii_default(monkeypatch)
    assert exercise_status() == [
        {'name': 'region_features', 'id': 'L1', 'status': 'implemented_unverified'}
    ]
