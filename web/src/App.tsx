import { lazy, Suspense, useCallback, useEffect, useState } from 'react';
import { Box, SlidersHorizontal } from 'lucide-react';
import DemoPanel from './DemoPanel';
import GpuSelector from './GpuSelector';
import { api } from './api';
import type { SearchScene } from './sceneTypes';
const Workbench = lazy(() => import('./Workbench'));
const SceneDemo = lazy(() => import('./SceneDemo'));
export default function App() {
  const [scenes, setScenes] = useState<SearchScene[]>([]);
  const [sceneId, setSceneId] = useState('kitchen');
  const [building, setBuilding] = useState(window.location.hash === '#workspace');
  useEffect(() => {
    const change = () => setBuilding(window.location.hash === '#workspace');
    window.addEventListener('hashchange', change);
    return () => window.removeEventListener('hashchange', change);
  }, []);
  const refresh = useCallback(() => {
    void api<{ scenes: SearchScene[] }>('/api/scenes')
      .then((v) => setScenes(v.scenes))
      .catch(() => {});
  }, []);
  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, [refresh]);
  const selected = scenes.find((s) => s.id === sceneId);
  function open(scene: SearchScene) {
    setScenes((previous) => [scene, ...previous.filter((s) => s.id !== scene.id)]);
    setSceneId(scene.id);
    setBuilding(false);
    window.location.hash = '#demo';
    refresh();
  }
  return (
    <div className={`product-shell product-simple${building ? ' product-workspace' : ''}`}>
      <header className="simple-header">
        <a
          className="product-brand"
          href="#demo"
          aria-label="Mini COLMAP 首页"
          onClick={() => {
            setBuilding(false);
            window.location.hash = '#demo';
          }}
        >
          <Box size={23} />
          <span>MINI COLMAP</span>
        </a>
        <div className="scene-switch">
          <GpuSelector />
          <label>
            <span className="sr-only">当前场景</span>
            <select
              aria-label="当前场景"
              value={building ? 'new' : sceneId}
              onChange={(e) => {
                if (e.target.value === 'new') {
                  setBuilding(true);
                  window.location.hash = '#workspace';
                } else {
                  setSceneId(e.target.value);
                  setBuilding(false);
                  window.location.hash = '#demo';
                }
              }}
            >
              <option value="kitchen">Waldo Kitchen · 示例</option>
              {scenes.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
              {building && <option value="new">重建与训练</option>}
            </select>
          </label>
          <button
            aria-pressed={building}
            onClick={() => {
              setBuilding(true);
              window.location.hash = '#workspace';
            }}
          >
            <SlidersHorizontal size={16} />
            重建与训练
          </button>
        </div>
      </header>
      <Suspense fallback={<main className="simple-demo">正在加载场景…</main>}>
        {building ? (
          <Workbench onOpenScene={open} />
        ) : selected ? (
          <SceneDemo key={selected.id} value={selected} onRefresh={refresh} />
        ) : (
          <DemoPanel />
        )}
      </Suspense>
    </div>
  );
}
