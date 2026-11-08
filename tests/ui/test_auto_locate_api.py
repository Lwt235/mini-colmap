import json
import pytest
from fastapi.testclient import TestClient
from minicolmap.ui.auto_locate import AutoLocateJobs
from minicolmap.semantics.auto_locate import search_key
from minicolmap.ui.app import create_app


def setup(root,monkeypatch,busy=True):
    monkeypatch.setattr('minicolmap.ui.auto_locate.catalogue',lambda root:{'models':[{'id':'raw3','ready':True}],'gpu_busy':False})
    monkeypatch.setattr('minicolmap.ui.auto_locate.source_identity',lambda root:{'source':'a'})
    jobs=AutoLocateJobs(root,lambda:busy);jobs.folder.mkdir(parents=True)
    return jobs


def test_search_does_not_accept_a_camera_or_untrusted_model_and_reuses_terminal_cache(tmp_path,monkeypatch):
    jobs=setup(tmp_path,monkeypatch)
    value=dict(id='a'*32,status='rejected',request={'model':'raw3','text':'sink'},
        search_key=search_key('sink',{'source':'a'}),source_identity={'source':'a'})
    jobs.path(value['id']).write_text(json.dumps(value))
    assert jobs.submit({'model':'raw3','text':' SINK '})['cached']
    for payload in ([],{'model':'reference','text':'sink'},{'model':'raw3','text':'sink','image_id':124},{'model':'raw3','text':[]}):
        with pytest.raises(ValueError):jobs.submit(payload)
    with pytest.raises(RuntimeError):jobs.submit({'model':'raw3','text':'knife'})


def test_candidate_choice_binds_server_candidates_and_rejects_stale_policy(tmp_path,monkeypatch):
    jobs=setup(tmp_path,monkeypatch,busy=False)
    value=dict(id='a'*32,status='needs_choice',request={'model':'raw3','text':'sink'},
        search_key=search_key('sink',{'source':'a'}),source_identity={'source':'a'},candidates=[{'id':'124-1'}])
    jobs.path(value['id']).write_text(json.dumps(value))
    for payload in ([],{'candidate_id':[]},{'candidate_id':'../../file'},{'candidate_id':'124-1','path':'/tmp'}):
        with pytest.raises(ValueError):jobs.choose(value['id'],payload)
    captured=[]
    monkeypatch.setattr(jobs,'start',lambda v:captured.append(v) or v)
    assert jobs.choose(value['id'],{'candidate_id':'124-1'})['chosen_id']=='124-1'
    value['search_key']='old-policy';jobs.path(value['id']).write_text(json.dumps(value))
    assert not jobs.history()
    with pytest.raises(ValueError,match='策略'):jobs.choose(value['id'],{'candidate_id':'124-1'})


def test_auto_locate_routes_keep_candidate_images_contained(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        assert client.post('/api/demo/auto-locate',json=[]).status_code==422
        assert client.post('/api/demo/auto-locate',content='{').status_code==400
        assert client.get('/api/demo/auto-locate/invalid').status_code==422
        assert client.get('/api/demo/auto-locate/'+'a'*32).status_code==404
        assert client.post('/api/demo/auto-locate',json={},headers={'origin':'https://example.com'}).status_code==403
