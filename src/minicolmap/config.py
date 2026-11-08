"""Small, validated JSON configuration; thresholds are dataset-dependent starting values."""
import copy
import json
import math
from pathlib import Path
from typing import Literal, TypedDict, cast


class FeatureConfig(TypedDict):
    """SIFT frontend options; not read directly by the mapper."""

    max_features: int
    ratio: float


class GeometryOverrides(TypedDict, total=False):
    min_pair_inliers: int  # Omitted: use min_inliers, preserving legacy behavior.


class GeometryConfig(GeometryOverrides):
    """Pair geometry: squared-Sampson threshold is threshold_px ** 2."""

    threshold_px: float
    max_trials: int
    confidence: float  # Target probability in (0, 1), subject to max_trials.
    min_trials: int  # Positive minimum; the hard max_trials cap takes precedence.
    min_inliers: int  # M1's minimum accepted initial points; legacy pair fallback.


class MappingOverrides(TypedDict, total=False):
    track_conflict_policy: Literal['reject', 'split']


class MappingConfig(MappingOverrides):
    """M1-M3 policy. Distances are pixels and min_angle_deg is in degrees."""

    min_pnp_points: int  # Unique input pairs AND retained M2 inliers required.
    pnp_threshold_px: float  # Passed to backends.solve_pnp, not squared.
    min_angle_deg: float  # Convert with np.deg2rad before radian comparisons.
    max_reprojection_px: float  # M1/M3 points and M2's added observations.


class BundleAdjustmentConfig(TypedDict):
    """Optimizer options; the pipeline, not the mapper, schedules BA."""

    enabled: bool
    max_nfev: int
    loss: Literal['linear', 'huber', 'soft_l1']
    f_scale: float


class PipelineConfig(TypedDict):
    """Full, merged load_config() result passed to every mapper function.

    This describes the complete output, not a partial JSON override.
    Defaults, units and stage rules: docs/configuration.md.
    """

    seed: int
    features: FeatureConfig
    geometry: GeometryConfig
    mapping: MappingConfig
    bundle_adjustment: BundleAdjustmentConfig

DEFAULT = {
    'seed': 0,
    'features': {'max_features': 4000, 'ratio': 0.75},
    'geometry': {'threshold_px': 1.5, 'max_trials': 2000, 'confidence': 0.99,
                 'min_trials': 20, 'min_inliers': 30},
    'mapping': {'min_pnp_points': 15, 'pnp_threshold_px': 3.0, 'min_angle_deg': 1.5,
                'max_reprojection_px': 3.0},
    'bundle_adjustment': {'enabled': False, 'max_nfev': 50, 'loss': 'huber', 'f_scale': 1.0},
}


def load_config(path: str | Path | None = None) -> PipelineConfig:
    """Merge a partial JSON override into DEFAULT, validate, and return a fresh full config.

    Use load_config() for defaults or load_config('configs/my_scene.json')
    for overrides. Pass the entire result to mapper functions, not just
    result['mapping']. Unknown keys are rejected. Returned configs are independent.
    """
    supplied = json.loads(Path(path).read_text(encoding='utf-8')) if path is not None else None
    if path is not None and not isinstance(supplied, dict):
        raise ValueError('Config must be a JSON object')
    return resolve_config(supplied)


def resolve_config(overrides: dict | None = None) -> PipelineConfig:
    """Merge in-memory overrides using the same validation as load_config()."""
    result = copy.deepcopy(DEFAULT)
    if overrides is not None:
        if not isinstance(overrides, dict):
            raise ValueError('Config must be a JSON object')
        for key, value in overrides.items():
            if key not in result:
                raise ValueError(f'Unknown config section: {key}')
            if isinstance(result[key], dict):
                optional = {'geometry': {'min_pair_inliers'},
                            'mapping': {'track_conflict_policy'}}.get(key, set())
                if not isinstance(value, dict) or set(value) - (set(result[key]) | optional):
                    raise ValueError(f'Invalid keys in config section: {key}')
                result[key].update(value)
            else:
                result[key] = value
    def number(section, key, lower, upper=None, integer=False, lower_open=False):
        value = result[section][key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f'{section}.{key} must be a finite number')
        if value < lower or (lower_open and value == lower) or (upper is not None and value >= upper) or (integer and not isinstance(value, int)):
            raise ValueError(f'Invalid value for {section}.{key}')
    if not isinstance(result['seed'], int) or isinstance(result['seed'], bool) or result['seed'] < 0:
        raise ValueError('seed must be a nonnegative integer')
    number('features', 'max_features', 2, integer=True)
    number('features', 'ratio', 1e-12, 1)
    number('geometry', 'threshold_px', 1e-12)
    number('geometry', 'max_trials', 1, integer=True)
    number('geometry', 'confidence', 0, 1, lower_open=True)
    number('geometry', 'min_trials', 1, integer=True)
    number('geometry', 'min_inliers', 8, integer=True)
    if 'min_pair_inliers' in result['geometry']:
        number('geometry', 'min_pair_inliers', 8, integer=True)
    if result['mapping'].get('track_conflict_policy', 'reject') not in ('reject', 'split'):
        raise ValueError('mapping.track_conflict_policy must be reject or split')
    number('mapping', 'min_pnp_points', 6, integer=True)
    number('mapping', 'pnp_threshold_px', 1e-12)
    number('mapping', 'min_angle_deg', 1e-12, 180)
    number('mapping', 'max_reprojection_px', 1e-12)
    number('bundle_adjustment', 'max_nfev', 1, integer=True)
    number('bundle_adjustment', 'f_scale', 1e-12)
    if type(result['bundle_adjustment']['enabled']) is not bool:
        raise ValueError('bundle_adjustment.enabled must be a bool')
    if result['bundle_adjustment']['loss'] not in ('linear', 'huber', 'soft_l1'):
        raise ValueError('Unsupported bundle-adjustment loss')
    return cast(PipelineConfig, result)

