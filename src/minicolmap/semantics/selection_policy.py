"""CPU-only selection identities and predeclared evidence gates.

These are consistency checks against language masks, not semantic accuracy or
confidence probabilities. Keep the policy version in every cache identity.
"""
import hashlib
import json
import re
from pathlib import Path

POLICY_VERSION = 'text-selection-v1'
POLICY = dict(version=POLICY_VERSION, fit_views=6, validation_views=5,
              steps=1200, seed=0, membership_threshold=.6,
              min_opacity=.005, voxel_size_scale=.025,
              min_gaussians=100, max_scene_fraction=.15,
              min_query_pixels=100, min_precision=.65, min_coverage=.5,
              min_iou=.4, min_good_validation_views=3,
              min_reprojection_iou=.4, min_foreground_agreement=.6,
              min_consistent_fit_views=3)


def valid_selection_id(value):
    return isinstance(value, str) and (value == 'sink-v5' or
        re.fullmatch(r'(?:object|scene)-[0-9a-f]{24}', value) is not None)


def normalized_text(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 160 or any(ord(c) < 32 for c in value):
        raise ValueError('请输入 1–160 字符的物体名称')
    return ' '.join(value.strip().casefold().split())


def selection_key(text, anchor, source_identity, hint=None):
    payload = dict(text=normalized_text(text), anchor=anchor, model='raw3',
                   source=source_identity, policy=POLICY)
    if hint is not None:payload['hint']=hint
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return 'object-' + digest[:24]


def assess_evidence(rows, consistency, selected, source_count):
    good = [r for r in rows if r['query_pixels'] >= POLICY['min_query_pixels']
            and r['selected_query_precision'] >= POLICY['min_precision']
            and r['query_coverage'] >= POLICY['min_coverage']
            and r['query_iou'] >= POLICY['min_iou']]
    consistent = [r for r in consistency if r.get('reprojection_iou') is not None
                  and r['reprojection_iou'] >= POLICY['min_reprojection_iou']
                  and r['foreground_agreement'] >= POLICY['min_foreground_agreement']]
    reasons = []
    if not POLICY['min_gaussians'] <= selected <= source_count * POLICY['max_scene_fraction']:
        reasons.append('选区过小或扩散到过大的场景范围')
    if len(rows) != POLICY['validation_views'] or len(good) < POLICY['min_good_validation_views']:
        reasons.append('五个独立视角中的选区与二维查询一致性不足')
    if len(consistent) < POLICY['min_consistent_fit_views']:
        reasons.append('拟合视角之间的可见表面重投影证据不足')
    return dict(accepted=not reasons, reasons=reasons, good_validation_views=len(good),
                consistent_fit_views=len(consistent), policy=POLICY,
                scope='Language-mask self-consistency only; not human 3D accuracy or calibrated confidence')


def source_identity(root):
    root = Path(root)
    prepared_path = root/'data/semantic_waldo/project.json'
    prepared = json.loads(prepared_path.read_text())
    config = json.loads((root/'configs/semantic_waldo.json').read_text())
    # Size + nanosecond mtime are cheap invalidation stamps. The worker records
    # actual SHA256 hashes and native_models verifies RGB/codec bindings.
    paths = [prepared_path, root/config.get('rgb_run', '__missing__')/'checkpoint.pt',
             root/'data/langsplat_raw3/autoencoder/best_ckpt.pth']
    for level in (1, 2, 3):
        folder = root/f'data/langsplat_raw3/train/level{level}'
        paths.extend([folder/'completed.json', folder/'chkpnt30000.pth'])
    stamps = []
    for path in paths:
        stat = path.stat()
        stamps.append([str(path.relative_to(root)), stat.st_size, stat.st_mtime_ns])
    return dict(rgb_sha256=prepared['rgb_checkpoint_sha256'], stamps=stamps)

