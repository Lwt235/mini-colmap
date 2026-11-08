"""Catalogue availability, GPU protection, cache replay and user input validation."""
import json
from pathlib import Path
import pytest
from minicolmap.ui.demo import catalogue, DemoQueries, query_result


def put(root, path, value):
    path = root/path
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value))
    return path


def setup(root):
    put(root,'data/semantic_waldo/project.json',{'frames':[{'image_id':124,'name':'cup.jpg'}],'splits':{'train':[124]},'gaussian_count':3})
    for level in (1,2,3):
        path = put(root,f'data/langsplat_reference/train/level{level}/completed.json',{'iterations':30000})
        path.with_name('chkpnt30000.pth').write_bytes(b'fixture')
    path = put(root,'reports/langsplat-reference-20261021/queries/saved/query.json',
        {'image_id':124,'text':'dark cup','level':2,'selected_pixels':17})
    for name in ('heatmap.png','mask.png'):
        path.with_name(name).write_bytes(b'fixture')
    return path


def test_catalogue_requires_all_three_checkpoints_and_reports_real_progress(tmp_path):
    assert not catalogue(tmp_path)['available']
    setup(tmp_path)
    value = catalogue(tmp_path)
    assert value['models'][0]['ready'] and not value['models'][1]['ready']
    assert len(value['gallery'])==1 and value['gallery'][0]['photo_url']=='/api/demo/images/124'
    (tmp_path/'data/langsplat_reference/train/level3/chkpnt30000.pth').unlink()
    assert not catalogue(tmp_path)['models'][0]['ready']


def test_busy_gpu_replays_saved_results_but_refuses_new_queries(tmp_path):
    setup(tmp_path)
    queries = DemoQueries(tmp_path,lambda:True)
    saved = queries.submit({'model':'reference','image_id':124,'text':'dark cup'})
    assert saved['status']=='completed' and saved['cached']
    with pytest.raises(RuntimeError,match='GPU'):
        queries.submit({'model':'reference','image_id':124,'text':'new object'})
    assert queries.process is None


@pytest.mark.parametrize('payload',[
    {'model':'../../secret','image_id':124,'text':'cup'},
    {'model':'reference','image_id':True,'text':'cup'},
    {'model':'reference','image_id':125,'text':'cup'},
    {'model':'reference','image_id':124,'text':' '},
    {'model':'reference','image_id':124,'text':'x'*161},
    {'model':'reference','image_id':124,'text':'cup\n'},
    {'model':'raw3','image_id':124,'text':'cup'},
])
def test_query_validation_before_process_launch(tmp_path,payload):
    setup(tmp_path)
    with pytest.raises(ValueError):
        DemoQueries(tmp_path,lambda:False).submit(payload)


def test_query_catalogue_rejects_escaped_result_directory(tmp_path):
    path = setup(tmp_path)
    outside = put(tmp_path,'private/query.json',json.loads(path.read_text()))
    assert query_result(tmp_path,'reference',outside) is None


def test_parent_reservation_releases_on_worker_spawn_failure(tmp_path,monkeypatch):
    from minicolmap.ui.gpu_lease import active_lease
    setup(tmp_path)
    def fail(*args,**kwargs):
        assert active_lease(tmp_path) is not None
        assert kwargs['env']['CUDA_VISIBLE_DEVICES']=='7'
        raise OSError('worker spawn failed')
    monkeypatch.setattr('minicolmap.ui.demo.subprocess.Popen',fail)
    with pytest.raises(OSError,match='spawn failed'):
        DemoQueries(tmp_path,lambda:False).submit({'model':'reference','image_id':124,'text':'knife'})
    assert active_lease(tmp_path) is None


def test_only_matching_reservation_can_be_adopted(tmp_path):
    from minicolmap.ui.gpu_lease import acquire,adopt,release,active_lease
    token = acquire(tmp_path,tmp_path/'report')
    with pytest.raises(RuntimeError,match='预留'):
        adopt(tmp_path,'wrong-token')
    assert active_lease(tmp_path)['token']==token
    assert adopt(tmp_path,token)==token
    release(tmp_path,token)
    assert active_lease(tmp_path) is None


def test_default_model_selection_uses_dev_and_never_test(tmp_path):
    setup(tmp_path)
    for level in (1,2,3):
        path=put(tmp_path,f'data/langsplat_raw3/train/level{level}/completed.json',{'iterations':30000})
        path.with_name('chkpnt30000.pth').write_bytes(b'fixture')
    for name,dev,test in [('langsplat-reference-20261021',.35,.99),('langsplat-raw3-20261021',.43,.01)]:
        for split,score in [('dev',dev),('test',test)]:
            put(tmp_path,f'reports/{name}/evaluation/{split}-field/summary.json',{'results':{'official':{'mean_iou':score}}})
    assert catalogue(tmp_path)['recommended_model']=='raw3'
