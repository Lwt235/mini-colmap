import { describe, expect, it } from 'vitest';
import * as THREE from 'three';
import {
  captureRenderCamera,
  parseRenderCamera,
  renderingConfigError,
  renderingSourceRequest,
  renderingSources,
} from './rendering';
import { alignedView } from './cameraView';
import type { Camera, DatasetSummary, RenderingConfig, Run } from './types';

const defaults: RenderingConfig = {
  method: 'nerf',
  iterations: 1000,
  learning_rate: 0.001,
  image_downscale: 4,
  rays_per_batch: 256,
  samples_per_ray: 32,
  near: 0.1,
  far: 4,
  holdout_every: 4,
  seed: 0,
  device: 'cpu',
  preview_every: 100,
  nerf: { levels_xyz: 6, levels_dir: 4, hidden_dim: 64 },
  gaussian: { initial_scale: 0.01, initial_opacity: 0.1, max_gaussians: 10000, densify_every: 0 },
};

describe('rendering input contract', () => {
  it('accepts legacy configs and validates independent optimization policies', () => {
    expect(renderingConfigError(defaults)).toBeNull();
    const revised = {
      ...defaults,
      gaussian: {
        ...defaults.gaussian,
        optimizer_state: 'preserve' as const,
        lr_mode: 'grouped' as const,
        loss: 'l1_ssim' as const,
        density_strategy: 'adaptive' as const,
      },
    };
    expect(renderingConfigError(revised)).toBeNull();
    expect(
      renderingConfigError({ ...revised, gaussian: { ...revised.gaussian, ssim_weight: 2 } }),
    ).toContain('ssim_weight');
    expect(
      renderingConfigError({
        ...revised,
        gaussian: { ...revised.gaussian, densify_start: 100, densify_stop: 50 },
      }),
    ).toContain('结束步');
    expect(
      renderingConfigError({
        ...revised,
        gaussian: { ...revised.gaussian, opacity_reset_every: -1 },
      }),
    ).toContain('非负整数');
  });
  it('accepts up to five million Gaussians and rejects invalid capacity settings', () => {
    for (const count of [1, 100001, 1000000, 5000000]) {
      expect(
        renderingConfigError({
          ...defaults,
          gaussian: { ...defaults.gaussian, max_gaussians: count },
        }),
      ).toBeNull();
    }
    for (const count of [0, -1, 1.5, NaN, Infinity, 5000001]) {
      expect(
        renderingConfigError({
          ...defaults,
          gaussian: { ...defaults.gaussian, max_gaussians: count },
        }),
      ).toContain('最大高斯数');
    }
  });
  it('validates SH degree, schedule and the explicit CUDA backend without changing old configs', () => {
    expect(renderingConfigError(defaults)).toBeNull();
    for (const degree of [-1, 4, 1.5, NaN]) {
      expect(
        renderingConfigError({
          ...defaults,
          gaussian: { ...defaults.gaussian, sh_degree: degree },
        }),
      ).toContain('球谐阶数');
    }
    for (const interval of [-1, 0.5, 1000001]) {
      expect(
        renderingConfigError({
          ...defaults,
          gaussian: { ...defaults.gaussian, sh_degree_interval: interval },
        }),
      ).toContain('升阶间隔');
    }
    const cuda = {
      ...defaults,
      method: '3dgs' as const,
      gaussian: { ...defaults.gaussian, sh_degree: 3, backend: 'gsplat' as const },
    };
    expect(renderingConfigError(cuda)).toContain('CUDA');
    expect(renderingConfigError({ ...cuda, device: 'cuda' })).toBeNull();
    expect(
      renderingConfigError({
        ...cuda,
        gaussian: { ...cuda.gaussian, backend: 'invalid' as 'gsplat' },
      }),
    ).toContain('光栅后端');
  });
  it('separates NeRF camera requirements from 3DGS point cloud requirements', () => {
    const source = {
      id: 'sfm',
      status: 'completed',
      images_available: true,
      registered_images: 8,
      point_count: 1283,
    } as Run;
    expect(
      renderingSources(
        [
          source,
          { ...source, id: 'train', pipeline: 'nerf' },
          { ...source, id: 'photos', images_available: false },
          { ...source, id: 'empty', point_count: 0 },
          { ...source, id: 'one', registered_images: 1 },
          { ...source, id: 'running', status: 'running' },
        ],
        '3dgs',
      ).map((r) => r.id),
    ).toEqual(['sfm', 'photos']);
    expect(renderingSources([{ ...source, point_count: 0 }], 'nerf')).toHaveLength(1);
  });
  it('accepts a point-free calibrated result only for NeRF', () => {
    const source = {
      id: 'pose-only',
      status: 'imported',
      images_available: true,
      registered_images: 3,
      point_count: 0,
    } as Run;
    expect(renderingSourceRequest('nerf', 'result', source.id, '', [source], [])).toEqual({
      source_run_id: source.id,
    });
    expect(() => renderingSourceRequest('3dgs', 'result', source.id, '', [source], [])).toThrow(
      '点云',
    );
  });
  it('accepts posed photos without a model and never substitutes guessed poses', () => {
    const dataset = { id: 'photos', image_count: 3, pose_count: 3 } as DatasetSummary;
    expect(renderingSourceRequest('nerf', 'dataset', dataset.id, '', [], [dataset])).toEqual({
      dataset_id: dataset.id,
    });
    expect(() =>
      renderingSourceRequest(
        'nerf',
        'dataset',
        dataset.id,
        '',
        [],
        [{ ...dataset, pose_count: 0 }],
      ),
    ).toThrow('真实相机位姿');
    expect(() => renderingSourceRequest('3dgs', 'dataset', dataset.id, '', [], [dataset])).toThrow(
      '点云',
    );
  });
  it('requires explicit matching photos for an imported model without original images', () => {
    const source = {
      id: 'imported',
      status: 'imported',
      images_available: false,
      registered_images: 3,
      point_count: 7,
    } as Run;
    const photos = { id: 'matching-photos', image_count: 3, pose_count: 0 } as DatasetSummary;
    expect(() =>
      renderingSourceRequest('3dgs', 'result', source.id, '', [source], [photos]),
    ).toThrow('关联');
    expect(
      renderingSourceRequest('3dgs', 'result', source.id, photos.id, [source], [photos]),
    ).toEqual({ source_run_id: source.id, photo_dataset_id: photos.id });
  });
  it('catches a reversed sampling interval before starting a worker', () => {
    expect(renderingConfigError(defaults)).toBeNull();
    expect(renderingConfigError({ ...defaults, near: 4, far: 1 })).toContain('near');
    expect(renderingConfigError({ ...defaults, near: 0 })).toBeNull();
  });
  it('accepts new sampling controls and preserves old configs without them', () => {
    expect(renderingConfigError(defaults)).toBeNull();
    expect(
      renderingConfigError({
        ...defaults,
        background: 'black',
        stratified_sampling: true,
        ray_sampling: 'all_images',
      }),
    ).toBeNull();
    expect(
      renderingConfigError({ ...defaults, background: 'red' } as unknown as RenderingConfig),
    ).toContain('背景');
    expect(
      renderingConfigError({
        ...defaults,
        ray_sampling: 'validation',
      } as unknown as RenderingConfig),
    ).toContain('光线');
    expect(
      renderingConfigError({ ...defaults, stratified_sampling: 1 } as unknown as RenderingConfig),
    ).toContain('布尔');
  });
  it('rejects NaN, fractional iteration budgets and invalid opacity', () => {
    expect(renderingConfigError({ ...defaults, learning_rate: NaN })).toBeTruthy();
    expect(renderingConfigError({ ...defaults, iterations: 3.5 })).toBeTruthy();
    expect(
      renderingConfigError({ ...defaults, gaussian: { ...defaults.gaussian, initial_opacity: 1 } }),
    ).toBeTruthy();
  });
  it('rejects malformed camera arrays and output sizes', () => {
    expect(() => parseRenderCamera('{"K":[]}')).toThrow();
    expect(() =>
      parseRenderCamera(
        JSON.stringify({
          K: [
            [1, 0, 0],
            [0, 1, 0],
            [0, 0, 1],
          ],
          R: [
            [1, 0, 0],
            [0, 1, 0],
            [0, 0, 1],
          ],
          t: [0, 0, 0],
          width: -1,
          height: 2,
        }),
      ),
    ).toThrow('正整数');
  });
});

describe('free view to OpenCV rendering camera', () => {
  it.each([false, true])(
    'matches independent Three projected pixels after a non-unit pose, portrait=%s',
    (portrait) => {
      const camera = new THREE.PerspectiveCamera(57, portrait ? 0.7 : 1.6, 0.01, 100);
      camera.position.set(3, -2, 4);
      camera.rotation.set(0.3, -0.4, 0.2);
      camera.zoom = 1.3;
      camera.updateProjectionMatrix();
      camera.updateMatrixWorld(true);
      const before = camera.matrixWorld.toArray();
      const converted = captureRenderCamera(camera);
      expect(Math.max(converted.width, converted.height)).toBe(256);
      for (const local of [
        [0.2, -0.3, -2],
        [-0.4, 0.1, -3],
        [0, 0, -1],
      ]) {
        const world = new THREE.Vector3(...local).applyMatrix4(camera.matrixWorld);
        const ndc = world.clone().project(camera);
        const expected = [
          ((ndc.x + 1) * converted.width) / 2 - 0.5,
          ((1 - ndc.y) * converted.height) / 2 - 0.5,
        ];
        const xyz = converted.R.map((row, i) =>
          row.reduce((sum, v, j) => sum + v * world.getComponent(j), converted.t[i]),
        );
        expect(xyz[2]).toBeGreaterThan(0);
        expect((converted.K[0][0] * xyz[0]) / xyz[2] + converted.K[0][2]).toBeCloseTo(
          expected[0],
          9,
        );
        expect((converted.K[1][1] * xyz[1]) / xyz[2] + converted.K[1][2]).toBeCloseTo(
          expected[1],
          9,
        );
      }
      expect(camera.matrixWorld.toArray()).toEqual(before);
      expect(parseRenderCamera(JSON.stringify(converted))).toEqual(converted);
    },
  );
  it('round-trips the saved non-identity SfM pose through aligned free view', () => {
    const rotation = new THREE.Matrix4().makeRotationFromEuler(new THREE.Euler(0.2, 0.4, -0.1));
    const e = rotation.elements;
    const source = {
      registered: true,
      R: [
        [e[0], e[4], e[8]],
        [e[1], e[5], e[9]],
        [e[2], e[6], e[10]],
      ],
      t: [0.4, -0.2, 1.3],
      K: [
        [400, 0, 319.5],
        [0, 400, 239.5],
        [0, 0, 1],
      ],
      width: 640,
      height: 480,
    } as Camera;
    const camera = alignedView(source, 640 / 480, 0.01, 100)!;
    const converted = captureRenderCamera(camera);
    converted.R.forEach((row, i) =>
      row.forEach((value, j) => expect(value).toBeCloseTo(source.R![i][j], 10)),
    );
    converted.t.forEach((value, i) => expect(value).toBeCloseTo(source.t![i], 10));
  });
});
