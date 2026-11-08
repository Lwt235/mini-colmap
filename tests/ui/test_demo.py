"""Product API containment and read-only readiness, without allocating CUDA."""
import json
from pathlib import Path
from fastapi.testclient import TestClient
from minicolmap.ui.app import create_app
import numpy as np


def test_demo_reads_only_registered_images_and_public_artifacts(tmp_path, make_symlink):
    image = tmp_path/'data/scene/image.jpg'
    image.parent.mkdir(parents=True)
    image.write_bytes(b'image fixture')
    work = tmp_path/'data/semantic_waldo'
    work.mkdir()
    (work/'project.json').write_text(json.dumps({'frames':[{'image_id':124,'name':'cup.jpg','path':str(image)}]}))
    report = tmp_path/'reports/langsplat-raw3-20261021'
    report.mkdir(parents=True)
    (report/'result.png').write_bytes(b'heatmap')
    (report/'checkpoint.pt').write_bytes(b'private weights')
    outside = tmp_path/'private.png'
    outside.write_bytes(b'private')
    make_symlink(report/'escape.png', outside)
    with TestClient(create_app(tmp_path)) as client:
        body = client.get('/api/demo').json()
        assert body['available'] and body['frames']==[{'id':124,'name':'cup.jpg'}]
        assert not any(m['ready'] for m in body['models'])
        assert client.get('/api/demo/images/124').content==b'image fixture'
        assert client.get('/api/demo/images/125').status_code==404
        assert client.get('/api/demo/artifacts/raw3/result.png').content==b'heatmap'
        assert client.get('/api/demo/artifacts/raw3/checkpoint.pt').status_code==404
        assert client.get('/api/demo/artifacts/raw3/escape.png').status_code==404
        assert client.get('/api/demo/artifacts/unknown/result.png').status_code==422
        assert client.post('/api/demo/query',json=[]).status_code==400
        assert client.post('/api/demo/query',content='{',headers={'content-type':'application/json'}).status_code==400
        assert client.post('/api/demo/query',json={'model':[]}).status_code==422
        assert client.post('/api/demo/query',json={'model':'raw3','image_id':124,'text':'cup'}).status_code==422
        assert client.get('/api/demo/jobs/not-a-job').status_code==422
        assert client.get('/api/demo/jobs/'+'a'*32).status_code==404
        assert client.post('/api/demo/query',json={},headers={'origin':'https://example.com'}).status_code==403


def test_selection_export_and_model_data_do_not_expose_arbitrary_paths(tmp_path):
    from minicolmap.semantics.selection import SELECTION_ID,selection_directory
    directory=selection_directory(tmp_path,SELECTION_ID);directory.mkdir(parents=True)
    metadata=dict(id=SELECTION_ID,name='水槽',text='sink',selected_gaussians=2,source_gaussians=4,rgb_checkpoint_sha256='fixture',center=[0,0,0],bounds={'min':[-1,-1,-1],'max':[1,1,1]})
    (directory/'selection.json').write_text(json.dumps(metadata));np.savez(directory/'selection.npz',indices=np.array([0,2],dtype=np.int64))
    (directory/'selection.zip').write_bytes(b'zip export fixture')
    report=tmp_path/'reports/sink-selection-20261021';report.mkdir(parents=True);(report/'README.md').write_text('selection validation')
    (report/'validation.json').write_text(json.dumps({'status':'completed','selection_id':SELECTION_ID}))
    with TestClient(create_app(tmp_path)) as client:
        values=client.get('/api/demo').json()['selections'];assert values[0]['id']==SELECTION_ID
        response=client.get(values[0]['export_url']);assert response.content==b'zip export fixture'
        assert 'attachment' in response.headers['content-disposition']
        assert client.get(values[0]['inspection_url']).text=='selection validation'
        assert client.get('/api/demo/selections/unknown/export').status_code==422


def test_public_guides_use_consolidated_documentation(tmp_path):
    docs = tmp_path / 'docs'
    docs.mkdir()
    for name in ('evaluation.md', 'usage.md', 'semantics.md'):
        (docs / name).write_text(name, encoding='utf-8')
    guides = {
        '/api/showcase/guide': 'evaluation.md',
        '/api/demo/guide': 'usage.md',
        '/api/semantic/guide': 'semantics.md',
        '/api/semantic/codec-study/guide': 'semantics.md',
        '/api/semantic/reference/guide': 'semantics.md',
    }
    with TestClient(create_app(tmp_path)) as client:
        for endpoint, expected in guides.items():
            response = client.get(endpoint)
            assert response.status_code == 200
            assert response.text == expected
