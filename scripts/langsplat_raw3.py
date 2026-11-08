"""Selected non-unit codec and matching raw Gaussian field, isolated from reference.

The pinned renderer is compiled in memory with exactly its language normalization
assignment removed. CUDA, RGB, alpha compositing and the training loop stay intact.
"""
import argparse
import ast
import json
from pathlib import Path
import shutil

import numpy as np
import torch
import langsplat_reference as ref
from semantic_codec_study import codec, VARIANTS
from minicolmap.semantics.project import load_config, project, sha256, write_json
from minicolmap.semantics.encoders import gpu_session

ROOT = ref.ROOT
WORK = ROOT/'data/langsplat_raw3'
REPORT = ROOT/'reports/langsplat-raw3-20261021'
SPEC = next(s for s in VARIANTS if s['name']=='no_latent_norm')


def raw_renderer_tree(source):
    tree = ast.parse(source)
    matches = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and len(node.targets)==1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id=='language_feature_precomp'
                and isinstance(node.value, ast.BinOp) and isinstance(node.value.op, ast.Div)):
            matches.append(node)
    if len(matches)!=1:
        raise ValueError('Expected exactly one pinned feature normalization assignment')
    target = matches[0]
    for node in ast.walk(tree):
        for _, value in ast.iter_fields(node):
            if isinstance(value, list) and target in value:
                value.remove(target)
    return ast.fix_missing_locations(tree)


def install_renderer():
    import gaussian_renderer
    source = ref.UP/'gaussian_renderer/__init__.py'
    namespace = dict(gaussian_renderer.__dict__)
    exec(compile(raw_renderer_tree(source.read_text()), str(source)+' [raw-feature adapter]', 'exec'), namespace)
    gaussian_renderer.render = namespace['render']


def configure():
    ref.WORK, ref.REPORT = WORK, REPORT
    ref.upstream_ae = lambda device='cuda': codec(SPEC).to(device)
    WORK.mkdir(parents=True, exist_ok=True)
    REPORT.mkdir(parents=True, exist_ok=True)
    return load_config(ROOT/'configs/semantic_waldo.json')


def encode(prepared):
    source = ROOT/'data/semantic_codec_study/no_latent_norm/best.pth'
    target = WORK/'autoencoder/best_ckpt.pth'
    target.parent.mkdir(exist_ok=True)
    if target.exists() and sha256(target)!=sha256(source):
        raise ValueError('Selected codec identity changed')
    if not target.exists():
        shutil.copy2(source,target)
    features = WORK/'language_features'
    if not features.exists():
        features.symlink_to(ROOT/'data/langsplat_reference/language_features',target_is_directory=True)
    model = ref.upstream_ae()
    model.load_state_dict(torch.load(target,weights_only=True))
    model.eval()
    directory = WORK/'language_features_dim3'
    directory.mkdir(exist_ok=True)
    norms = []
    with torch.inference_mode():
        for frame in prepared['frames']:
            stem = Path(frame['name']).stem
            values = torch.from_numpy(np.load(features/f'{stem}_f.npy')).cuda()
            codes = torch.cat([model.encode(v) for v in values.split(256)]).cpu().numpy()
            if not np.isfinite(codes).all():
                raise ValueError('Nonfinite raw latent codes')
            np.save(directory/f'{stem}_f.npy', codes)
            np.save(directory/f'{stem}_s.npy', np.load(features/f'{stem}_s.npy'))
            norms.extend(np.linalg.norm(codes,axis=-1).tolist())
    write_json(REPORT/'encoding.json',dict(images=len(prepared['frames']),latent_dim=3,
        normalized=False,mean_latent_norm=float(np.mean(norms)),autoencoder_sha256=sha256(target)))
    write_json(REPORT/'manifest.json',dict(codec=SPEC,gpu=7,seed=0,
        renderer_source_sha256=sha256(ref.UP/'gaussian_renderer/__init__.py'),
        adapter_sha256=sha256(Path(__file__)),rgb_source_sha256=prepared['rgb_checkpoint_sha256'],
        train_ids=prepared['splits']['train'],scope='Frozen RGB; matched 173 language views; raw 3D features; no alpha division'))


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('stage',choices=('encode','train','evaluate','query'))
    parser.add_argument('--level',type=int,choices=(1,2,3),default=1)
    parser.add_argument('--steps',type=int,choices=(100,30000),default=30000)
    parser.add_argument('--split',choices=('dev','test'),default='dev')
    parser.add_argument('--image-id',type=int)
    parser.add_argument('--text')
    args = parser.parse_args()
    config = configure()
    prepared = project(config)
    with gpu_session({**config,'workspace':str(REPORT)}):
        ref.seed()
        if args.stage=='encode':
            encode(prepared)
            return
        _, GaussianModel = ref.native_imports()
        if args.stage=='train' and args.steps==100 and not (REPORT/'geometry-probe.json').exists():
            ref.geometry_probe(config,prepared,GaussianModel,ref.rgb_checkpoint(config,prepared))
        install_renderer()
        if args.stage=='train':
            ref.train(config,prepared,args.level,args.steps)
            directory = WORK/('train' if args.steps==30000 else f'smoke-{args.steps}')/f'level{args.level}'
            marker = json.loads((directory/'completed.json').read_text())
            marker.update(unit_gaussian_features=False,codec='no_latent_norm',raw_adapter_sha256=sha256(Path(__file__)))
            write_json(directory/'completed.json',marker)
        elif args.stage=='evaluate':
            ref.evaluate(config,prepared,'field',args.split)
        else:
            ref.query(config,prepared,args.image_id,args.text)


if __name__=='__main__':
    main()
