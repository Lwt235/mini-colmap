import json
from fastapi.testclient import TestClient
from minicolmap.ui.app import create_app


def test_scene_routes_validate_ids_payloads_and_artifact_boundaries(tmp_path, make_symlink):
    scene_id='scene-'+'a'*16
    folder=tmp_path/'data/semantic_scenes'/scene_id
    folder.mkdir(parents=True)
    (folder/'scene.json').write_text(json.dumps(dict(id=scene_id,rgb_run_id='missing',anchor_image_id=1)))
    (folder/'project.json').write_text('{}')
    (folder/'scores.npz').write_bytes(b'private')
    (folder/'preview.png').write_bytes(b'png')
    private=tmp_path/'private.png';private.write_bytes(b'private')
    make_symlink(folder/'escape.png', private)
    with TestClient(create_app(tmp_path)) as client:
        assert client.get('/api/scenes').json()=={'scenes':[]}
        for payload in ({},[],{'rgb_run_id':3},{'rgb_run_id':'x','path':'/tmp'}):
            assert client.post('/api/scenes',json=payload).status_code==422
        assert client.post('/api/scenes',json={'rgb_run_id':'missing'}).status_code==404
        assert client.get(f'/api/scenes/{scene_id}/artifacts/preview.png').content==b'png'
        for name in ('scores.npz','project.json','escape.png'):
            assert client.get(f'/api/scenes/{scene_id}/artifacts/{name}').status_code==404
        assert client.post(f'/api/scenes/{scene_id}/query',json={'text':'chair','path':'/tmp'}).status_code==422
        assert client.post(f'/api/scenes/{scene_id}/query',json={'text':'chair'}).status_code==422
        assert client.get('/api/scene-jobs/invalid').status_code==422
        assert client.get('/api/scene-jobs/'+'a'*32).status_code==404
        assert client.post('/api/scenes',json={},headers={'origin':'https://example.com'}).status_code==403
