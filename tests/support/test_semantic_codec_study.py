"""Meaningful controls for the codec study's isolated factors and loss math."""
import importlib.util
from pathlib import Path
import sys

import pytest
import torch
from torch.nn import functional as F

ROOT=Path(__file__).resolve().parents[2]


@pytest.fixture
def study():
    if not (ROOT/'.cache/langsplat-official/autoencoder/model.py').exists():
        pytest.skip('Pinned official codec source is not downloaded')
    sys.path.insert(0,str(ROOT/'scripts'))
    spec=importlib.util.spec_from_file_location('codec_study_tests',ROOT/'scripts/semantic_codec_study.py')
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_baseline_matches_official_forward_and_parameter_gradients(study):
    torch.manual_seed(9)
    model=study.codec(study.VARIANTS[0])
    original=study.ref.upstream_ae('cpu')
    original.load_state_dict(model.state_dict())
    target=F.normalize(torch.randn(32,512),dim=-1)
    for training in (True,False):
        model.train(training);original.train(training)
        model.zero_grad(set_to_none=True);original.zero_grad(set_to_none=True)
        actual,expected=model(target),original(target)
        torch.testing.assert_close(actual,expected,rtol=0,atol=0)
        study.objective(actual,target,study.VARIANTS[0]).backward()
        study.objective(expected,target,study.VARIANTS[0]).backward()
        for a,b in zip(model.parameters(),original.parameters()):
            torch.testing.assert_close(a.grad,b.grad,rtol=0,atol=0)


def test_unit_mse_and_row_cosine_weights_are_only_a_loss_scale(study):
    torch.manual_seed(7)
    raw=torch.randn(19,512,requires_grad=True)
    target=F.normalize(torch.randn(19,512),dim=-1)
    prediction=F.normalize(raw,dim=-1)
    mse=(prediction-target).square().mean()
    row=1-F.cosine_similarity(prediction,target,dim=-1).mean()
    torch.testing.assert_close(mse,2/512*row,rtol=1e-6,atol=1e-9)
    low=study.objective(prediction,target,study.VARIANTS[1])
    high=study.objective(prediction,target,study.VARIANTS[2])
    low_grad=torch.autograd.grad(low,raw,retain_graph=True)[0]
    high_grad=torch.autograd.grad(high,raw)[0]
    ratio=(2/512+1)/(2/512+.001)
    torch.testing.assert_close(high_grad,low_grad*ratio,rtol=1e-5,atol=1e-9)


def test_declared_experiments_change_only_the_named_factor(study):
    specs={s['name']:s for s in study.VARIANTS}
    expected={'row_cos001':'axis','row_cos1':'weight','no_latent_norm':'latent_norm',
              'no_output_norm':'output_norm','row_dim8':'dim','row_dim16':'dim','stock_dim16':'dim'}
    for name,key in expected.items():
        child=specs[name];parent=specs[child['parent']]
        changed={k for k in ('dim','axis','weight','latent_norm','output_norm') if child[k]!=parent[k]}
        assert changed=={key}
    for spec in study.VARIANTS:
        model=study.codec(spec).eval()
        with torch.inference_mode():
            code=model.encode(torch.randn(16,512))
            output=model.decode(code)
        assert code.shape==(16,spec['dim']) and output.shape==(16,512)
        assert torch.isfinite(output).all()
        if spec['latent_norm']:
            torch.testing.assert_close(code.norm(dim=-1),torch.ones(16))
        if spec['output_norm']:
            torch.testing.assert_close(output.norm(dim=-1),torch.ones(16))
