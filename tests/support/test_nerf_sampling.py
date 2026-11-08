"""Sampling/I/O guarantees, independently of the learned appearance field."""
from types import SimpleNamespace

import numpy as np
import pytest

torch = pytest.importorskip('torch')

from minicolmap.rendering.config import resolve_render_config
from minicolmap.rendering.data import Frame
from minicolmap.rendering.pipeline import _nerf_ray_bundle, _training_ray_pool


class ConstantField(torch.nn.Module):
    def forward(self, xyz, directions):
        self.samples = xyz.detach().clone()
        return xyz.new_tensor([.2,.4,.6]).expand(len(xyz),3), xyz.new_full((len(xyz),),.7)


@pytest.mark.parametrize('background', ['white','black'])
@pytest.mark.parametrize('random', [False,True])
def test_finite_constant_field_matches_closed_form(background, random):
    config = resolve_render_config(dict(near=.2,far=1.1,samples_per_ray=8,
        background=background,stratified_sampling=random))
    network=ConstantField()
    origins=torch.zeros(3,3); directions=torch.tensor([[0.,0.,1.]]).expand(3,3)
    result=_nerf_ray_bundle(network,origins,directions,config,torch,training=True)
    opacity=1-np.exp(-.7*(1.1-.2))
    expected=torch.tensor([.2,.4,.6])*opacity + (1-opacity)*(background=='white')
    torch.testing.assert_close(result['rgb'],expected.expand(3,3))
    torch.testing.assert_close(result['opacity'],torch.full((3,),opacity))


def test_training_depths_are_ordered_seeded_and_evaluation_does_not_consume_rng():
    config=resolve_render_config(dict(near=.2,far=1.1,samples_per_ray=8,stratified_sampling=True))
    network=ConstantField(); origins=torch.zeros(3,3); directions=torch.tensor([[0.,0.,1.]]).expand(3,3)
    torch.manual_seed(39)
    _nerf_ray_bundle(network,origins,directions,config,torch,training=True)
    first=network.samples[:,2].reshape(3,8)
    assert torch.all(first[:,0]==.2) and torch.all(first[:,1:]>first[:,:-1])
    edges=.2+torch.arange(8)*(1.1-.2)/8
    assert torch.all(first>=edges) and torch.all(first<edges+(1.1-.2)/8)
    assert not torch.equal(first[0],first[1])
    torch.manual_seed(39)
    _nerf_ray_bundle(network,origins,directions,config,torch,training=True)
    torch.testing.assert_close(network.samples[:,2].reshape(3,8),first)
    before=torch.get_rng_state()
    _nerf_ray_bundle(network,origins,directions,config,torch)
    torch.testing.assert_close(network.samples[:,2].reshape(3,8),edges.expand(3,8))
    assert torch.equal(before,torch.get_rng_state())


def test_mixed_pool_contains_only_training_photos_and_their_world_rays():
    frames={i:Frame(i,str(i),np.eye(3),np.eye(3),np.array([i,0.,0.]),
                    np.full((2,2,3),color,dtype=np.float32))
            for i,color in ((1,.1),(2,.9),(3,.3))}
    scene=SimpleNamespace(frames=frames,train_ids=[1,3],validation_ids=[2])
    origins,directions,targets=_training_ray_pool(scene,torch,'cpu')
    assert len(targets)==8
    torch.testing.assert_close(targets[:4],torch.full((4,3),.1))
    torch.testing.assert_close(targets[4:],torch.full((4,3),.3))
    torch.testing.assert_close(origins[:4],torch.tensor([-1.,0.,0.]).expand(4,3))
    torch.testing.assert_close(origins[4:],torch.tensor([-3.,0.,0.]).expand(4,3))
    torch.testing.assert_close(directions.norm(dim=-1),torch.ones(8))


@pytest.mark.parametrize('override',[{'background':'red'},{'stratified_sampling':1},
                                    {'ray_sampling':'validation'}])
def test_reject_invalid_sampling_controls(override):
    with pytest.raises(ValueError):
        resolve_render_config(override)
