import { Download, RotateCcw, Upload } from 'lucide-react';
import type { Config } from './types';
import { api, json, downloadJSON } from './api';
type Field = {
  key: string;
  label: string;
  unit?: string;
  help: string;
  step?: number;
  min?: number;
  max?: number;
};
const groups: {
  key: 'features' | 'geometry' | 'mapping' | 'bundle_adjustment';
  name: string;
  subtitle: string;
  fields: Field[];
}[] = [
  {
    key: 'features',
    name: '特征提取',
    subtitle: 'SIFT 与描述子匹配',
    fields: [
      {
        key: 'max_features',
        label: '最大特征数',
        unit: '点 / 图',
        help: '每张图像最多保留的 SIFT 特征数。',
        step: 100,
        min: 2,
      },
      {
        key: 'ratio',
        label: '匹配距离比',
        help: 'Lowe 比值阈值；越小筛选越严格。',
        step: 0.01,
        min: 0.01,
      },
    ],
  },
  {
    key: 'geometry',
    name: '几何验证',
    subtitle: 'RANSAC 与初始模型',
    fields: [
      {
        key: 'threshold_px',
        label: 'Sampson 阈值',
        unit: 'px',
        help: '内部使用该数值的平方筛选 Sampson 误差。',
        step: 0.1,
        min: 0.01,
      },
      {
        key: 'max_trials',
        label: '最大采样次数',
        unit: '次',
        help: '自适应 RANSAC 的采样硬上限；置信度要求更多次数时仍受此限制。',
        step: 100,
        min: 1,
      },
      {
        key: 'confidence',
        label: 'RANSAC 置信度',
        help: '目标概率，严格在0与1之间；提高置信度通常需要更多采样，仍受硬上限限制。',
        step: 0.001,
        min: 0,
        max: 1,
      },
      {
        key: 'min_trials',
        label: '最少采样次数',
        unit: '次',
        help: '提前停止前至少抽样的次数；若超过最大采样次数，则硬上限优先。',
        step: 1,
        min: 1,
      },
      {
        key: 'min_pair_inliers',
        label: '图像对最少内点',
        unit: '点',
        help: '通过几何验证的图像对进入匹配图的门槛；可以低于初始化点数。',
        step: 1,
        min: 8,
      },
      {
        key: 'min_inliers',
        label: '初始化最少点数',
        unit: '点',
        help: '初始化最终有效三维点的下限；未单独设置图像对门槛时也用于图像对准入。',
        step: 1,
        min: 8,
      },
    ],
  },
  {
    key: 'mapping',
    name: '增量建图',
    subtitle: 'PnP、三角化与质量筛选',
    fields: [
      {
        key: 'min_pnp_points',
        label: 'PnP 最少对应点',
        unit: '点',
        help: '注册前和最终筛选后都需满足的 2D–3D 对应数量。',
        step: 1,
        min: 6,
      },
      {
        key: 'pnp_threshold_px',
        label: 'PnP 内点阈值',
        unit: 'px',
        help: '传给 PnP RANSAC 的像素距离阈值。',
        step: 0.1,
        min: 0.01,
      },
      {
        key: 'min_angle_deg',
        label: '最小三角化夹角',
        unit: '°',
        help: '夹角过小的点深度不稳定，将被拒绝。',
        step: 0.1,
        min: 0.01,
      },
      {
        key: 'max_reprojection_px',
        label: '最大重投影误差',
        unit: 'px',
        help: '初始化、补点及新增观测的像素误差上限。',
        step: 0.1,
        min: 0.01,
      },
    ],
  },
  {
    key: 'bundle_adjustment',
    name: '束调整 BA',
    subtitle: '对现有位姿与点进行整体优化',
    fields: [
      {
        key: 'max_nfev',
        label: '函数评估预算',
        unit: '次',
        help: '允许的最大函数评估次数；达到预算不代表已收敛。',
        step: 10,
        min: 1,
      },
      {
        key: 'f_scale',
        label: '鲁棒损失尺度',
        unit: 'px',
        help: '控制鲁棒损失对大残差的抑制尺度。',
        step: 0.1,
        min: 0.01,
      },
    ],
  },
];
export default function ConfigPanel({
  config,
  defaults,
  disabled,
  onChange,
  onValidate,
  onError,
}: {
  config: Config;
  defaults: Config;
  disabled: boolean;
  onChange: (c: Config) => void;
  onValidate: () => void;
  onError: (s: string) => void;
}) {
  function change(group: keyof Config, key: string, value: unknown) {
    onChange({ ...config, [group]: { ...(config[group] as object), [key]: value } });
  }
  return (
    <div className="config-panel">
      <div className="panel-intro">
        <br />
        参数在下一次运行时生效。
      </div>
      <div className="small-actions">
        <button
          title="恢复默认参数"
          disabled={disabled}
          onClick={() => onChange(structuredClone(defaults))}
        >
          <RotateCcw size={13} />
          默认
        </button>
        <label className={`button ${disabled ? 'disabled' : ''}`}>
          <Upload size={13} />
          导入
          <input
            type="file"
            accept=".json"
            disabled={disabled}
            hidden
            onChange={async (e) => {
              const f = e.target.files?.[0];
              if (!f) return;
              try {
                const parsed = JSON.parse(await f.text());
                onChange(await api<Config>('/api/config/validate', json(parsed)));
              } catch (err) {
                onError(String(err));
              }
              e.target.value = '';
            }}
          />
        </label>
        <button onClick={() => downloadJSON('mini-colmap-config.json', config)}>
          <Download size={13} />
          导出
        </button>
      </div>
      <fieldset disabled={disabled}>
        <label className="number-field seed-field">
          <span>
            随机种子 <small>复现实验</small>
          </span>
          <input
            type="number"
            min="0"
            step="1"
            value={config.seed}
            onChange={(e) => onChange({ ...config, seed: +e.target.value })}
          />
        </label>
        {groups.map((group) => (
          <details key={group.key} open className="config-group">
            <summary>
              {group.name}
              <span>{group.subtitle}</span>
            </summary>
            {group.key === 'bundle_adjustment' && (
              <label className="switch-row">
                启用最终 BA
                <input
                  type="checkbox"
                  checked={config.bundle_adjustment.enabled}
                  onChange={(e) => change('bundle_adjustment', 'enabled', e.target.checked)}
                />
              </label>
            )}
            {group.fields.map((field) => (
              <label className="number-field" key={field.key} title={field.help}>
                <span>
                  {field.label}
                  <small>{field.unit ?? ''}</small>
                </span>
                <input
                  type="number"
                  step={field.step ?? 1}
                  min={field.min}
                  max={field.max}
                  aria-label={field.label}
                  value={Number(
                    field.key === 'min_pair_inliers'
                      ? (config.geometry.min_pair_inliers ?? config.geometry.min_inliers)
                      : (config[group.key] as unknown as Record<string, unknown>)[field.key],
                  )}
                  onChange={(e) => change(group.key, field.key, +e.target.value)}
                />
              </label>
            ))}
            {group.key === 'geometry' && (
              <p className="form-note">
                自适应 RANSAC：置信度不是内点比例；最少采样次数超过硬上限时，按上限执行。
              </p>
            )}
            {group.key === 'mapping' && (
              <label
                className="number-field"
                title="拆分模式优先保留多视图一致的子轨迹，阻止同图不同特征合并。"
              >
                <span>轨迹冲突策略</span>
                <select
                  aria-label="轨迹冲突策略"
                  value={config.mapping.track_conflict_policy ?? 'reject'}
                  onChange={(e) => change('mapping', 'track_conflict_policy', e.target.value)}
                >
                  <option value="reject">整组丢弃（原有策略）</option>
                  <option value="split">保留一致子轨迹</option>
                </select>
              </label>
            )}
            {group.key === 'bundle_adjustment' && (
              <label className="number-field">
                <span>损失函数</span>
                <select
                  value={config.bundle_adjustment.loss}
                  onChange={(e) => change('bundle_adjustment', 'loss', e.target.value)}
                >
                  <option value="linear">Linear</option>
                  <option value="huber">Huber</option>
                  <option value="soft_l1">Soft L1</option>
                </select>
              </label>
            )}
          </details>
        ))}
      </fieldset>
      <button className="outline full" disabled={disabled} onClick={onValidate}>
        检查参数配置
      </button>
      <p className="form-note">悬停字段查看说明。初始化两视图优化与最终 BA 为不同步骤。</p>
    </div>
  );
}
