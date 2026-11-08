import { useEffect, useRef, useState } from 'react';
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import {
  Axis3D,
  Camera as CameraIcon,
  Expand,
  Focus,
  Scan,
  ImageDown,
  Layers3,
  MousePointer2,
  RotateCcw,
  SlidersHorizontal,
} from 'lucide-react';
import type { Camera, DisplayOptions, Scene, Selection } from './types';
import { cameraCenter, frustumCorners, visiblePointIndices } from './math';
import { alignedView, CameraTransition } from './cameraView';
import { captureRenderCamera } from './rendering';
import type { RenderCamera } from './rendering';
import { number, snapshotName } from './api';

type Props = {
  scene: Scene | null;
  runId: string;
  options: DisplayOptions;
  setOptions: (value: DisplayOptions) => void;
  selection: Selection;
  onSelect: (s: Selection) => void;
  cameraViewRequest: { imageId: number; version: number } | null;
  onCameraViewChange: (imageId: number | null) => void;
  onRenderCamera?: (camera: RenderCamera) => void;
  renderBusy?: boolean;
  renderMaxEdge?: number;
};
type Engine = {
  renderer: THREE.WebGLRenderer;
  scene: THREE.Scene;
  camera: THREE.PerspectiveCamera;
  controls: OrbitControls;
  content: THREE.Group;
  highlight: THREE.Group;
  bounds: THREE.Box3;
  radius: number;
  fit: (bounds?: THREE.Box3) => void;
  pickables: THREE.Object3D[];
  pointIndices: number[];
  enterCamera: (source: Camera) => void;
  cancelAlignment: () => void;
  invalidate: () => void;
};
function dispose(group: THREE.Group) {
  for (const child of [...group.children]) {
    group.remove(child);
    child.traverse((o) => {
      if (o instanceof THREE.Mesh || o instanceof THREE.Points || o instanceof THREE.Line) {
        o.geometry.dispose();
        const materials = Array.isArray(o.material) ? o.material : [o.material];
        materials.forEach((m) => m.dispose());
      }
    });
  }
}

export default function Viewer({
  scene,
  runId,
  options,
  setOptions,
  selection,
  onSelect,
  cameraViewRequest,
  onCameraViewChange,
  onRenderCamera,
  renderBusy,
  renderMaxEdge = 256,
}: Props) {
  const host = useRef<HTMLDivElement>(null),
    engine = useRef<Engine | null>(null),
    latestSelection = useRef(onSelect),
    latestCameraViewChange = useRef(onCameraViewChange),
    previousRun = useRef('');
  const [error, setError] = useState(''),
    [settings, setSettings] = useState(false),
    [visibleCount, setVisibleCount] = useState(0),
    [hover, setHover] = useState('');
  latestSelection.current = onSelect;
  latestCameraViewChange.current = onCameraViewChange;
  useEffect(() => {
    if (!host.current) return;
    const parent = host.current;
    let frame = 0,
      disposed = false;
    let transition: CameraTransition | null = null;
    const invalidate = () => {
      if (!disposed && !document.hidden && !frame) frame = requestAnimationFrame(draw);
    };
    let renderer: THREE.WebGLRenderer;
    try {
      renderer = new THREE.WebGLRenderer({
        antialias: true,
        alpha: true,
        preserveDrawingBuffer: true,
      });
    } catch (e) {
      setError(`无法创建 WebGL 视图：${String(e)}`);
      return;
    }
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.setClearColor(0x11171b, 0);
    renderer.domElement.setAttribute('aria-label', '可拖拽的三维重建视图');
    renderer.domElement.setAttribute('data-testid', 'scene-canvas');
    parent.appendChild(renderer.domElement);
    const world = new THREE.Scene(),
      camera = new THREE.PerspectiveCamera(45, 1, 0.001, 1e6);
    camera.up.set(0, -1, 0);
    camera.position.set(3, -2, -4);
    const createControls = () => {
      const value = new OrbitControls(camera, renderer.domElement);
      value.enableDamping = true;
      value.dampingFactor = 0.12;
      value.screenSpacePanning = true;
      value.minDistance = 0.0001;
      value.maxDistance = 1e8;
      value.addEventListener('change', invalidate);
      return value;
    };
    let controls = createControls();
    const content = new THREE.Group(),
      highlight = new THREE.Group();
    world.add(content, highlight);
    const orbitFrom = (target: THREE.Vector3) => {
      // OrbitControls caches the camera's up axis at construction. Recreate it
      // after alignment so the first drag preserves this camera's roll.
      const position = camera.position.clone();
      controls.dispose();
      controls = createControls();
      camera.position.copy(position);
      controls.target.copy(target);
      controls.update();
      state.controls = controls;
      invalidate();
    };
    const cancelAlignment = () => {
      if (!transition) return;
      transition = null;
      renderer.domElement.removeAttribute('data-camera-aligning');
      orbitFrom(controls.target.clone());
    };
    // Capture on the parent before OrbitControls receives the same input. The
    // replacement controls can process the first drag/wheel event immediately.
    parent.addEventListener('pointerdown', cancelAlignment, true);
    parent.addEventListener('wheel', cancelAlignment, { capture: true, passive: true });
    const layout = () => {
      const width = parent.clientWidth,
        height = parent.clientHeight;
      camera.aspect = width / Math.max(height, 1);
      camera.updateProjectionMatrix();
    };
    const state: Engine = {
      renderer,
      scene: world,
      camera,
      controls,
      content,
      highlight,
      bounds: new THREE.Box3(new THREE.Vector3(-1, -1, -1), new THREE.Vector3(1, 1, 1)),
      radius: 1,
      pickables: [],
      pointIndices: [],
      invalidate,
      cancelAlignment,
      enterCamera: (source) => {
        const aligned = alignedView(
          source,
          parent.clientWidth / Math.max(parent.clientHeight, 1),
          Math.max(state.radius * 1e-6, 1e-8),
          Math.max(state.radius * 10000, 1000),
        );
        if (!aligned) return;
        const forward = aligned.getWorldDirection(new THREE.Vector3());
        const depth = Math.max(
          state.bounds.getCenter(new THREE.Vector3()).sub(aligned.position).dot(forward),
          state.radius * 0.01,
        );
        transition = new CameraTransition(
          camera,
          aligned,
          camera.position.distanceTo(controls.target),
          depth,
          performance.now(),
        );
        controls.enabled = false;
        renderer.domElement.setAttribute('data-camera-aligning', 'true');
        invalidate();
        latestCameraViewChange.current(source.image_id);
      },
      fit: (requestedBounds) => {
        cancelAlignment();
        const bounds =
          requestedBounds && !requestedBounds.isEmpty() ? requestedBounds : state.bounds;
        const center = bounds.getCenter(new THREE.Vector3()),
          r = Math.max(bounds.getSize(new THREE.Vector3()).length() / 2, 0.001);
        const direction = new THREE.Vector3(-0.5, -0.35, -1).normalize();
        camera.up.set(0, -1, 0);
        camera.fov = 45;
        camera.zoom = 1;
        camera.position.copy(center).addScaledVector(direction, r * 3.1);
        camera.near = Math.max(r / 10000, 1e-6);
        camera.far = Math.max(r * 10000, 1000);
        camera.updateProjectionMatrix();
        orbitFrom(center);
        latestCameraViewChange.current(null);
      },
    };
    engine.current = state;
    const ray = new THREE.Raycaster(),
      pointer = new THREE.Vector2();
    let down = [0, 0];
    const intersect = (event: PointerEvent) => {
      const b = renderer.domElement.getBoundingClientRect();
      const viewport = { x: 0, y: 0, width: b.width, height: b.height };
      const x = event.clientX - b.left - viewport.x,
        y = event.clientY - b.top - viewport.y;
      if (x < 0 || y < 0 || x > viewport.width || y > viewport.height) return [];
      pointer.set((x / viewport.width) * 2 - 1, -(y / viewport.height) * 2 + 1);
      ray.setFromCamera(pointer, camera);
      ray.params.Points!.threshold = Math.max(
        (camera.position.distanceTo(controls.target) *
          (1 / camera.projectionMatrix.elements[5]) *
          12) /
          Math.max(viewport.height, 1),
        1e-8,
      );
      return ray.intersectObjects(state.pickables, false).filter((hit) => hit.object.visible);
    };
    const pointerDown = (e: PointerEvent) => {
      down = [e.clientX, e.clientY];
    };
    const pointerUp = (e: PointerEvent) => {
      if (e.button !== 0 || Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 5) return;
      const hits = intersect(e),
        cameraHit = hits.find((h) => h.object.userData.kind === 'camera'),
        hit = cameraHit ?? hits[0];
      if (!hit) {
        latestSelection.current(null);
        return;
      }
      if (hit.object.userData.kind === 'camera')
        latestSelection.current({ kind: 'camera', id: hit.object.userData.id });
      else if (hit.index != null)
        latestSelection.current({ kind: 'point', id: hit.object.userData.ids[hit.index] });
    };
    const move = (e: PointerEvent) => {
      if (e.buttons) return;
      const hit = intersect(e)[0];
      setHover(
        hit
          ? hit.object.userData.kind === 'camera'
            ? `相机 #${hit.object.userData.id}`
            : `三维点 #${hit.object.userData.ids[hit.index!]}`
          : '',
      );
      renderer.domElement.style.cursor = hit ? 'pointer' : 'grab';
    };
    renderer.domElement.addEventListener('pointerdown', pointerDown);
    renderer.domElement.addEventListener('pointerup', pointerUp);
    renderer.domElement.addEventListener('pointermove', move);
    const resize = new ResizeObserver(() => {
      const w = parent.clientWidth,
        h = parent.clientHeight;
      if (!w || !h) return;
      renderer.setSize(w, h);
      layout();
      invalidate();
    });
    resize.observe(parent);
    function draw(now: number) {
      frame = 0;
      if (disposed || document.hidden) return;
      if (transition) {
        const finished = transition.update(camera, now);
        controls.target.copy(transition.target);
        if (finished) cancelAlignment();
        else invalidate();
      } else {
        // Damping schedules frames only while moving; idle scenes stay idle.
        // During alignment, update() would overwrite the interpolated rotation.
        controls.update();
      }
      // Avoid drawing/picking the camera marker around the observer's own lens.
      for (const child of content.children) {
        if (child.userData.cameraCenter)
          child.visible =
            camera.position.distanceToSquared(child.userData.cameraCenter) >
            (state.radius * 0.02) ** 2;
      }
      renderer.setViewport(0, 0, parent.clientWidth, parent.clientHeight);
      renderer.render(world, camera);
    }
    document.addEventListener('visibilitychange', invalidate);
    invalidate();
    return () => {
      disposed = true;
      cancelAnimationFrame(frame);
      document.removeEventListener('visibilitychange', invalidate);
      parent.removeEventListener('pointerdown', cancelAlignment, true);
      parent.removeEventListener('wheel', cancelAlignment, true);
      resize.disconnect();
      controls.dispose();
      renderer.domElement.removeEventListener('pointerdown', pointerDown);
      renderer.domElement.removeEventListener('pointerup', pointerUp);
      renderer.domElement.removeEventListener('pointermove', move);
      dispose(content);
      dispose(highlight);
      renderer.dispose();
      parent.removeChild(renderer.domElement);
      engine.current = null;
    };
  }, []);
  useEffect(() => {
    const e = engine.current;
    if (!e) return;
    dispose(e.content);
    e.pickables = [];
    if (!scene) {
      e.cancelAlignment();
      latestCameraViewChange.current(null);
      setVisibleCount(0);
      e.invalidate();
      return;
    }
    const p = scene.points,
      bounds = new THREE.Box3();
    for (let i = 0; i < p.positions.length; i += 3)
      bounds.expandByPoint(
        new THREE.Vector3(p.positions[i], p.positions[i + 1], p.positions[i + 2]),
      );
    for (const c of scene.cameras) {
      if (c.registered && c.R && c.t)
        bounds.expandByPoint(
          new THREE.Vector3(...(cameraCenter(c.R, c.t) as [number, number, number])),
        );
    }
    if (bounds.isEmpty()) bounds.set(new THREE.Vector3(-1, -1, -1), new THREE.Vector3(1, 1, 1));
    e.bounds.copy(bounds);
    e.radius = Math.max(bounds.getSize(new THREE.Vector3()).length() / 2, 0.001);
    const r = e.radius;
    const indices = visiblePointIndices(
      p.errors,
      p.track_lengths,
      p.ids.length,
      options.maxError,
      options.minTrack,
    );
    e.pointIndices = indices;
    setVisibleCount(options.points ? indices.length : 0);
    if (options.points && indices.length) {
      const positions = new Float32Array(indices.length * 3),
        colors = new Float32Array(indices.length * 3),
        color = new THREE.Color();
      const errorLimit = Math.max(scene.summary.p95_reprojection_error_px ?? 1, 0.01),
        maxTrack = p.track_lengths.reduce((max, value) => Math.max(max, value), 2);
      indices.forEach((idx, j) => {
        positions.set(p.positions.slice(idx * 3, idx * 3 + 3), j * 3);
        if (options.color === 'rgb')
          color.setRGB(
            (p.colors[idx * 3] ?? 180) / 255,
            (p.colors[idx * 3 + 1] ?? 180) / 255,
            (p.colors[idx * 3 + 2] ?? 180) / 255,
            THREE.SRGBColorSpace,
          );
        else {
          const ratio =
            options.color === 'error'
              ? Math.min((p.errors[idx] ?? errorLimit) / errorLimit, 1)
              : Math.min(Math.log(p.track_lengths[idx] ?? 1) / Math.log(maxTrack), 1);
          color.setHSL(
            options.color === 'error' ? (1 - ratio) * 0.48 : 0.55 - ratio * 0.4,
            0.84,
            0.55,
            THREE.SRGBColorSpace,
          );
        }
        colors.set([color.r, color.g, color.b], j * 3);
      });
      const geo = new THREE.BufferGeometry();
      geo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
      geo.setAttribute('color', new THREE.BufferAttribute(colors, 3));
      const points = new THREE.Points(
        geo,
        new THREE.PointsMaterial({
          size: options.pointSize,
          vertexColors: true,
          sizeAttenuation: false,
          transparent: true,
          opacity: 0.96,
        }),
      );
      points.userData = { kind: 'points', ids: indices.map((i) => p.ids[i]) };
      e.content.add(points);
      e.pickables.push(points);
    }
    if (options.cameras)
      for (const c of scene.cameras) {
        if (!c.registered || !c.R || !c.t) continue;
        const center = cameraCenter(c.R, c.t),
          corners = frustumCorners(c, r * 0.09 * options.cameraScale),
          positions: number[] = [];
        corners.forEach((v, i) => positions.push(...center, ...v, ...v, ...corners[(i + 1) % 4]));
        const geo = new THREE.BufferGeometry();
        geo.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
        const selected = selection?.kind === 'camera' && selection.id === c.image_id;
        const line = new THREE.LineSegments(
          geo,
          new THREE.LineBasicMaterial({
            color: selected ? 0xffbf69 : 0x56bbcd,
            transparent: true,
            opacity: selected ? 1 : 0.6,
          }),
        );
        line.userData.cameraCenter = new THREE.Vector3().fromArray(center);
        e.content.add(line);
        const sphere = new THREE.Mesh(
          new THREE.SphereGeometry(r * 0.0075, 10, 8),
          new THREE.MeshBasicMaterial({ color: selected ? 0xffbf69 : 0x7be0df }),
        );
        sphere.position.fromArray(center);
        sphere.userData = {
          kind: 'camera',
          id: c.image_id,
          cameraCenter: new THREE.Vector3().fromArray(center),
        };
        e.content.add(sphere);
        e.pickables.push(sphere);
      }
    if (options.axes) {
      const axes = new THREE.AxesHelper(r * 0.4);
      e.content.add(axes);
    }
    if (previousRun.current !== runId) {
      e.fit();
      previousRun.current = runId;
    }
    e.invalidate();
  }, [scene, runId, options, selection]);
  useEffect(() => {
    const source = scene?.cameras.find((c) => c.image_id === cameraViewRequest?.imageId);
    if (source) engine.current?.enterCamera(source);
    // Alignment happens only on a deliberate click; snapshots and selection never snap back.
  }, [cameraViewRequest]);
  useEffect(() => {
    const e = engine.current;
    if (!e) return;
    dispose(e.highlight);
    e.invalidate();
    if (!scene || selection?.kind !== 'point') return;
    const index = scene.points.ids.indexOf(selection.id);
    if (index < 0) return;
    const point = new THREE.Mesh(
      new THREE.SphereGeometry(e.radius * 0.009, 12, 10),
      new THREE.MeshBasicMaterial({ color: 0xffc778, depthTest: false }),
    );
    point.position.fromArray(scene.points.positions, index * 3);
    point.renderOrder = 10;
    e.highlight.add(point);
  }, [selection, scene]);
  const change = <K extends keyof DisplayOptions>(key: K, value: DisplayOptions[K]) =>
    setOptions({ ...options, [key]: value });
  const snapshot = scene?.snapshots.find((s) => s.id === scene.snapshot_id);
  return (
    <div className="viewport">
      <div className="view-heading">
        <span className="view-title">
          <Layers3 size={14} /> 三维重建
        </span>
        <span className="view-context">
          {snapshot ? snapshotName(snapshot.label) : '模型空间'} <span className="live-dot" />{' '}
          真实算法输出
        </span>
      </div>
      <div ref={host} className="canvas-host" />
      {(!scene ||
        (scene.points.ids.length === 0 && !scene.cameras.some((camera) => camera.registered))) && (
        <div className="empty-scene">
          <div className="empty-orbit">
            <Layers3 size={32} />
          </div>
          <h3>{scene ? '等待三维点生成' : '从照片，进入三维空间'}</h3>
          <p>
            {scene
              ? '初始化成功后，点云和相机将出现在这里。'
              : '选择一个已有结果，或导入数据开始重建。'}
          </p>
          <span>只呈现真实结果 · 不生成示意点云</span>
        </div>
      )}
      {error && <div className="view-error">{error}</div>}
      <div className="view-toolbar">
        {onRenderCamera && (
          <button
            title={`以训练模型渲染当前自由视角（长边 ${renderMaxEdge} px）`}
            aria-label="渲染当前自由视角"
            disabled={renderBusy || !scene}
            onClick={() => {
              if (engine.current)
                onRenderCamera(captureRenderCamera(engine.current.camera, renderMaxEdge));
            }}
          >
            <CameraIcon size={17} />
            渲染当前视角
          </button>
        )}
        <button
          title="适配完整场景"
          aria-label="适配完整场景"
          onClick={() => engine.current?.fit()}
        >
          <Focus size={17} />
        </button>
        <button
          title="聚焦点云（仅调整观察视角）"
          aria-label="聚焦点云"
          disabled={!scene?.points.ids.length}
          onClick={() => {
            if (!scene) return;
            const bounds = new THREE.Box3();
            const positions = scene.points.positions;
            for (let i = 0; i < positions.length; i += 3) {
              bounds.expandByPoint(
                new THREE.Vector3(positions[i], positions[i + 1], positions[i + 2]),
              );
            }
            engine.current?.fit(bounds);
          }}
        >
          <Scan size={17} />
        </button>
        <button title="重置视角" aria-label="重置视角" onClick={() => engine.current?.fit()}>
          <RotateCcw size={16} />
        </button>
        <span className="toolbar-divider" />
        <button
          className={options.cameras ? 'active' : ''}
          title="相机显隐"
          aria-label="相机显隐"
          aria-pressed={options.cameras}
          onClick={() => change('cameras', !options.cameras)}
        >
          <CameraIcon size={17} />
        </button>
        <button
          className={options.axes ? 'active' : ''}
          title="坐标轴显隐"
          aria-label="坐标轴显隐"
          aria-pressed={options.axes}
          onClick={() => change('axes', !options.axes)}
        >
          <Axis3D size={17} />
        </button>
        <button
          className={settings ? 'active' : ''}
          title="显示设置"
          aria-label="显示设置"
          onClick={() => setSettings(!settings)}
        >
          <SlidersHorizontal size={17} />
        </button>
        <span className="toolbar-divider" />
        <button
          title="保存视图 PNG"
          aria-label="保存视图 PNG"
          disabled={!scene}
          onClick={() => {
            const e = engine.current;
            if (!e) return;
            e.renderer.render(e.scene, e.camera);
            const a = document.createElement('a');
            a.href = e.renderer.domElement.toDataURL('image/png');
            a.download = 'minicolmap-view.png';
            a.click();
          }}
        >
          <ImageDown size={17} />
        </button>
        <button
          title="全屏显示"
          aria-label="全屏显示"
          onClick={() => {
            const viewport = host.current?.parentElement;
            if (document.fullscreenElement) void document.exitFullscreen();
            else if (viewport) void viewport.requestFullscreen();
          }}
        >
          <Expand size={16} />
        </button>
      </div>
      {settings && (
        <div className="view-settings panel-popover">
          <div className="popover-title">
            显示设置 <span>仅影响当前视图</span>
          </div>
          <label className="switch-row">
            显示点云
            <input
              type="checkbox"
              checked={options.points}
              onChange={(e) => change('points', e.target.checked)}
            />
          </label>
          <label>
            点云着色
            <select
              aria-label="点云着色"
              value={options.color}
              onChange={(e) => change('color', e.target.value as DisplayOptions['color'])}
            >
              <option value="rgb">原始 RGB</option>
              <option value="error">重投影误差</option>
              <option value="track">轨迹长度</option>
            </select>
          </label>
          <label>
            点大小 <span>{options.pointSize} px</span>
            <input
              type="range"
              min="1"
              max="9"
              step=".5"
              value={options.pointSize}
              onChange={(e) => change('pointSize', +e.target.value)}
            />
          </label>
          <label>
            相机视锥 <span>{options.cameraScale.toFixed(1)} ×</span>
            <input
              type="range"
              min=".2"
              max="3"
              step=".1"
              value={options.cameraScale}
              onChange={(e) => change('cameraScale', +e.target.value)}
            />
          </label>
          <div className="two-inputs">
            <label>
              最大误差 / px
              <input
                type="number"
                min="0"
                step=".1"
                value={options.maxError}
                onChange={(e) => change('maxError', +e.target.value)}
              />
              <small>0 = 不筛选</small>
            </label>
            <label>
              最短轨迹
              <input
                type="number"
                min="0"
                step="1"
                value={options.minTrack}
                onChange={(e) => change('minTrack', +e.target.value)}
              />
              <small>观测图像数量</small>
            </label>
          </div>
        </div>
      )}
      {scene && (
        <div className="scene-stats">
          <div>
            <strong>{number(visibleCount, 0)}</strong>
            <span>显示点 / {number(scene.points.ids.length, 0)}</span>
          </div>
          <div>
            <strong>
              {scene.summary.registered_images}
              <i> / {scene.summary.image_count}</i>
            </strong>
            <span>已注册相机</span>
          </div>
          <div>
            <strong>
              {number(scene.summary.mean_reprojection_error_px)}
              <i> px</i>
            </strong>
            <span>平均重投影误差</span>
          </div>
        </div>
      )}
      {scene && options.color !== 'rgb' && (
        <div className={`color-legend ${options.color}`}>
          <span>{options.color === 'error' ? '重投影误差 / px' : '轨迹长度 / 张'}</span>
          <i />
          <div>
            <b>{options.color === 'error' ? '0' : '1'}</b>
            <b>
              {options.color === 'error'
                ? number(scene.summary.p95_reprojection_error_px)
                : scene.points.track_lengths.reduce((m, v) => Math.max(m, v), 0)}
              {options.color === 'error' ? '+' : ''}
            </b>
          </div>
        </div>
      )}
      <div className="view-footer">
        <span>
          <MousePointer2 size={12} /> {hover || '左键旋转 · 右键平移 · 滚轮缩放'}
        </span>
        <span>
          坐标单位：源场景尺度 <b className="axis-x">X</b>
          <b className="axis-y">Y</b>
          <b className="axis-z">Z</b>
        </span>
      </div>
    </div>
  );
}
