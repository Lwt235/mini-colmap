"""Pinned-source parity for the reference's engineering accelerations."""
import ast
import importlib.util
import json
from pathlib import Path
import numpy as np
import pytest
import torch

ROOT = Path(__file__).resolve().parents[2]
UP = ROOT/'.cache/langsplat-official'


def function(path,name,namespace):
    node = next(n for n in ast.parse(path.read_text()).body if isinstance(n,ast.FunctionDef) and n.name==name)
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),namespace)
    return namespace[name]


@pytest.fixture
def metrics():
    path = ROOT/'scripts/langsplat_reference_metrics.py'
    spec = importlib.util.spec_from_file_location('reference_metric_tests',path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_majority_filter_keeps_upstream_boundary_and_tie_behavior(metrics):
    for seed in (2,7):
        mask = np.random.default_rng(seed).integers(0,2,(13,17),dtype=np.uint8)
        expected = mask.copy()
        for y in range(len(mask)):
            for x in range(mask.shape[1]):
                window = mask[max(0,y-3):min(y+4,len(mask)-1),max(0,x-3):min(x+4,mask.shape[1]-1)]
                expected[y,x] = np.argmax(np.bincount(window.ravel()))
        np.testing.assert_array_equal(metrics.smooth_majority(mask),expected)


@pytest.mark.skipif(not UP.is_dir(),reason='Pinned upstream source is downloaded by the reference setup')
def test_pairwise_nms_acceleration_matches_pinned_source_on_overlap_and_ties():
    original = function(UP/'preprocess.py','mask_nms',dict(torch=torch))
    fast = function(ROOT/'scripts/langsplat_reference.py','fast_mask_nms',dict(torch=torch,progress=lambda *a,**k:None))
    masks = torch.from_numpy(np.random.default_rng(8).random((14,17,21))>.4)
    masks[3] = masks[1]
    masks[4] = masks[1]
    scores = torch.tensor([.92,.95,.96,.95,.97,.9,.91,.89,.85,.88,.96,.94,.9,.93])
    for iou,inner in ((.8,.5),(.7,.2)):
        expected = original(masks,scores,iou_thr=iou,score_thr=.7,inner_thr=inner)
        actual = fast(masks,scores,iou_thr=iou,score_thr=.7,inner_thr=inner,intersection_device='cpu')
        torch.testing.assert_close(actual,expected,rtol=0,atol=0)


@pytest.mark.skipif(not UP.is_dir(),reason='Needs pinned official evaluation source')
def test_official_bbox_localization_is_not_polygon_peak_localization(metrics,tmp_path):
    annotation = {'info':{'height':40,'width':40},'objects':[{
        'category':'triangle','bbox':[5,5,25,25],
        'segmentation':[[5,5],[25,5],[5,25]]}]}
    path = tmp_path/'annotation.json'
    path.write_text(json.dumps(annotation))
    heat = np.full((1,1,40,40),.1,np.float32)
    heat[:,:,18:21,18:21] = .9
    one,_ = metrics.evaluate_maps(UP,heat,['triangle'],path)
    three,_ = metrics.evaluate_maps(UP,np.repeat(heat,3,axis=0),['triangle'],path)
    assert one['common']['polygon_peak_rate']==0.
    assert one['official']['bbox_localization_rate']==1.
    assert one==three


@pytest.mark.skipif(not UP.is_dir(),reason='Needs pinned official evaluation source')
def test_query_mask_without_gt_matches_actual_official_metric_mask(metrics,tmp_path):
    path = tmp_path/'annotation.json'
    path.write_text(json.dumps({'info':{'height':40,'width':43},'objects':[{
        'category':'target','bbox':[4,4,25,30], 'segmentation':[[4,4],[25,4],[25,30],[4,30]]}]}))
    maps = np.random.default_rng(0).uniform(.2,.8,(3,1,40,43)).astype(np.float32)
    result,masks = metrics.evaluate_maps(UP,maps,['target'],path)
    chosen,mask = metrics.query_mask(maps[:,0])
    assert chosen+1==result['per_query'][0]['official_level']
    np.testing.assert_array_equal(mask,masks['target'])


@pytest.mark.skipif(not UP.is_dir(),reason='Needs pinned official AE source')
def test_official_codec_is_unit_normalized_and_has_trainable_batchnorm():
    spec = importlib.util.spec_from_file_location('official_ae_test',UP/'autoencoder/model.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    torch.manual_seed(0)
    model = module.Autoencoder([256,128,64,32,3],[16,32,64,128,256,256,512])
    target = torch.nn.functional.normalize(torch.randn(64,512),dim=-1)
    codes = model.encode(target)
    prediction = model.decode(codes)
    torch.testing.assert_close(codes.norm(dim=-1),torch.ones(64))
    torch.testing.assert_close(prediction.norm(dim=-1),torch.ones(64))
    loss = (prediction-target).square().mean()+.001*(1-torch.nn.functional.cosine_similarity(prediction,target,dim=0).mean())
    loss.backward()
    assert any(isinstance(layer,torch.nn.BatchNorm1d) for layer in model.encoder)
    assert all(p.grad is not None and torch.isfinite(p.grad).all() for p in model.parameters())
