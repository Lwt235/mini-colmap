import json
from fastapi.testclient import TestClient
from minicolmap.ui.app import create_app


def test_showcase_only_exposes_source_checked_curated_selections(tmp_path,monkeypatch):
    config=tmp_path/'configs';config.mkdir()
    (config/'showcase.json').write_text(json.dumps(dict(release='test',selections=[
        {'selection_id':'present','title':'水槽','description':'verified'},
        {'selection_id':'missing','title':'不应显示','description':'not validated'}])))
    docs=tmp_path/'docs';docs.mkdir();(docs/'evaluation.md').write_text('Evidence and scope')
    monkeypatch.setattr('minicolmap.ui.showcase.selections',lambda root:[dict(id='present',selected_gaussians=100,anchor_image_id=126)])
    with TestClient(create_app(tmp_path)) as client:
        d=client.get('/api/showcase').json()
        assert d['ready'] and len(d['selections'])==1 and d['selections'][0]['source_checked']
        assert client.get(d['evidence_url']).text=='Evidence and scope'
        assert client.post('/api/showcase',json={}).status_code==405
