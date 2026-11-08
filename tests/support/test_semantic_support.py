"""Semantic project, codec, evaluation, CLI and error-protocol checks."""
import copy
import json
from pathlib import Path

import cv2
import numpy as np
import pytest
import torch

from minicolmap.io import save_reconstruction
from minicolmap.models import Camera, ImageRecord, Pose, Reconstruction
from minicolmap.semantics.project import load_config, prepare, project, image_frame, exercise_status
from minicolmap.semantics.encoders import read_raw, gpu_session
from minicolmap.semantics.evaluation import binary_metrics, annotation_masks
from minicolmap.semantics.pipeline import choose_frames
from minicolmap.semantics.cli import main


def test_conditioned_codec_roundtrips_buffers_and_keeps_decode_in_original_units():
    from minicolmap.semantics.compression import build_codec, initialize_conditioning, codec_loss
    torch.manual_seed(4)
    values = torch.randn(16, 8) * .02 + .3
    model = build_codec(8, 3, conditioning='centered')
    initialize_conditioning(model, values)
    torch.testing.assert_close(model.center, values.mean(0))
    encoded = model.encode(values)
    predicted = model.decode(encoded)
    torch.testing.assert_close(predicted, model(values))
    loss = codec_loss(model, predicted, values, .2)
    loss.backward()
    assert all(p.grad is not None and torch.isfinite(p.grad).all() for p in model.parameters())
    clone = build_codec(8, 3, conditioning='centered')
    clone.load_state_dict(model.state_dict())
    torch.testing.assert_close(clone(values), predicted)
    assert not model.center.requires_grad and model.scale > 0
    # Decoding a latent average must not apply any per-vector normalization.
    codes = encoded.detach()
    midpoint = .5 * (codes[0] + codes[1])
    expected = model.center + model.scale * model.network.decode(midpoint)
    torch.testing.assert_close(model.decode(midpoint), expected)


def test_pca_codec_fits_train_only_and_commutes_with_feature_interpolation():
    from minicolmap.semantics.compression import build_codec, fit_pca
    # Exactly rank two, with an offset: decoding must restore the center once.
    generator = torch.Generator().manual_seed(8)
    train = torch.randn(30, 2, generator=generator) @ torch.randn(2, 8, generator=generator) + .7
    model = build_codec(8, 2, codec='pca')
    assert fit_pca(model, train) == pytest.approx(1., abs=1e-6)
    torch.testing.assert_close(model(train), train, atol=1e-5, rtol=1e-5)
    weights = torch.tensor([.2, .3, .5])
    decoded_mix = model.decode((model.encode(train[:3]) * weights[:, None]).sum(0))
    torch.testing.assert_close(decoded_mix, (model(train[:3]) * weights[:, None]).sum(0))
    codes = model.encode(train[:3]).detach().requires_grad_(True)
    model.decode(codes).square().mean().backward()
    assert codes.grad is not None and codes.grad.abs().sum() > 0
    clone = build_codec(8, 2, codec='pca')
    clone.load_state_dict(model.state_dict())
    torch.testing.assert_close(clone(train), train, atol=1e-5, rtol=1e-5)


def test_old_autoencoder_checkpoint_remains_loadable_but_codec_mismatch_fails(semantic_config, monkeypatch):
    from minicolmap.semantics import algorithms, pipeline
    config, _ = semantic_config
    prepared = prepare(config)
    monkeypatch.setattr(torch.nn.Module, 'cuda', lambda self: self)
    model = algorithms.build_autoencoder(512, 3)
    checkpoint = {'state': model.state_dict(), 'latent_dim': 3, 'train_ids': prepared['splits']['train'],
                  'rgb_checkpoint_sha256': prepared['rgb_checkpoint_sha256']}
    path = Path(config['workspace']) / 'autoencoder/checkpoint.pt'
    path.parent.mkdir()
    torch.save(checkpoint, path)  # Historical files have neither codec nor conditioning keys.
    loaded = pipeline.load_autoencoder(config, prepared)
    x = torch.randn(4, 512)
    torch.testing.assert_close(loaded(x), model(x))
    config['autoencoder']['codec'] = 'pca'
    with pytest.raises(ValueError, match='different source/split'):
        pipeline.load_autoencoder(config, prepared)


def test_residual_loss_configuration_is_explicit(semantic_config):
    _, path = semantic_config
    document = json.loads(path.read_text())
    for weight in (-1, float('nan'), True):
        document['autoencoder'].update(conditioning='centered', residual_weight=weight)
        path.write_text(json.dumps(document))
        with pytest.raises(ValueError, match='residual'):
            load_config(path)
    document['autoencoder'].update(conditioning=None, residual_weight=.2)
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match='requires centered'):
        load_config(path)


def test_compression_query_preserves_grid_and_runs_without_a_renderer(semantic_config, monkeypatch):
    """Expose compression failures before 3D; raw features must stay untouched."""
    from minicolmap.semantics import pipeline
    config, _ = semantic_config
    prepared = prepare(config)
    frame = choose_frames(prepared, image_id=2)[0]
    labels = np.zeros((6, 8), np.int32)
    labels[:, 4:] = 1
    labels[0, 0] = -1
    features = np.eye(2, dtype=np.float32)

    class Clip:
        def texts(self, texts):
            return torch.tensor([[1., 0.], [0., 1.]])

    # A deliberately bad codec reverses the two concepts, despite identical grids.
    codec = torch.nn.Linear(2, 2, bias=False)
    with torch.no_grad():
        codec.weight.copy_(torch.tensor([[0., 1.], [1., 0.]]))
    monkeypatch.setattr(pipeline, 'read_regions', lambda *args: (labels, features))
    monkeypatch.setattr(pipeline, 'load_autoencoder', lambda *args: codec)
    import minicolmap.semantics.rendering as rendering
    monkeypatch.setattr(rendering, 'FrozenGaussians', lambda *args: pytest.fail('2D compression must not render'))
    raw, valid, rgb = pipeline.prediction_map(config, prepared, frame, 'object', '2d', clip=Clip())
    compressed, compressed_valid, compressed_rgb = pipeline.prediction_map(
        config, prepared, frame, 'object', 'reconstructed', clip=Clip())
    np.testing.assert_array_equal(valid, compressed_valid)
    np.testing.assert_array_equal(rgb, compressed_rgb)
    np.testing.assert_array_equal(features, np.eye(2, dtype=np.float32))
    assert raw[0, 0] == compressed[0, 0] == 0
    assert raw[2, 2] > .5 and compressed[2, 2] < .5
    assert raw[2, 6] < .5 and compressed[2, 6] > .5


@pytest.fixture
def semantic_config(tmp_path):
    dataset = tmp_path / 'data/photos'
    dataset.mkdir(parents=True)
    output = tmp_path / 'outputs/rgb'
    output.mkdir(parents=True)
    camera = Camera(1, 8, 6, [[10, 0, 3.5], [0, 11, 2.5], [0, 0, 1]])
    images, poses, records = {}, {}, []
    for i in range(1, 7):
        images[i] = ImageRecord(i, f'{i}.png', 1, np.empty((0, 2)))
        poses[i] = Pose(np.eye(3), [i*.1, 0., 0.])
        cv2.imwrite(str(dataset / f'{i}.png'), np.full((6, 8, 3), i, np.uint8))
        records.append(dict(id=i, name=f'{i}.png', path=f'{i}.png', camera_id=1))
    save_reconstruction(Reconstruction({1: camera}, images, poses, {}), output / 'source_model.json')
    (dataset / 'dataset.json').write_text(json.dumps({'cameras': [{'id': 1, 'width': 8, 'height': 6, 'K': camera.K.tolist()}], 'images': records}))
    annotations = []
    for i in (2, 4):
        annotation = dataset / f'label-{i}.json'
        annotation.write_text('{}')
        annotations.append({'image_id': i, 'image_name': f'{i}.png', 'annotation': str(annotation)})
    (dataset / 'annotations.json').write_text(json.dumps({'frames': annotations}))
    torch.save({'pipeline': '3dgs', 'schema_version': 2, 'normalization': {'center': [0., 0., 0.], 'scale': 1.},
                'validation_ids': [6], 'state': {'means': torch.zeros(3, 3)}}, output / 'checkpoint.pt')
    template = Path(__file__).resolve().parents[2] / 'configs/semantic_waldo.json'
    document = json.loads(template.read_text())
    document.update(dataset='data/photos/dataset.json', annotations='data/photos/annotations.json',
                    rgb_run='outputs/rgb', workspace='data/semantic', dev_image_ids=[2], smoke_image_ids=[1])
    config_path = tmp_path / 'configs/semantic.json'
    config_path.parent.mkdir()
    config_path.write_text(json.dumps(document))
    return load_config(config_path), config_path


def test_semantic_split_excludes_all_annotations_and_rgb_holdout(semantic_config):
    config, _ = semantic_config
    result = prepare(config)
    assert result['splits'] == {'train': [1, 3, 5], 'dev': [2], 'test': [4], 'rgb_holdout': [6]}
    assert prepare(config) == result  # Identical preparation is idempotent.
    assert project(config)['gaussian_count'] == 3
    assert [f['image_id'] for f in choose_frames(result, 'smoke')] == [1]
    with pytest.raises(ValueError):
        choose_frames(result, image_id=100)


def test_semantic_stale_source_or_split_is_rejected(semantic_config):
    config, _ = semantic_config
    prepare(config)
    revised = copy.deepcopy(config)
    revised['seed'] = 999
    with pytest.raises(ValueError, match='Config changed'):
        project(revised)
    Path(config['rgb_run'], 'checkpoint.pt').write_bytes(b'changed')
    with pytest.raises(ValueError, match='changed'):
        project(config)


def test_semantic_unknown_fields_and_escaping_paths_fail(semantic_config):
    _, path = semantic_config
    original = json.loads(path.read_text())
    for override in ({'workspace': '../../escape'}, {'unknown': 1}):
        path.write_text(json.dumps({**original, **override}))
        with pytest.raises(ValueError):
            load_config(path)


def test_semantic_image_resize_keeps_pixel_projection_on_the_same_grid(semantic_config):
    config, _ = semantic_config
    frame = prepare(config)['frames'][0]
    rgb, resized = image_frame(frame, 4)
    assert rgb.shape == (3, 4, 3)
    expected = np.array([[5., 0., 1.5], [0., 5.5, 1.], [0., 0., 1.]])
    np.testing.assert_allclose(resized['K'], expected)
    assert frame['width'] == 8  # Source metadata is not mutated.


def test_raw_mask_bitpacking_roundtrip_and_empty_proposals(tmp_path):
    directory = tmp_path / 'raw'
    directory.mkdir()
    for image_id, masks in [(1, np.random.default_rng(0).integers(0, 2, (4, 3, 5)).astype(bool)), (2, np.empty((0, 3, 5), bool))]:
        packed = np.packbits(masks.reshape(len(masks), 15), axis=1)
        np.savez_compressed(directory / f'{image_id:06d}.npz', masks=packed, shape=[3, 5], quality=np.ones(len(masks)), features=np.zeros((len(masks), 512), np.float32))
        actual, quality, features = read_raw(tmp_path, image_id)
        np.testing.assert_array_equal(actual, masks)
        assert quality.shape == (len(masks),) and features.shape == (len(masks), 512)


def test_semantic_metrics_distinguish_mask_iou_from_point_localization():
    gt = np.zeros((3, 4), np.uint8)
    gt[1, 1] = 1
    scores = np.zeros((3, 4), np.float32)
    scores[1, 1] = .9
    scores[1, 2] = .8
    result = binary_metrics(scores > .5, gt, scores)
    assert result['iou'] == .5 and result['peak_inside_gt'] is True
    assert binary_metrics(np.zeros_like(gt), np.zeros_like(gt), np.zeros_like(scores))['iou'] == 1


def test_multiple_polygons_of_one_label_are_unioned(tmp_path):
    path = tmp_path / 'annotation.json'
    path.write_text(json.dumps({'info': {'height': 5, 'width': 8}, 'objects': [
        {'category': 'cup', 'segmentation': [[0, 0], [1, 0], [1, 1], [0, 1]]},
        {'category': 'cup', 'segmentation': [[6, 3], [7, 3], [7, 4], [6, 4]]}]}))
    masks = annotation_masks(path)
    assert masks['cup'].sum() == 8


def test_semantic_gpu_policy_rejects_multiple_cards_before_torch(semantic_config, monkeypatch):
    config, _ = semantic_config
    monkeypatch.setenv('CUDA_VISIBLE_DEVICES', '0,1')
    with pytest.raises(ValueError, match='exactly one physical GPU'):
        with gpu_session(config):
            pytest.fail('Should not reach CUDA')


def test_semantic_cli_reports_real_todo_with_exit_two(semantic_config, monkeypatch, capsys):
    config, path = semantic_config
    prepare(config)
    directory = Path(config['workspace']) / 'raw'
    directory.mkdir()
    np.savez_compressed(directory / '000001.npz', masks=np.array([[255, 255]], np.uint8), shape=[4, 4], features=np.ones((1, 512), np.float32), quality=np.ones(1))
    # A deliberate NotImplementedError fixture verifies CLI todo-state handling;
    # it does not indicate a missing production L1 implementation.
    from minicolmap.semantics import algorithms
    def unfinished(*args, **kwargs):
        raise NotImplementedError('L1 protocol fixture')
    monkeypatch.setattr(algorithms, 'masks_to_regions', unfinished)
    assert main(['regions', '--config', str(path), '--split', 'smoke']) == 2
    assert 'L1 protocol fixture' in capsys.readouterr().err
    assert not list((Path(config['workspace']) / 'regions').glob('*.npz'))
    assert json.loads((Path(config['workspace']) / 'last-command.json').read_text())['status'] == 'todo'


def test_unlabelled_scene_has_no_fake_dev_or_test_metrics(semantic_config):
    config, path = semantic_config
    document = json.loads(path.read_text())
    document.update(schema_version=2, dev_image_ids=[])
    Path(config['annotations']).write_text(json.dumps({'frames': []}))
    path.write_text(json.dumps(document))
    config = load_config(path)
    prepared = prepare(config)
    assert prepared['splits'] == {'train': [1, 2, 3, 4, 5], 'dev': [], 'test': [], 'rgb_holdout': [6]}
    assert 'no semantic accuracy' in prepared['evaluation_scope']
    # Schema 1 still requires labelled Dev data; it must not silently change meaning.
    path.write_text(json.dumps({**document, 'schema_version': 1}))
    with pytest.raises(ValueError): load_config(path)


def test_scene_creation_keeps_each_rgb_source_separate_and_does_not_start_gpu(semantic_config):
    from minicolmap.ui.scenes import SceneJobs
    from minicolmap.io import load_dataset
    from types import SimpleNamespace
    config, path = semantic_config
    root = path.parent.parent
    template = json.loads(path.read_text())
    (path.parent/'semantic_waldo.json').write_text(json.dumps(template))
    run = dict(id='rgb', pipeline='3dgs', status='completed', output_path=config['rgb_run'], name='new-room')
    store = SimpleNamespace(get=lambda run_id: run, image_dataset=lambda value:(Path(config['dataset']),load_dataset(config['dataset'])))
    jobs = SceneJobs(root,store,None)
    created=jobs.create('rgb')
    assert created['rgb_run_id']=='rgb'
    assert not jobs.processes
    assert jobs.create('rgb')==created
    generated=load_config(path.parent/f"{created['config']}.json")
    assert generated['schema_version']==2 and generated['dev_image_ids']==[]
    assert generated['rgb_run']==config['rgb_run']
    assert not (Path(generated['workspace'])/'distilled/checkpoint.pt').exists()
    run['pipeline']='nerf'
    with pytest.raises(ValueError,match='3DGS'):jobs.create('rgb')


def test_scene_worker_failure_releases_parent_reservation(tmp_path, monkeypatch):
    from minicolmap.semantics.scene_worker import main
    from minicolmap.ui.gpu_lease import acquire, active_lease
    token=acquire(tmp_path,tmp_path/'report')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('MINICOLMAP_QUERY_RESERVATION',token)
    path=tmp_path/'job.json'
    path.write_text(json.dumps(dict(id='a'*32,config='missing',action='train',scene_id='scene-'+'b'*16,status='running')))
    assert main(path)==1
    assert json.loads(path.read_text())['status']=='failed'
    assert active_lease(tmp_path) is None


def test_generic_selection_is_source_bound_and_not_a_published_kitchen_case(semantic_config):
    from minicolmap.semantics.scene_worker import source_identity
    from minicolmap.semantics.selection import load_selection, catalogue
    from minicolmap.semantics.project import sha256
    config,path=semantic_config
    scene_id='scene-'+'a'*16
    document=json.loads(path.read_text());document.update(schema_version=2,name=scene_id,dev_image_ids=[])
    Path(config['annotations']).write_text(json.dumps({'frames':[]}));path.write_text(json.dumps(document))
    config=load_config(path);prepared=prepare(config)
    root=path.parent.parent
    Path(config['model_manifest']).parent.mkdir(exist_ok=True)
    Path(config['model_manifest']).write_text('{}')
    for name in ('autoencoder','distilled'):
        p=Path(config['workspace'])/name/'checkpoint.pt';p.parent.mkdir(parents=True);p.write_bytes(b'identity fixture')
    sid='scene-'+'b'*24;directory=root/'data/object_selections'/sid;directory.mkdir(parents=True)
    np.savez_compressed(directory/'selection.npz',indices=np.array([0,1],np.int64))
    metadata=dict(id=sid,scene_config=str(path.relative_to(root)),source_scene=scene_id,supervision='scene-distilled-v1',status='candidate',source_identity=source_identity(config,prepared),rgb_checkpoint_sha256=prepared['rgb_checkpoint_sha256'],source_gaussians=3,selected_gaussians=2,center=[0,0,0],bounds=dict(min=[0,0,0],max=[1,1,1]),selection_sha256=sha256(directory/'selection.npz'))
    (directory/'selection.json').write_text(json.dumps(metadata))
    _,indices=load_selection(root,sid,source_sha256=prepared['rgb_checkpoint_sha256'],count=3)
    assert indices.tolist()==[0,1]
    assert catalogue(root)==[]
    with pytest.raises(ValueError,match='不匹配'):load_selection(root,sid,source_sha256='other-scene',count=3)
    (Path(config['workspace'])/'distilled/checkpoint.pt').write_bytes(b'changed semantic field')
    with pytest.raises(ValueError,match='变化'):load_selection(root,sid)
