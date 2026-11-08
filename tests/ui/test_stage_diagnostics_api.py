import json
from fastapi.testclient import TestClient
from minicolmap.ui.app import create_app


def test_stage_diagnostics_are_read_only_and_do_not_expose_private_maps(tmp_path, make_symlink):
    report=tmp_path/'reports/semantic-stage-diagnostics-20261022';report.mkdir(parents=True)
    (report/'results.json').write_text(json.dumps({'status':'completed','focus':[]}))
    (report/'findings.json').write_text(json.dumps([{'query':'knife','detail':'out of frame'}]))
    (report/'comparison.jpg').write_bytes(b'comparison')
    (report/'private-maps.npz').write_bytes(b'private feature arrays')
    outside=tmp_path/'private.jpg';outside.write_bytes(b'outside')
    make_symlink(report/'escape.jpg', outside)
    with TestClient(create_app(tmp_path)) as client:
        value=client.get('/api/semantic/stage-diagnostics').json()
        assert value['available'] and value['findings'][0]['query']=='knife'
        assert client.get('/api/semantic/stage-diagnostics/artifacts/comparison.jpg').content==b'comparison'
        assert client.get('/api/semantic/stage-diagnostics/artifacts/private-maps.npz').status_code==404
        assert client.get('/api/semantic/stage-diagnostics/artifacts/escape.jpg').status_code==404
        assert client.post('/api/semantic/stage-diagnostics',json={}).status_code==405
