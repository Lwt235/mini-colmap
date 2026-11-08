import { useEffect, useRef, useState } from 'react';
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { Expand, Focus, LoaderCircle, Play, RotateCcw, Square } from 'lucide-react';
import type { Camera, Scene } from './types';
import type { RenderCamera } from './rendering';
import {
  selectionOverlay,
  type ObjectSelection,
  type SelectionMode,
  type BoxOverlay,
} from './objectSelection';
import { alignedView } from './cameraView';
import { number } from './api';
import {
  cameraCalibration,
  LatestFrameScheduler,
  LatestPaintQueue,
  liveIntrinsics,
  liveOutputSize,
  liveQualityTarget,
  liveRenderCamera,
  liveSceneBounds,
  setLiveProjection,
} from './gaussianLive';

type Props = {
  runId: string;
  scene: Scene | null;
  selectedCameraId: number | null;
  disabled?: boolean;
  presentation?: boolean;
  simple?: boolean;
  onPause?: () => void;
  objectSelection?: ObjectSelection;
  selectionMode?: SelectionMode;
  focusSelectionRequest?: number;
  autoStartRequest?: number;
  restoreSceneRequest?: number;
};
type Ready = { backend: string; device: string; gaussian_count: number; sh_degree: number };
type Frame = {
  type: 'frame';
  seq: number;
  width: number;
  height: number;
  render_ms: number;
  encode_ms: number;
  quality?: 'interactive' | 'final';
  selection?: { id: string; mode: 'highlight' | 'isolate' } | null;
};
type Metrics = {
  frames: number;
  seq: number;
  fps: number | null;
  render: number;
  encode: number;
  latency: number;
  width: number;
  height: number;
  quality?: 'interactive' | 'final';
  selection?: { id: string; mode: 'highlight' | 'isolate' } | null;
};
type Engine = {
  fit: () => void;
  align: (source: Camera) => void;
  refresh: () => void;
  focusSelection: () => void;
  restoreScene: () => void;
};
const sizes = {
  '640': [640, 480],
  '960': [960, 720],
  '1280': [1280, 720],
  '1920': [1920, 1080],
} as const;

export default function GaussianLiveViewer({
  runId,
  scene,
  selectedCameraId,
  disabled = false,
  presentation = false,
  simple = false,
  onPause,
  objectSelection,
  selectionMode = 'none',
  focusSelectionRequest = 0,
  autoStartRequest = 0,
  restoreSceneRequest = 0,
}: Props) {
  const [enabled, setEnabled] = useState(false);
  const [boxOverlay, setBoxOverlay] = useState<BoxOverlay>({ lines: [], center: null });
  const selectionRef = useRef(objectSelection);
  selectionRef.current = objectSelection;
  const modeRef = useRef(selectionMode);
  modeRef.current = selectionMode;
  const consumedStart = useRef(0);
  const [adaptive, setAdaptive] = useState(() => {
    if (simple) return true;
    try {
      return localStorage.getItem('minicolmap.live-adaptive') !== 'off';
    } catch {
      return true;
    }
  });
  const adaptiveRef = useRef(adaptive);
  adaptiveRef.current = adaptive;
  const [retry, setRetry] = useState(0);
  const [resolution, setResolution] = useState<keyof typeof sizes>(() => {
    if (simple) return '1280';
    try {
      const saved = localStorage.getItem('minicolmap.live-resolution');
      if (saved && saved in sizes) return saved as keyof typeof sizes;
    } catch {
      /* Storage may be unavailable in a private browser window. */
    }
    return '1280';
  });
  const [cameraId, setCameraId] = useState('');
  const [status, setStatus] = useState('尚未开启');
  const [error, setError] = useState('');
  const [ready, setReady] = useState<Ready | null>(null);
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const viewport = useRef<HTMLDivElement>(null);
  const fullscreen = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const image = useRef<HTMLImageElement>(null);
  const engine = useRef<Engine | null>(null);
  const resolutionRef = useRef(resolution);
  resolutionRef.current = resolution;
  const registered = scene?.cameras.filter((item) => item.registered && item.R && item.t) ?? [];
  const selectedRef = useRef(selectedCameraId);
  selectedRef.current = selectedCameraId;

  useEffect(() => {
    const selected = registered.find((item) => item.image_id === selectedCameraId) ?? registered[0];
    setCameraId(selected ? String(selected.image_id) : '');
  }, [scene?.revision, selectedCameraId]);
  useEffect(() => {
    setEnabled(false);
    setMetrics(null);
    setReady(null);
    setError('');
  }, [runId]);
  useEffect(() => {
    if (disabled) setEnabled(false);
  }, [disabled]);
  useEffect(() => {
    if (autoStartRequest > consumedStart.current && scene && !disabled) {
      consumedStart.current = autoStartRequest;
      setEnabled(true);
    }
  }, [autoStartRequest, scene?.revision, disabled]);
  useEffect(() => {
    engine.current?.refresh();
  }, [selectionMode, objectSelection?.id]);
  useEffect(() => {
    engine.current?.refresh();
    try {
      localStorage.setItem('minicolmap.live-resolution', resolution);
    } catch {
      /* Optional preference. */
    }
  }, [resolution]);

  useEffect(() => {
    if (!enabled || !viewport.current || !canvas.current || !scene) return;
    const element = viewport.current;
    const bounds = liveSceneBounds(scene);
    const initial =
      scene.cameras.find(
        (item) => item.image_id === selectedRef.current && item.registered && item.R && item.t,
      ) ?? scene.cameras.find((item) => item.registered && item.R && item.t);
    if (!initial) {
      setError('源模型缺少可用的注册相机。');
      return;
    }
    let disposed = false;
    let socket: WebSocket | null = null;
    let connected = false;
    let fatal = false;
    let reconnectTimer = 0;
    let timeoutTimer = 0;
    let settleTimer = 0;
    let reconnectCount = 0;
    let metadata: Frame | null = null;
    let displayedUrl = '';
    let frameCount = 0;
    let presentedTimes: number[] = [];
    let width = 1,
      height = 1;
    let calibration = cameraCalibration(initial);
    let K = initial.K;
    const decodeUrls = new Set<string>();
    const scheduler = new LatestFrameScheduler<{
      camera: RenderCamera;
      quality: 'interactive' | 'final';
      selection?: { id: string; mode: 'highlight' | 'isolate' };
    }>(2);
    const painter = new LatestPaintQueue<{
      seq: number;
      url: string;
      frame: Frame;
      completion: NonNullable<ReturnType<typeof scheduler.finish>>;
    }>(
      ({ url, frame, completion }) => {
        if (disposed || !scheduler.acceptDisplay(frame.seq, completion.generation)) {
          URL.revokeObjectURL(url);
          return;
        }
        if (image.current) image.current.src = url;
        const selection = selectionRef.current;
        setBoxOverlay(
          selection && completion.value.selection?.id === selection.id
            ? selectionOverlay(selection, completion.value.camera)
            : { lines: [], center: null },
        );
        if (displayedUrl) URL.revokeObjectURL(displayedUrl);
        displayedUrl = url;
        const now = performance.now();
        if (presentedTimes.length && now - presentedTimes.at(-1)! >= 1000) presentedTimes = [];
        presentedTimes.push(now);
        presentedTimes = presentedTimes.filter((time) => now - time <= 1000).slice(-30);
        const fps =
          presentedTimes.length >= 2
            ? (1000 * (presentedTimes.length - 1)) / (now - presentedTimes[0])
            : null;
        setMetrics({
          frames: ++frameCount,
          seq: frame.seq,
          fps,
          render: frame.render_ms,
          encode: frame.encode_ms,
          latency: now - completion.sentAt,
          width: frame.width,
          height: frame.height,
          quality: frame.quality,
          selection: frame.selection,
        });
      },
      ({ url }) => URL.revokeObjectURL(url),
      (callback) => requestAnimationFrame(callback),
      (id) => cancelAnimationFrame(id),
    );
    const camera = new THREE.PerspectiveCamera(
      45,
      1,
      Math.max(bounds.radius * 1e-5, 1e-7),
      Math.max(bounds.radius * 10000, 100),
    );
    const createControls = () => {
      const value = new OrbitControls(camera, canvas.current!);
      value.enableDamping = false;
      value.minDistance = Math.max(bounds.radius * 0.001, 1e-6);
      value.maxDistance = bounds.radius * 1000;
      value.screenSpacePanning = true;
      return value;
    };
    let controls = createControls();

    const pump = () => {
      if (!connected || socket?.readyState !== WebSocket.OPEN || disposed) return;
      const request = scheduler.begin(performance.now());
      if (!request) return;
      socket.send(JSON.stringify({ type: 'camera', seq: request.seq, ...request.value }));
      window.clearTimeout(timeoutTimer);
      timeoutTimer = window.setTimeout(() => {
        if (!disposed) {
          setStatus('等待图像超时，正在重新连接…');
          socket?.close();
        }
      }, 30000);
    };
    let currentQuality: 'interactive' | 'final' = 'final';
    const queue = () => {
      if (disposed || !width || !height) return;
      scheduler.queue({
        camera: liveRenderCamera(camera, K, width, height),
        quality: currentQuality,
        ...(selectionRef.current && modeRef.current !== 'none'
          ? {
              selection: {
                id: selectionRef.current.id,
                mode: modeRef.current as 'highlight' | 'isolate',
              },
            }
          : {}),
      });
      pump();
    };
    const resize = (interactive = false) => {
      const fullTarget = sizes[resolutionRef.current];
      const target = liveQualityTarget(
        fullTarget[0],
        fullTarget[1],
        interactive && adaptiveRef.current,
      );
      currentQuality = target.quality;
      const next = liveOutputSize(
        Math.max(element.clientWidth, 1),
        Math.max(element.clientHeight, 1),
        target.width,
        Math.min(window.devicePixelRatio || 1, 2),
        target.height,
      );
      width = next.width;
      height = next.height;
      K = liveIntrinsics(calibration, width, height);
      setLiveProjection(camera, K, width, height);
      queue();
    };
    const interaction = () => {
      resize(true);
      window.clearTimeout(settleTimer);
      settleTimer = window.setTimeout(() => resize(false), 180);
    };
    const fullResize = () => resize(false);
    const align = (source: Camera) => {
      const pose = alignedView(source, width / height, camera.near, camera.far);
      if (!pose) return;
      calibration = cameraCalibration(source);
      camera.position.copy(pose.position);
      camera.quaternion.copy(pose.quaternion);
      camera.up.copy(pose.up);
      const forward = camera.getWorldDirection(new THREE.Vector3());
      const distance = Math.max(camera.position.distanceTo(bounds.center), bounds.radius * 0.5);
      // OrbitControls caches its up-axis transform at construction. Recreate after alignment
      // so a rotated source camera still orbits about the newly aligned up direction.
      controls.removeEventListener('change', interaction);
      controls.dispose();
      controls = createControls();
      controls.target.copy(camera.position).addScaledVector(forward, distance);
      controls.update();
      controls.addEventListener('change', interaction);
      resize();
    };
    const fit = () => {
      const forward = camera.getWorldDirection(new THREE.Vector3());
      const distance = (bounds.radius / Math.sin(THREE.MathUtils.degToRad(camera.fov / 2))) * 1.15;
      controls.target.copy(bounds.center);
      camera.position.copy(bounds.center).addScaledVector(forward, -distance);
      controls.update();
      queue();
    };
    const focusSelection = () => {
      const selection = selectionRef.current;
      if (!selection) return;
      // Keep a real capture position. Pulling the camera far back to fit a
      // Gaussian-center box extrapolates outside the trained scene and can
      // expose opaque splat backsides, particularly when isolating an object.
      const source =
        registered.find((item) => item.image_id === selection.anchor_image_id) ?? initial;
      setCameraId(String(source.image_id));
      align(source);
      controls.target.set(...(selection.center as [number, number, number]));
      controls.update();
      fullResize();
    };
    engine.current = {
      fit,
      align,
      refresh: fullResize,
      focusSelection,
      restoreScene: () => {
        const source = registered.find((item) => item.image_id === selectedRef.current) ?? initial;
        setCameraId(String(source.image_id));
        align(source);
      },
    };
    align(initial);
    if (focusSelectionRequest > 0 && modeRef.current !== 'none') focusSelection();
    controls.addEventListener('change', interaction);
    const observer = new ResizeObserver(fullResize);
    observer.observe(element);
    window.addEventListener('resize', fullResize);

    const connect = () => {
      if (disposed) return;
      fatal = false;
      connected = false;
      scheduler.reset();
      painter.clear();
      presentedTimes = [];
      metadata = null;
      setReady(null);
      setBoxOverlay({ lines: [], center: null });
      setError('');
      setStatus(
        reconnectCount
          ? '连接中断，正在重连…'
          : simple
            ? '正在加载场景…'
            : '正在加载高斯模型到 GPU…',
      );
      const url = new URL(`/api/rendering/runs/${encodeURIComponent(runId)}/live`, location.href);
      url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
      const current = new WebSocket(url);
      socket = current;
      current.binaryType = 'blob';
      current.onmessage = (event) => {
        if (disposed || current !== socket) return;
        if (typeof event.data === 'string') {
          try {
            const message = JSON.parse(event.data);
            if (message.type === 'ready') {
              connected = true;
              reconnectCount = 0;
              setReady(message as Ready);
              setStatus(simple ? '可以旋转查看场景' : '已连接 · 自由视角');
              queue();
            } else if (message.type === 'frame') metadata = message as Frame;
            else if (message.type === 'error') {
              fatal = true;
              connected = false;
              window.clearTimeout(timeoutTimer);
              setStatus('渲染失败');
              setError(String(message.message ?? '服务器未能渲染此视角。'));
              current.close();
            }
          } catch {
            setError('实时渲染返回了无效的消息。');
          }
          return;
        }
        const frame = metadata;
        metadata = null;
        if (!frame) return;
        const completion = scheduler.finish(frame.seq, performance.now());
        if (!completion) return;
        window.clearTimeout(timeoutTimer);
        if (completion.display) {
          const url = URL.createObjectURL(event.data as Blob);
          decodeUrls.add(url);
          const decoded = new Image();
          decoded.onload = () => {
            decodeUrls.delete(url);
            if (disposed || current !== socket) {
              URL.revokeObjectURL(url);
              return;
            }
            painter.queue({ seq: frame.seq, url, frame, completion });
          };
          decoded.onerror = () => {
            decodeUrls.delete(url);
            URL.revokeObjectURL(url);
            setError('实时图像解码失败。');
          };
          decoded.src = url;
        }
        pump();
        if (scheduler.inFlight) {
          window.clearTimeout(timeoutTimer);
          timeoutTimer = window.setTimeout(() => {
            if (!disposed) {
              setStatus('等待图像超时，正在重新连接…');
              socket?.close();
            }
          }, 30000);
        }
      };
      current.onclose = () => {
        if (disposed || current !== socket) return;
        connected = false;
        window.clearTimeout(timeoutTimer);
        scheduler.reset();
        painter.clear();
        if (fatal) return;
        if (reconnectCount < 3) {
          setStatus('连接中断，正在重连…');
          reconnectTimer = window.setTimeout(connect, 1000 * 2 ** reconnectCount++);
        } else {
          setStatus('连接已断开');
          setError('无法连接实时渲染服务。可点击重新连接；已有静态结果仍可查看。');
        }
      };
      current.onerror = () => {
        if (!disposed) setStatus('实时连接发生错误…');
      };
    };
    resize();
    connect();
    return () => {
      disposed = true;
      connected = false;
      window.clearTimeout(reconnectTimer);
      window.clearTimeout(timeoutTimer);
      window.clearTimeout(settleTimer);
      observer.disconnect();
      window.removeEventListener('resize', fullResize);
      controls.removeEventListener('change', interaction);
      controls.dispose();
      socket?.close();
      scheduler.reset();
      painter.dispose();
      engine.current = null;
      if (displayedUrl) URL.revokeObjectURL(displayedUrl);
      decodeUrls.forEach((url) => URL.revokeObjectURL(url));
      if (image.current) image.current.removeAttribute('src');
    };
  }, [enabled, runId, scene?.revision, retry]);
  useEffect(() => {
    if (focusSelectionRequest > 0 && enabled) engine.current?.focusSelection();
  }, [focusSelectionRequest, enabled]);
  useEffect(() => {
    if (restoreSceneRequest > 0 && enabled) engine.current?.restoreScene();
  }, [restoreSceneRequest]);

  const [maxWidth, maxHeight] = sizes[resolution];
  return (
    <section
      className={`gaussian-live${simple ? ' gaussian-live-simple' : ''}`}
      data-testid="gaussian-live"
      aria-label="3DGS 自由视角渲染"
    >
      <div className="gaussian-live-heading">
        <div>
          <strong>{simple ? '三维场景' : '3DGS 自由视角'}</strong>
          {!simple && <small>真实高斯模型 · 工作台 GPU 渲染</small>}
        </div>
        <button
          data-testid="gaussian-live-toggle"
          disabled={disabled || !registered.length}
          className={enabled ? 'outline' : 'primary'}
          onClick={() => {
            if (simple && enabled) onPause?.();
            setEnabled(!enabled);
            setStatus(enabled ? '已关闭 · GPU 资源已释放' : '正在连接…');
            setMetrics(null);
          }}
        >
          {enabled ? <Square size={13} /> : <Play size={13} />}
          {enabled ? (simple ? '暂停浏览' : '关闭实时渲染') : simple ? '继续浏览' : '开启实时渲染'}
        </button>
        {simple && (
          <button
            aria-label="全屏查看场景"
            onClick={() => {
              if (document.fullscreenElement) void document.exitFullscreen();
              else
                void fullscreen.current
                  ?.requestFullscreen()
                  .catch(() => setError('无法打开全屏，请重试。'));
            }}
          >
            <Expand size={14} />
            全屏
          </button>
        )}
      </div>
      {!simple && (
        <p className="rendering-note">
          拖拽旋转 · 右键平移 · 滚轮缩放。
          {!presentation && '仅视角变化时请求新图像；视图操作不修改训练模型。'}
          {disabled && '请等待当前训练任务结束后开启。'}
        </p>
      )}
      {!simple && (
        <label className="live-adaptive-toggle">
          <input
            type="checkbox"
            checked={adaptive}
            onChange={(event) => {
              const value = event.target.checked;
              setAdaptive(value);
              try {
                localStorage.setItem('minicolmap.live-adaptive', value ? 'on' : 'off');
              } catch {
                /* Optional preference. */
              }
              engine.current?.refresh();
            }}
          />
          交互加速<span>拖动时轻量预览，停下后恢复清晰画面</span>
        </label>
      )}
      {enabled && (
        <>
          {!simple && (
            <div className="gaussian-live-toolbar">
              <label>
                输出上限
                <select
                  data-testid="gaussian-live-resolution"
                  aria-label="实时渲染分辨率"
                  value={resolution}
                  onChange={(event) => setResolution(event.target.value as keyof typeof sizes)}
                >
                  <option value="640">640 × 480</option>
                  <option value="960">960 × 720</option>
                  <option value="1280">1280 × 720</option>
                  <option value="1920">1920 × 1080 · 高清</option>
                </select>
              </label>
              <label>
                已有相机
                <select
                  data-testid="gaussian-live-camera"
                  aria-label="实时渲染对齐相机"
                  value={cameraId}
                  onChange={(event) => setCameraId(event.target.value)}
                >
                  {registered.map((source) => (
                    <option value={source.image_id} key={source.image_id}>
                      #{source.image_id} · {source.name}
                    </option>
                  ))}
                </select>
              </label>
              <button
                data-testid="gaussian-live-align"
                onClick={() => {
                  const source = registered.find((item) => String(item.image_id) === cameraId);
                  if (source) engine.current?.align(source);
                }}
              >
                <Focus size={13} />
                对齐相机
              </button>
              <button data-testid="gaussian-live-fit" onClick={() => engine.current?.fit()}>
                <Focus size={13} />
                适配场景
              </button>
              <button
                aria-label="实时渲染全屏"
                onClick={() => {
                  if (document.fullscreenElement) void document.exitFullscreen();
                  else
                    void fullscreen.current
                      ?.requestFullscreen()
                      .catch((failure) => setError(String(failure)));
                }}
              >
                <Expand size={13} />
                全屏
              </button>
              <button
                data-testid="gaussian-live-reconnect"
                onClick={() => {
                  setMetrics(null);
                  setRetry((value) => value + 1);
                }}
              >
                <RotateCcw size={13} />
                重新连接
              </button>
            </div>
          )}
          <div
            className="gaussian-live-stage"
            ref={fullscreen}
            style={{ '--live-aspect': `${maxWidth / maxHeight}` } as React.CSSProperties}
          >
            <div ref={viewport} className="gaussian-live-viewport">
              {boxOverlay.lines.length > 0 && (
                <svg
                  className="selection-bounds"
                  viewBox="0 0 1000 1000"
                  preserveAspectRatio="none"
                  aria-label={`${objectSelection?.name ?? '选区'}三维包围盒`}
                >
                  {boxOverlay.lines.map((line, i) => (
                    <line
                      key={i}
                      x1={line[0] * 1000}
                      y1={line[1] * 1000}
                      x2={line[2] * 1000}
                      y2={line[3] * 1000}
                      vectorEffect="non-scaling-stroke"
                    />
                  ))}
                  {boxOverlay.center && (
                    <circle
                      cx={boxOverlay.center[0] * 1000}
                      cy={boxOverlay.center[1] * 1000}
                      r="5"
                      vectorEffect="non-scaling-stroke"
                    >
                      <title>选区中心</title>
                    </circle>
                  )}
                </svg>
              )}
              <img
                ref={image}
                alt="实时高斯渲染输出"
                data-testid="gaussian-live-image"
                draggable={false}
              />
              {!metrics && (
                <div className="gaussian-live-placeholder">
                  {!error && <LoaderCircle size={25} className="spin" />}
                  <span>{error ? '渲染不可用' : status}</span>
                </div>
              )}
              <canvas
                ref={canvas}
                data-testid="gaussian-live-canvas"
                tabIndex={0}
                aria-label="可拖拽旋转、右键平移、滚轮缩放的实时高斯视图"
                onContextMenu={(event) => event.preventDefault()}
              />
              {!simple && (
                <span className="gaussian-live-badge">
                  3DGS ·{' '}
                  {metrics ? `${metrics.width} × ${metrics.height}` : `${maxWidth} × ${maxHeight}`}
                  {metrics?.quality === 'interactive' ? ' · 交互预览' : ''}
                </span>
              )}
            </div>
          </div>
          <div className="gaussian-live-status" data-testid="gaussian-live-status" role="status">
            {status}
            {!simple && presentation && metrics && (
              <span>
                响应 {Math.round(metrics.latency)} ms
                {metrics.quality === 'interactive' ? ' · 交互预览' : ' · 清晰画面'}
              </span>
            )}
            {!simple && ready && (
              <span title={`${ready.backend} / ${ready.device} · SH ${ready.sh_degree}`}>
                {presentation ? (
                  metrics?.selection?.mode === 'isolate' && objectSelection ? (
                    `${number(objectSelection.selected_gaussians, 0)} 个选中高斯 · 单独查看`
                  ) : (
                    `${number(ready.gaussian_count, 0)} 个高斯 · 三维模型已加载`
                  )
                ) : (
                  <>
                    {ready.backend} / {ready.device} · {number(ready.gaussian_count, 0)} 个高斯 · SH{' '}
                    {ready.sh_degree}
                  </>
                )}
              </span>
            )}
          </div>
          <div
            hidden={simple}
            className="gaussian-live-metrics"
            data-testid="gaussian-live-metrics"
            data-frame-count={metrics?.frames ?? 0}
            data-last-seq={metrics?.seq ?? ''}
            data-fps={metrics?.fps ?? ''}
            data-latency-ms={metrics?.latency ?? ''}
          >
            <span>
              相邻新帧 <b>{number(metrics?.fps, 1)}</b> FPS
            </span>
            <span>
              GPU 渲染 <b>{number(metrics?.render, 1)}</b> ms
            </span>
            <span>
              编码 <b>{number(metrics?.encode, 1)}</b> ms
            </span>
            <span>
              端到端 <b>{number(metrics?.latency, 1)}</b> ms
            </span>
            <span>
              已显示 <b>{metrics?.frames ?? 0}</b> 帧
            </span>
          </div>
          {!simple && (
            <small className="rendering-note">
              帧率只统计真实返回并显示的新图像；静止时不重复渲染。端到端包含请求、GPU
              计算、编码、传输与解码。
            </small>
          )}
          {error && (
            <p className="rendering-error" role="alert">
              {error}
            </p>
          )}
        </>
      )}
    </section>
  );
}
