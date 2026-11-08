"""UI command parsing without importing server dependencies or opening a browser."""

from pathlib import Path

import pytest

from minicolmap.cli import main


@pytest.fixture
def ui_calls(monkeypatch):
    from minicolmap import ui

    calls = []
    monkeypatch.setattr(ui, 'run', lambda **kwargs: calls.append(kwargs))
    return calls


def test_ui_defaults_are_forwarded_to_lazy_runner(ui_calls):
    assert main(['ui']) == 0
    assert ui_calls == [{'port': 8765, 'open_browser': True, 'project_root': None}]


@pytest.mark.parametrize('port', [1, 8766, 65535])
def test_ui_accepts_valid_port_boundaries_and_custom_port(port, ui_calls):
    assert main(['ui', '--port', str(port)]) == 0
    assert ui_calls[0]['port'] == port


@pytest.mark.parametrize('port', [0, -1, 65536])
def test_ui_rejects_out_of_range_port_without_starting_runner(port, ui_calls, capsys):
    assert main(['ui', '--port', str(port)]) == 1
    assert not ui_calls
    assert '--port must be between 1 and 65535' in capsys.readouterr().err


def test_ui_rejects_noninteger_port_before_starting_runner(ui_calls, capsys):
    with pytest.raises(SystemExit) as exc:
        main(['ui', '--port', 'not-a-number'])
    assert exc.value.code == 2
    assert not ui_calls
    assert 'invalid int value' in capsys.readouterr().err


def test_ui_no_browser_and_project_are_forwarded_without_server_start(ui_calls):
    project = Path('data') / 'project with spaces'
    assert main(['ui', '--no-browser', '--project', str(project), '--port', '9001']) == 0
    assert ui_calls == [{'port': 9001, 'open_browser': False, 'project_root': project}]
    assert isinstance(ui_calls[0]['project_root'], Path)


def test_ui_optional_dependency_error_returns_actionable_cli_error(monkeypatch, capsys):
    from minicolmap import ui

    def unavailable(**kwargs):
        raise RuntimeError('Install the optional UI dependencies with .[ui]')

    monkeypatch.setattr(ui, 'run', unavailable)
    assert main(['ui', '--no-browser']) == 1
    assert '.[ui]' in capsys.readouterr().err
