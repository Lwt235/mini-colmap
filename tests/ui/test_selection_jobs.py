import json
import os
from pathlib import Path
import numpy as np
import pytest
from fastapi.testclient import TestClient
from minicolmap.ui.app import create_app
from minicolmap.ui.selection_jobs import SelectionJobs, validate_payload
from minicolmap.semantics.selection import load_selection, selection_directory, catalogue
from minicolmap.semantics.selection_policy import selection_key


def setup(root, monkeypatch):
    path=root/'data/semantic_waldo';path.mkdir(parents=True)
    (path/'project.json').write_text(json.dumps(dict(frames=[{'image_id':124},{'image_id':85}],splits={'test':[85]})))
    monkeypatch.setattr('minicolmap.ui.selection_jobs.source_identity',lambda root:{'source':'a'})
    monkeypatch.setattr('minicolmap.ui.selection_jobs.demo_catalogue',lambda root:{'models':[{'id':'raw3','ready':True}],'gpu_busy':False})
    return {'model':'raw3','image_id':124,'text':' Dark   CUP '}


def test_job_submission_reuses_result_even_while_gpu_busy_and_preserves_rejections(tmp_path,monkeypatch):
    payload=setup(tmp_path,monkeypatch)
    jobs=SelectionJobs(tmp_path,lambda:True)
    sid=selection_key('dark cup',124,{'source':'a'})
    jobs.folder.mkdir(parents=True)
    value=dict(id='a'*32,selection_id=sid,status='rejected',request=payload,message='evidence insufficient')
    jobs.path(value['id']).write_text(json.dumps(value))
    assert jobs.submit(payload)['cached']
    assert jobs.submit(payload)['status']=='rejected'
    with pytest.raises(RuntimeError):jobs.submit({**payload,'text':'knife'})
    assert len(list(jobs.folder.glob('*.json')))==1


def test_job_spawns_gpu7_worker_and_detects_exit_before_startup_progress(tmp_path,monkeypatch):
    payload=setup(tmp_path,monkeypatch)
    calls=[]
    class Process:
        exited=False
        def poll(self):return 1 if self.exited else None
    process=Process()
    def spawn(command,**kwargs):calls.append((command,kwargs));return process
    monkeypatch.setattr('minicolmap.ui.selection_jobs.subprocess.Popen',spawn)
    jobs=SelectionJobs(tmp_path,lambda:False)
    value=jobs.submit(payload)
    assert value['status']=='running' and value['request']['text']=='dark cup'
    command,kwargs=calls[0]
    assert kwargs['env']['CUDA_VISIBLE_DEVICES']=='7' and 'shell' not in kwargs
    assert command[-2:]==['--job-id',value['id']]
    assert jobs.submit(payload)['cached'] and len(calls)==1
    process.exited=True
    assert jobs.job(value['id'])['status']=='failed'
    jobs.close()


def test_payload_and_job_identity_reject_test_views_and_untrusted_paths(tmp_path,monkeypatch):
    payload=setup(tmp_path,monkeypatch)
    for invalid in ([],{**payload,'model':'reference'},{**payload,'image_id':85},
                    {**payload,'image_id':True},{**payload,'text':'knife\n'},
                    {**payload,'steps':1000000}):
        with pytest.raises(ValueError):validate_payload(tmp_path,invalid)
    jobs=SelectionJobs(tmp_path,lambda:False)
    with pytest.raises(ValueError):jobs.job('../private')
    with pytest.raises(KeyError):jobs.job('a'*32)
    jobs.folder.mkdir(parents=True)
    jobs.path('a'*32).write_text(json.dumps(dict(id='a'*32,status='running',pid=-1)))
    assert jobs.history()[0]['status']=='failed'


def test_generated_selection_requires_accepted_evidence_and_current_source(tmp_path,monkeypatch):
    identity={'source':'a'}
    monkeypatch.setattr('minicolmap.semantics.selection_policy.source_identity',lambda root:identity)
    sid='object-'+'b'*24
    folder=selection_directory(tmp_path,sid);folder.mkdir(parents=True)
    metadata=dict(id=sid,source_identity={'source':'a'},source_gaussians=10,selected_gaussians=2,
        center=[0,0,0],bounds={'min':[-1,-1,-1],'max':[1,1,1]})
    (folder/'selection.json').write_text(json.dumps(metadata))
    np.savez(folder/'selection.npz',indices=np.array([1,3],dtype=np.int64))
    (folder/'selection.zip').write_bytes(b'zip')
    (folder/'validation.json').write_text(json.dumps(dict(selection_id=sid,evidence={'accepted':False})))
    assert catalogue(tmp_path)==[]
    with pytest.raises(ValueError):load_selection(tmp_path,sid)
    (folder/'validation.json').write_text(json.dumps(dict(selection_id=sid,evidence={'accepted':True})))
    assert load_selection(tmp_path,sid)[1].tolist()==[1,3]
    assert len(catalogue(tmp_path))==1
    identity['source']='changed'
    with pytest.raises(ValueError,match='源模型'):load_selection(tmp_path,sid)


def test_selection_job_routes_validate_input_and_do_not_export_rejected_candidates(tmp_path,monkeypatch):
    setup(tmp_path,monkeypatch)
    sid='object-'+'c'*24
    folder=selection_directory(tmp_path,sid);folder.mkdir(parents=True)
    (folder/'README.md').write_text('Insufficient evidence')
    with TestClient(create_app(tmp_path)) as client:
        assert client.post('/api/demo/selections',json=[]).status_code==422
        assert client.post('/api/demo/selections',content='{',headers={'content-type':'application/json'}).status_code==400
        assert client.get('/api/demo/selection-jobs/invalid').status_code==422
        assert client.get('/api/demo/selection-jobs/'+'a'*32).status_code==404
        assert client.get(f'/api/demo/selections/{sid}/report').text=='Insufficient evidence'
        assert client.get(f'/api/demo/selections/{sid}/export').status_code==422
        assert client.get(f'/api/demo/selections/{sid}/validation-image').status_code==404
        assert client.post('/api/demo/selections',json={},headers={'origin':'https://example.com'}).status_code==403
