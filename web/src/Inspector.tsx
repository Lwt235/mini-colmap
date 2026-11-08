import { useEffect, useState } from 'react';
import {
  Camera as CameraIcon,
  ChevronRight,
  Crosshair,
  ExternalLink,
  Focus,
  Info,
} from 'lucide-react';
import { api, number } from './api';
import type { Camera, Dataset, PointDetail, Scene, Selection } from './types';
function Matrix({ label, values }: { label: string; values: number[][] }) {
  return (
    <div className="matrix-block">
      <span>{label}</span>
      <div className="matrix">
        {values.map((row, i) => (
          <div key={i}>
            {row.map((v, j) => (
              <code key={j}>{number(v, 4)}</code>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}
function Photo({
  url,
  width,
  height,
  xy,
  projected,
}: {
  url?: string;
  width?: number;
  height?: number;
  xy?: number[];
  projected?: number[];
}) {
  const [failed, setFailed] = useState(false),
    [size, setSize] = useState([width ?? 1, height ?? 1]);
  useEffect(() => {
    setFailed(false);
    setSize([width ?? 1, height ?? 1]);
  }, [url, width, height]);
  return (
    <div className="photo-preview">
      {url && !failed ? (
        <>
          <img
            src={url}
            alt="选中相机的原始照片"
            onError={() => setFailed(true)}
            onLoad={(e) => setSize([e.currentTarget.naturalWidth, e.currentTarget.naturalHeight])}
          />
          {xy && (
            <svg viewBox={`0 0 ${size[0]} ${size[1]}`} aria-label="观测点和重投影点">
              <circle
                cx={xy[0]}
                cy={xy[1]}
                r={size[0] * 0.013}
                fill="none"
                stroke="#62efbd"
                strokeWidth={size[0] * 0.004}
              />
              <path
                d={`M${xy[0] - size[0] * 0.02},${xy[1]}h${size[0] * 0.04} M${xy[0]},${xy[1] - size[0] * 0.02}v${size[0] * 0.04}`}
                stroke="#62efbd"
                strokeWidth={size[0] * 0.0025}
              />
              {projected && (
                <>
                  <line
                    x1={xy[0]}
                    y1={xy[1]}
                    x2={projected[0]}
                    y2={projected[1]}
                    stroke="#ffc478"
                    strokeWidth={size[0] * 0.003}
                  />
                  <circle cx={projected[0]} cy={projected[1]} r={size[0] * 0.006} fill="#ffc478" />
                </>
              )}
            </svg>
          )}
          <a
            href={url}
            target="_blank"
            rel="noreferrer"
            className="photo-open"
            title="打开原始照片"
          >
            <ExternalLink size={12} />
          </a>
        </>
      ) : (
        <div className="photo-missing">
          <CameraIcon size={24} />
          <span>原始照片不可用</span>
          <small>仍可检查三维模型和位姿</small>
        </div>
      )}
    </div>
  );
}
export default function Inspector({
  scene,
  inputDataset,
  runId,
  snapshot,
  selection,
  onSelect,
  onViewCamera,
  viewCameraId,
}: {
  scene: Scene | null;
  inputDataset: Dataset | null;
  runId: string;
  snapshot: string;
  selection: Selection;
  onSelect: (s: Selection) => void;
  onViewCamera: (imageId: number) => void;
  viewCameraId: number | null;
}) {
  const [point, setPoint] = useState<PointDetail | null>(null),
    [error, setError] = useState(''),
    [obsIndex, setObsIndex] = useState(0),
    [loading, setLoading] = useState(false);
  useEffect(() => {
    setPoint(null);
    setError('');
    setObsIndex(0);
    if (selection?.kind !== 'point' || !runId) return;
    let cancelled = false;
    setLoading(true);
    api<PointDetail>(
      `/api/runs/${encodeURIComponent(runId)}/points/${selection.id}?snapshot=${encodeURIComponent(snapshot)}`,
    )
      .then((v) => {
        if (!cancelled) setPoint(v);
      })
      .catch((e) => {
        if (!cancelled) setError(String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [runId, snapshot, selection]);
  useEffect(() => {
    if (!point || viewCameraId == null) return;
    const index = point.observations.findIndex((o) => o.image_id === viewCameraId);
    if (index >= 0) setObsIndex(index);
  }, [point, viewCameraId]);
  let camera: Camera | undefined =
    selection?.kind === 'camera'
      ? scene?.cameras.find((c) => c.image_id === selection.id)
      : undefined;
  if (!scene && selection?.kind === 'camera' && inputDataset) {
    const image = inputDataset.images.find((i) => i.id === selection.id);
    const calibration = inputDataset.cameras.find((c) => c.id === image?.camera_id);
    if (image && calibration) {
      camera = {
        ...image,
        image_id: image.id,
        K: calibration.K,
        registered: false,
        observations: 0,
      };
    }
  }
  const obs = point?.observations[obsIndex],
    obsCam = scene?.cameras.find((c) => c.image_id === obs?.image_id);
  if (!selection)
    return (
      <aside className="inspector">
        <div className="panel-heading">
          <Info size={14} />
          场景检查器
        </div>
        <div className="inspector-empty">
          <div className="inspector-symbol">
            <Crosshair size={26} />
          </div>
          <h3>探索你的重建</h3>
          <p>
            点选三维点或相机，
            <br />
            查看对应照片、位姿与观测。
          </p>
          <div className="legend">
            <span>
              <i className="legend-point" />
              三维点
            </span>
            <span>
              <i className="legend-camera" />
              注册相机
            </span>
            <span>
              <i className="legend-selected" />
              当前选中
            </span>
          </div>
        </div>
        <div className="inspector-hint">
          <Info size={14} />
          <p>所有坐标、颜色与位姿均来自算法结果。视图操作不会修改地图。</p>
        </div>
      </aside>
    );
  return (
    <aside className="inspector">
      <div className="panel-heading">
        {camera ? <CameraIcon size={14} /> : <Focus size={14} />}{' '}
        {camera ? '相机信息' : '三维点信息'}
        <button className="text-button" onClick={() => onSelect(null)}>
          取消选择
        </button>
      </div>
      <div className="inspector-scroll">
        {camera ? (
          <>
            <div className="object-title">
              <span className="object-icon">
                <CameraIcon size={20} />
              </span>
              <div>
                <strong>{camera.name}</strong>
                <span>IMAGE #{camera.image_id}</span>
              </div>
              <i className={`status-dot ${camera.registered ? 'good' : ''}`} />
            </div>
            <Photo url={camera.image_url} width={camera.width} height={camera.height} />
            {camera.registered && camera.R && camera.t && (
              <button
                className="outline full camera-view-action"
                onClick={() => onViewCamera(camera.image_id)}
              >
                <CameraIcon size={14} /> 对齐至此相机
              </button>
            )}
            <div className="detail-grid">
              <span>注册状态</span>
              <strong>{camera.registered ? '已注册' : '未注册'}</strong>
              <span>图像尺寸</span>
              <strong>
                {camera.width} × {camera.height}
              </strong>
              <span>相机 ID</span>
              <strong>{camera.camera_id}</strong>
              <span>有效观测</span>
              <strong>{number(camera.observations, 0)}</strong>
            </div>
            <Matrix label="内参 K" values={camera.K} />
            {camera.registered && camera.R && camera.t ? (
              <>
                <Matrix label="世界 → 相机旋转 R" values={camera.R} />
                <Matrix label="平移 t" values={[camera.t]} />
                {camera.center && <Matrix label="相机中心 C = −Rᵀt" values={[camera.center]} />}
                <p className="form-note">相机局部坐标：+Z 朝前，+Y 朝下。位置使用重建尺度。</p>
              </>
            ) : (
              <div className="info-note">
                该图像尚未估计出相机位姿，因此不会在三维视图中生成相机。
              </div>
            )}
          </>
        ) : loading ? (
          <div className="loading-inline">读取点的观测轨迹…</div>
        ) : error ? (
          <div className="inline-error">{error}</div>
        ) : point ? (
          <>
            <div className="object-title">
              <span className="object-icon point-icon">
                <Crosshair size={20} />
              </span>
              <div>
                <strong>三维点 #{point.id}</strong>
                <span>POINT INSPECTOR</span>
              </div>
              <span
                className="rgb-swatch"
                style={{ background: `rgb(${point.color.join(',')})` }}
              />
            </div>
            <Matrix label="世界坐标 XYZ" values={[point.xyz]} />
            <div className="detail-grid">
              <span>RGB 颜色</span>
              <strong>{point.color.join(', ')}</strong>
              <span>轨迹长度</span>
              <strong>{point.track_length} 张图像</strong>
              <span>平均误差</span>
              <strong>{number(point.mean_error)} px</strong>
            </div>
            <div className="section-label">
              关联观测 <span>{point.observations.length}</span>
            </div>
            {viewCameraId != null &&
              !point.observations.some((o) => o.image_id === viewCameraId) && (
                <div className="info-note">
                  上次对齐的相机没有该点的记录观测；下方显示其他相机的观测。
                </div>
              )}
            <div className="observation-list">
              {point.observations.map((o, i) => (
                <button
                  key={`${o.image_id}-${o.keypoint_index}`}
                  className={obsIndex === i ? 'selected' : ''}
                  onClick={() => setObsIndex(i)}
                >
                  <CameraIcon size={13} />
                  <span>
                    {scene?.cameras.find((c) => c.image_id === o.image_id)?.name ??
                      `图像 #${o.image_id}`}
                  </span>
                  <small>{number(o.error)} px</small>
                  <ChevronRight size={12} />
                </button>
              ))}
            </div>
            {obs && (
              <>
                <Photo
                  url={obs.image_url ?? obsCam?.image_url}
                  width={obsCam?.width}
                  height={obsCam?.height}
                  xy={obs.xy}
                  projected={obs.projected_xy}
                />
                <div className="pixel-legend">
                  <span>
                    <i />
                    观测位置
                  </span>
                  <span>
                    <i />
                    重投影位置
                  </span>
                </div>
                {obsCam?.registered && obsCam.R && obsCam.t && (
                  <button
                    className="outline full camera-view-action"
                    onClick={() => onViewCamera(obs.image_id)}
                  >
                    <CameraIcon size={14} /> 对齐至此观测相机
                  </button>
                )}
                <div className="detail-grid">
                  <span>特征点索引</span>
                  <strong>{obs.keypoint_index}</strong>
                  <span>观测像素</span>
                  <strong>{obs.xy.map((v) => number(v, 2)).join(', ')}</strong>
                  <span>投影像素</span>
                  <strong>{obs.projected_xy?.map((v) => number(v, 2)).join(', ') ?? '—'}</strong>
                </div>
                <button
                  className="outline full"
                  onClick={() => onSelect({ kind: 'camera', id: obs.image_id })}
                >
                  <CameraIcon size={13} />
                  检查这台相机
                </button>
              </>
            )}
          </>
        ) : null}
      </div>
    </aside>
  );
}
