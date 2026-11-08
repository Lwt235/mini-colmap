import { useCallback, useEffect, useState } from 'react';
import {
  Download,
  Focus,
  Highlighter,
  Layers3,
  LoaderCircle,
  Play,
  RotateCcw,
  Search,
} from 'lucide-react';
import { api, json } from './api';
import GaussianLiveViewer from './GaussianLiveViewer';
import type { Scene } from './types';
import type { ObjectSelection, SelectionMode } from './objectSelection';
import type { SceneJob, SearchScene } from './sceneTypes';

export default function SceneDemo({
  value,
  onRefresh,
}: {
  value: SearchScene;
  onRefresh: () => void;
}) {
  const [scene, setScene] = useState<Scene | null>(null);
  const [job, setJob] = useState<SceneJob | undefined>(value.job);
  const [selection, setSelection] = useState<ObjectSelection | undefined>(
    value.job?.result?.selection,
  );
  const [cameraId, setCameraId] = useState(value.job?.result?.image_id ?? value.anchor_image_id);
  const [mode, setMode] = useState<SelectionMode>('none');
  const [live, setLive] = useState(false);
  const [text, setText] = useState('');
  const [working, setWorking] = useState(false);
  const [start, setStart] = useState(0);
  const [focus, setFocus] = useState(0);
  const [restore, setRestore] = useState(0);
  const [error, setError] = useState('');
  const [ready, setReady] = useState(value.ready);
  const busy = working || job?.status === 'running';
  useEffect(() => {
    setReady(value.ready);
  }, [value.ready]);
  useEffect(() => {
    let active = true;
    void api<Scene>(`/api/runs/${value.rgb_run_id}/scene`)
      .then((s) => {
        if (active) setScene(s);
      })
      .catch((e) => setError(String(e)));
    return () => {
      active = false;
    };
  }, [value.rgb_run_id]);
  const complete = useCallback(
    (next: SceneJob) => {
      setJob(next);
      onRefresh();
      if (next.status === 'completed' && next.action === 'train') setReady(true);
      if (next.result) {
        setCameraId(next.result.image_id);
        setStart((v) => v + 1);
        setLive(true);
      }
      if (next.result?.selection) {
        setSelection(next.result.selection);
        setMode('highlight');
        setFocus((v) => v + 1);
        setStart((v) => v + 1);
        setLive(true);
      }
    },
    [onRefresh],
  );
  useEffect(() => {
    if (job?.status !== 'running') return;
    const timer = setInterval(() => {
      void api<SceneJob>(`/api/scene-jobs/${job.id}`)
        .then((next) => {
          if (next.status === 'running') setJob(next);
          else complete(next);
        })
        .catch((e) => setError(String(e)));
    }, 1500);
    return () => clearInterval(timer);
  }, [job?.id, job?.status, complete]);
  async function process(action: 'train' | 'query') {
    setWorking(true);
    setError('');
    setLive(false);
    setSelection(undefined);
    setMode('none');
    setJob(undefined);
    try {
      let idle = false;
      for (let i = 0; i < 40; i++) {
        await new Promise((r) => setTimeout(r, 250));
        const status = await api<{ gpu_busy: boolean }>('/api/demo');
        if (!status.gpu_busy) {
          idle = true;
          break;
        }
      }
      if (!idle) throw Error('场景正忙，请稍后再试。');
      const next = await api<SceneJob>(
        `/api/scenes/${value.id}/${action}`,
        json(action === 'query' ? { text } : {}),
      );
      if (next.status === 'running') setJob(next);
      else complete(next);
    } catch (e) {
      setError(String(e));
    } finally {
      setWorking(false);
    }
  }
  function browse() {
    setLive(true);
    setStart((v) => v + 1);
  }
  const sceneReady = !!scene && !busy;
  return (
    <main className="simple-demo">
      <div className="simple-intro">
        <h1>在自己的场景中查找物体</h1>
        <p>输入英文物体名称，在三维模型中查看候选区域。</p>
      </div>
      {!ready ? (
        <div className="scene-index-prompt">
          <span>此场景尚未生成语义索引。</span>
          <button className="simple-primary" disabled={busy} onClick={() => void process('train')}>
            {busy ? <LoaderCircle className="spin" size={16} /> : <Search size={16} />}生成语义索引
          </button>
          <small>首次需要提取照片特征并学习到三维模型，完成后可重复检索。</small>
        </div>
      ) : (
        <form
          className="simple-search-row scene-search"
          onSubmit={(e) => {
            e.preventDefault();
            void process('query');
          }}
        >
          <label className="simple-search-input">
            <Search size={20} />
            <span className="sr-only">物体描述</span>
            <input
              value={text}
              maxLength={160}
              disabled={busy}
              placeholder="英文物体名，如 chair"
              onChange={(e) => setText(e.target.value)}
            />
          </label>
          <button className="simple-primary" disabled={busy || !text.trim()}>
            {busy ? <LoaderCircle className="spin" size={17} /> : <Search size={17} />}查找物体
          </button>
        </form>
      )}
      {job && (
        <div className="scene-build-progress" role="status">
          <span>
            {job.message}
            {job.progress?.total ? ` · ${job.progress.step} / ${job.progress.total}` : ''}
          </span>
          {job.status === 'running' && (
            <button
              onClick={() => {
                void api<SceneJob>(`/api/scene-jobs/${job.id}/cancel`, { method: 'POST' })
                  .then(complete)
                  .catch((e) => setError(String(e)));
              }}
            >
              停止任务
            </button>
          )}
        </div>
      )}
      <section className="simple-scene" aria-label="三维场景">
        <div className="simple-scene-bar">
          <strong>{value.name}</strong>
          <span>拖动旋转 · 右键平移 · 滚轮缩放</span>
        </div>
        {live ? (
          <GaussianLiveViewer
            simple
            presentation
            runId={value.rgb_run_id}
            scene={scene}
            selectedCameraId={selection?.anchor_image_id ?? cameraId}
            disabled={busy}
            onPause={() => setLive(false)}
            objectSelection={selection}
            selectionMode={mode}
            focusSelectionRequest={focus}
            autoStartRequest={start}
            restoreSceneRequest={restore}
          />
        ) : (
          <div className="simple-scene-preview">
            <img src={value.preview_url} alt="当前场景照片预览" />
            <div>
              <button className="simple-primary" disabled={!sceneReady} onClick={browse}>
                <Play size={17} />
                {busy ? '正在处理场景…' : !scene ? '正在连接场景…' : '开始浏览'}
              </button>
            </div>
          </div>
        )}
        <div className="simple-result-bar">
          <strong>{selection ? `${selection.text} · 候选区域` : '整个场景'}</strong>
          <div className="simple-result-actions">
            <button
              disabled={!selection || busy}
              aria-pressed={mode === 'highlight'}
              onClick={() => {
                setMode('highlight');
                browse();
              }}
            >
              <Highlighter size={16} />
              高亮
            </button>
            <button
              disabled={!selection || busy}
              aria-pressed={mode === 'isolate'}
              onClick={() => {
                setMode('isolate');
                browse();
              }}
            >
              <Layers3 size={16} />
              单独查看
            </button>
            <button
              disabled={!selection || busy}
              onClick={() => {
                setMode('highlight');
                setFocus((v) => v + 1);
                browse();
              }}
            >
              <Focus size={16} />
              回到物体
            </button>
            <button
              disabled={!sceneReady}
              onClick={() => {
                setMode('none');
                setRestore((v) => v + 1);
                browse();
              }}
            >
              <RotateCcw size={16} />
              恢复场景
            </button>
            {selection && (
              <a href={selection.export_url} download>
                <Download size={16} />
                导出候选
              </a>
            )}
          </div>
        </div>
      </section>
      {job?.result && (
        <div className="scene-query-preview">
          <img src={job.result.overlay_url} alt={`${job.result.text} 的三维语义检索响应`} />
          <span>候选区域，请确认是否为目标物体。</span>
        </div>
      )}
      {error && (
        <p role="alert" className="simple-error">
          {error}
        </p>
      )}
    </main>
  );
}
