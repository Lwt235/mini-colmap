"""Read-only semantic status and artifacts never start computation."""
from pathlib import Path
import json
import shutil

from fastapi.testclient import TestClient

from minicolmap.ui.app import create_app


def semantic_client(tmp_path):
    repository = Path(__file__).resolve().parents[2]
    (tmp_path / 'configs').mkdir()
    shutil.copy(repository / 'configs/semantic_waldo.json', tmp_path / 'configs/semantic_waldo.json')
    (tmp_path / 'docs').mkdir()
    (tmp_path / 'docs/semantics.md').write_text('Semantic workflow guide')
    return TestClient(create_app(tmp_path))


def test_multiview_report_is_read_only_and_excludes_depth_arrays(tmp_path, make_symlink):
    report=tmp_path/'reports/semantic-multiview-20261021'
    report.mkdir(parents=True)
    (report/'results.json').write_text(json.dumps({'status':'completed','per_view':[]}))
    (report/'sink-views.jpg').write_bytes(b'contact sheet')
    (report/'depth.npz').write_bytes(b'private depth cache')
    outside=tmp_path/'private.jpg';outside.write_bytes(b'private')
    make_symlink(report/'escape.jpg', outside)
    with semantic_client(tmp_path) as client:
        assert client.get('/api/semantic/multiview').json()['available']
        assert client.get('/api/semantic/multiview/artifacts/sink-views.jpg').content==b'contact sheet'
        assert client.get('/api/semantic/multiview/artifacts/depth.npz').status_code==404
        assert client.get('/api/semantic/multiview/artifacts/escape.jpg').status_code==404


def test_semantic_status_reports_missing_models_and_real_exercise_states(tmp_path):
    with semantic_client(tmp_path) as client:
        response = client.get('/api/semantic/status')
        assert response.status_code == 200
        body = response.json()
        assert body['available'] and not body['prepared'] and not body['gpu_busy']
        assert len(body['exercises']) == 7
        assert all(not item['ready'] for item in body['models'])
        assert client.get('/api/semantic/guide').text == 'Semantic workflow guide'


def test_semantic_artifacts_are_contained_and_only_expose_expected_types(tmp_path, make_symlink):
    directory = tmp_path / 'data/semantic_waldo/raw'
    directory.mkdir(parents=True)
    (directory / '000099.png').write_bytes(b'PNG protocol fixture')
    (directory / '000099.npz').write_bytes(b'not exposed')
    outside = tmp_path / 'secret.png'
    outside.write_bytes(b'not exposed')
    make_symlink(directory / 'escape.png', outside)
    with semantic_client(tmp_path) as client:
        assert client.get('/api/semantic/artifacts/raw/000099.png').content == b'PNG protocol fixture'
        assert client.get('/api/semantic/artifacts/raw/000099.npz').status_code == 404
        assert client.get('/api/semantic/artifacts/raw/escape.png').status_code == 404
        assert client.get('/api/semantic/artifacts/raw/missing.png').status_code == 404


def test_semantic_workspaces_show_separate_artifacts_and_reject_path_selection(tmp_path):
    with semantic_client(tmp_path) as client:
        config = json.loads((tmp_path / 'configs/semantic_waldo.json').read_text())
        config.update(workspace='data/semantic_repaired', name='repaired')
        (tmp_path / 'configs/semantic_waldo_repaired.json').write_text(json.dumps(config))
        for folder, content in [('semantic_waldo', b'original'), ('semantic_repaired', b'repaired')]:
            raw = tmp_path / 'data' / folder / 'raw'
            raw.mkdir(parents=True)
            (raw / '000001.png').write_bytes(content)
        repaired = client.get('/api/semantic/status').json()
        assert repaired['config'] == 'semantic_waldo_repaired'
        assert repaired['name'] == 'repaired'
        assert client.get(repaired['previews'][0]['url']).content == b'repaired'
        original = client.get('/api/semantic/status?config=semantic_waldo').json()
        assert client.get(original['previews'][0]['url']).content == b'original'
        for name in ('../semantic_waldo', 'semantic_../secret', '/tmp/semantic_other'):
            assert client.get('/api/semantic/status', params={'config': name}).status_code == 400


def test_reference_progress_and_images_are_read_only_and_contained(tmp_path, make_symlink):
    with semantic_client(tmp_path) as client:
        assert client.get('/api/semantic/reference').json()=={'available':False}
        report = tmp_path/'reports/langsplat-reference-20261021'
        report.mkdir(parents=True)
        (report/'source-manifest.json').write_text('{}')
        (tmp_path/'docs/semantics.md').write_text('Reference workflow')
        assert client.get('/api/semantic/reference/guide').text=='Reference workflow'
        (report/'controller-status.json').write_text(json.dumps({'status':'waiting_for_preprocessing'}))
        features = tmp_path/'data/langsplat_reference/language_features'
        features.mkdir(parents=True)
        (features/'frame_00001.json').write_text('{}')
        (features/'frame_00002_f.npy').write_bytes(b'incomplete extraction: no metadata yet')
        model = tmp_path/'data/langsplat_reference/train/level2'
        model.mkdir(parents=True)
        (model/'progress.json').write_text(json.dumps({'trace':[{'iteration':200}]}))
        images = report/'evaluation/dev-field'
        images.mkdir(parents=True)
        (images/'000062-knife-heatmap.png').write_bytes(b'heatmap fixture')
        (images/'000062-knife-mask.png').write_bytes(b'mask fixture')
        (report/'private.pt').write_bytes(b'checkpoint not exposed')
        outside = tmp_path/'secret.png'
        outside.write_bytes(b'private outside report')
        make_symlink(report/'escape.png', outside)
        body = client.get('/api/semantic/reference').json()
        assert body['available'] and body['extraction']['completed']==1
        assert body['protocols']['matched']['models'][1]['iteration']==200
        item = body['protocols']['matched']['images'][0]
        assert client.get(item['heatmap_url']).content==b'heatmap fixture'
        assert client.get(item['mask_url']).content==b'mask fixture'
        assert client.get('/api/semantic/reference/artifacts/private.pt').status_code==404
        assert client.get('/api/semantic/reference/artifacts/escape.png').status_code==404


def test_codec_study_exposes_only_completed_metrics_and_report_artifacts(tmp_path, make_symlink):
    with semantic_client(tmp_path) as client:
        assert client.get('/api/semantic/codec-study').json()=={'available':False}
        report=tmp_path/'reports/semantic-codec-study-20261021'
        report.mkdir(parents=True)
        (report/'manifest.json').write_text(json.dumps({'variants':[{'name':'stock3','dim':3}]}))
        (report/'progress.json').write_text(json.dumps({'stage':'fit','variant':'stock3','epoch':42}))
        (tmp_path/'docs/semantics.md').write_text('Codec diagnostic guide')
        model=tmp_path/'data/semantic_codec_study/stock3';model.mkdir(parents=True)
        (model/'completed.json').write_text(json.dumps({'epochs':100}))
        body=client.get('/api/semantic/codec-study').json()
        assert body['progress']['epoch']==42 and not body['complete']
        assert body['variants'][0]['trained'] and body['variants'][0]['metrics']=={}
        assert client.get('/api/semantic/codec-study/guide').text=='Codec diagnostic guide'
        (report/'README.md').write_text('Actual diagnostic results')
        (report/'model.pt').write_bytes(b'private model')
        outside=tmp_path/'private.png';outside.write_bytes(b'outside report')
        make_symlink(report/'escape.png', outside)
        assert client.get('/api/semantic/codec-study/artifacts/README.md').text=='Actual diagnostic results'
        assert client.get('/api/semantic/codec-study/artifacts/model.pt').status_code==404
        assert client.get('/api/semantic/codec-study/artifacts/escape.png').status_code==404
