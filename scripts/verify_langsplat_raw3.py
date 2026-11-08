"""Final provenance and native rendering controls, after the finite campaign."""
import json
from pathlib import Path
from types import SimpleNamespace
import torch
import langsplat_raw3 as raw
from minicolmap.semantics.project import project,sha256,write_json
from minicolmap.semantics.encoders import gpu_session


def main():
    config = raw.configure()
    prepared = project(config)
    if not (raw.REPORT/'completed.json').is_file():
        raise ValueError('Training and final evaluations have not completed')
    expected = {
        'src/minicolmap/semantics/algorithms.py':'a2d3b7b1b9731035c872da0d43176ee6cdf6df1e8f707a9151b3e5fc866886c0',
        'outputs/ui_3dgs_20261019_230921_797f2a/checkpoint.pt':'fa56a42c1c1fc50db13eda64eb43c6d1445b7453abdac4e4c40a908a32059d5d',
        'data/semantic_waldo/autoencoder/checkpoint.pt':'1c21e15be21926d4f8e07dcbec5e9c0cfb3ccb8a2dbca065810c7aa0456146bb',
    }
    actual = {p:sha256(raw.ROOT/p) for p in expected}
    if expected!=actual:
        raise ValueError('An original learner/RGB/codec artifact changed')
    source_manifest=json.loads((raw.ROOT/'reports/langsplat-reference-20261021/source-manifest.json').read_text())
    if any(sha256(raw.ref.UP/p)!=value for p,value in source_manifest['files'].items()):
        raise ValueError('Pinned official source changed')
    if any(sha256(Path(p))!=value for p,value in source_manifest['native_libraries'].items()):
        raise ValueError('Pinned CUDA library changed')
    for level in (1,2,3):
        marker=json.loads((raw.WORK/f'train/level{level}/completed.json').read_text())
        if marker['iterations']!=30000 or not marker['geometry_unchanged'] or marker['unit_gaussian_features']:
            raise ValueError('Raw field completion/freeze contract failed')
    with gpu_session({**config,'workspace':str(raw.REPORT)}):
        raw.ref.seed()
        _,GaussianModel=raw.ref.native_imports()
        import gaussian_renderer
        original=gaussian_renderer.render
        model=GaussianModel(3)
        params,_=torch.load(raw.ref.rgb_checkpoint(config,prepared),map_location='cuda',weights_only=True)
        model.restore(params,SimpleNamespace(include_feature=True),mode='eval')
        for name in ('_xyz','_features_dc','_features_rest','_scaling','_rotation','_opacity'):
            getattr(model,name).requires_grad_(False)
        camera=raw.ref.native_camera(next(f for f in prepared['frames'] if f['image_id']==124))
        pipe=SimpleNamespace(debug=False,convert_SHs_python=False,compute_cov3D_python=False)
        opt=SimpleNamespace(include_feature=True)
        background=torch.ones(3,device='cuda')
        unit=torch.nn.functional.normalize(torch.ones(len(model.get_xyz),3,device='cuda'),dim=-1)
        with torch.no_grad():
            model._language_feature=unit
            expected_render=original(camera,model,pipe,background,opt)
            raw.install_renderer()
            actual_render=gaussian_renderer.render(camera,model,pipe,background,opt)
            torch.testing.assert_close(actual_render['render'],expected_render['render'],rtol=0,atol=0)
            torch.testing.assert_close(actual_render['language_feature_image'],expected_render['language_feature_image'],rtol=1e-5,atol=1e-6)
            model._language_feature=3*unit
            scaled=gaussian_renderer.render(camera,model,pipe,background,opt)
            torch.testing.assert_close(scaled['language_feature_image'],3*actual_render['language_feature_image'],rtol=1e-5,atol=2e-6)
            maximum_norm=float(scaled['language_feature_image'].norm(dim=0).max())
            if maximum_norm<2.9:
                raise ValueError('Raw field is still limited to unit magnitude')
        model._language_feature=torch.zeros_like(unit,requires_grad=True)
        output=gaussian_renderer.render(camera,model,pipe,background,opt)['language_feature_image']
        output.sum().backward()
        gradient=model._language_feature.grad
        if gradient is None or not torch.isfinite(gradient).all() or not gradient.abs().sum()>0:
            raise ValueError('Zero-initialized raw field has invalid/zero gradients')
    write_json(raw.REPORT/'acceptance.json',dict(status='passed',gpu=7,original_artifacts_preserved=actual,
        official_sources_unchanged=True,native_libraries_unchanged=True,geometry_frozen=True,
        unit_feature_render_parity=True,rgb_render_bit_exact=True,raw_feature_linear_scaling=True,
        maximum_rendered_norm=maximum_norm,zero_initialization_finite_nonzero_gradients=True))
    comparison=json.loads((raw.REPORT/'comparison.json').read_text())
    rows=['# 非归一化编码的三维验证','',
        '冻结同一个 RGB 模型，173 张语言训练视图；GPU 7、seed 0，每层 30,000 步。',
        '新编码器来自既有二维试验的 no_latent_norm，同时取消高斯渲染前的语言特征归一化，以匹配编码幅度。',
        '这是所选编码器与兼容三维表示的整体对照，不能将所有差异单独归因于一个归一化操作。','',
        '| 划分 | 模型 | 固定阈值 IoU | 官方流程 IoU |','|---|---|---:|---:|']
    for split,models in comparison.items():
        for name,metrics in models.items():
            rows.append(f"| {split} | {name} | {metrics['common']['mean_iou']:.4%} | {metrics['official']['mean_iou']:.4%} |")
    rows+=['','| 划分 | 模型 | 多边形内峰值率 | 官方框定位率 |','|---|---|---:|---:|']
    for split,models in comparison.items():
        for name,metrics in models.items():
            rows.append(f"| {split} | {name} | {metrics['common']['polygon_peak_rate']:.4%} | {metrics['official']['bbox_localization_rate']:.4%} |")
    rows+=['','官方流程的 Dev/Test 平均 IoU 提升，但固定阈值 Dev IoU 下降，Test 官方框定位率也下降。',
        '因此不能声称所有目标、阈值与定位指标都改善。默认模型按 Dev 官方流程 IoU 选择，Test 不参与选择。']
    rows+=['','所有参数固定后检查 test；既有研究已使用这些拆分，本次不是全新的盲测。',
        'RGB/SfM 可能包含评测图像，不代表端到端未见视角泛化，也不是完整论文复现。',
        '只有 seed 0，不对小幅波动宣称统计显著性。','',
        '验收见 acceptance.json；实际自由文本查询见 queries/。原学习代码、原 RGB、旧压缩器和官方源码均未改动。']
    (raw.REPORT/'README.md').write_text('\n'.join(rows)+'\n')
    print(json.dumps(comparison,indent=2))


if __name__=='__main__':
    main()
