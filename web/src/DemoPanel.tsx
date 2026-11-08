import { useCallback, useEffect, useRef, useState } from 'react';
import { Download, Focus, Highlighter, Layers3, Play, RotateCcw } from 'lucide-react';
import { api } from './api';
import GaussianLiveViewer from './GaussianLiveViewer';
import AutoLocate, { type AutoLocateJob } from './AutoLocate';
import type { ObjectSelection, SelectionMode } from './objectSelection';
import type { Scene } from './types';

type Catalogue = {
  available: boolean;
  offline_gpu_busy: boolean;
  gpu_busy: boolean;
  rgb_run_id?: string;
  selections: ObjectSelection[];
  auto_locate_jobs: AutoLocateJob[];
  models: { id: string; ready: boolean }[];
};
type Examples = { selections: { id: string; title: string; selection_id: string }[] };

export default function DemoPanel() {
  const [data, setData] = useState<Catalogue | null>(null);
  const [examples, setExamples] = useState<Examples['selections']>([]);
  const [scene, setScene] = useState<Scene | null>(null);
  const [text, setText] = useState('');
  const [selection, setSelection] = useState<ObjectSelection>();
  const [mode, setMode] = useState<SelectionMode>('none');
  const [live, setLive] = useState(false);
  const [searching, setSearching] = useState(false);
  const [focus, setFocus] = useState(0);
  const [start, setStart] = useState(0);
  const [restore, setRestore] = useState(0);
  const [error, setError] = useState('');
  const resume = useRef(false);
  const refresh = useCallback(() => {
    void api<Catalogue>('/api/demo')
      .then(setData)
      .catch(() => setError('连接中断，请刷新页面重试。'));
  }, []);
  useEffect(() => {
    refresh();
    void api<Examples>('/api/showcase')
      .then((value) => setExamples(value.selections))
      .catch(() => {});
    const timer = window.setInterval(refresh, 5000);
    return () => clearInterval(timer);
  }, [refresh]);
  useEffect(() => {
    if (!data?.rgb_run_id) return;
    let active = true;
    void api<Scene>(`/api/runs/${data.rgb_run_id}/scene`)
      .then((value) => {
        if (active) setScene(value);
      })
      .catch(() => {
        if (active) setError('三维场景加载失败，请刷新页面重试。');
      });
    return () => {
      active = false;
    };
  }, [data?.rgb_run_id]);
  const locate = useCallback((value: ObjectSelection) => {
    setSelection(value);
    setMode('highlight');
    setLive(true);
    setStart((v) => v + 1);
    setFocus((v) => v + 1);
    setError('');
  }, []);
  async function prepareSearch() {
    resume.current = live;
    setLive(false);
    setSelection(undefined);
    setMode('none');
    setError('');
    // Unmounting closes the WebSocket. Wait for its worker to release the lease.
    for (let n = 0; n < 40; n++) {
      await new Promise((resolve) => setTimeout(resolve, 250));
      const next = await api<Catalogue>('/api/demo');
      setData(next);
      if (!next.gpu_busy) return;
      if (next.offline_gpu_busy) throw new Error('场景正在处理其他任务，请稍后再试。');
    }
    throw new Error('场景尚未空闲，请稍后重试。');
  }
  const finishSearch = useCallback(() => {
    if (resume.current) {
      setLive(true);
      setStart((v) => v + 1);
    }
    resume.current = false;
  }, []);
  const disabled = searching || !!data?.offline_gpu_busy;
  const canBrowse = !!scene && !!data?.rgb_run_id && !disabled;
  function browse() {
    setLive(true);
    setStart((v) => v + 1);
  }
  function display(next: SelectionMode) {
    setMode(next);
    browse();
  }
  return (
    <main className="simple-demo">
      <div className="simple-intro">
        <h1>用文字找到三维场景中的物体</h1>
        <p>在照片重建的厨房中查找物体，旋转查看，或单独显示它。</p>
      </div>
      <AutoLocate
        model="raw3"
        text={text}
        onTextChange={setText}
        jobs={data?.auto_locate_jobs ?? []}
        selections={data?.selections ?? []}
        busy={!!data?.offline_gpu_busy}
        pending={!data?.models.some((m) => m.id === 'raw3' && m.ready)}
        onRefresh={refresh}
        onLocate={locate}
        beforeSubmit={prepareSearch}
        onSearching={setSearching}
        onFinished={finishSearch}
      >
        <div className="simple-examples" aria-label="试试这些物体">
          <span>试一试</span>
          {examples.map((example) => {
            const value = data?.selections.find((s) => s.id === example.selection_id);
            return (
              <button
                type="button"
                key={example.id}
                data-testid={`demo-example-${example.id}`}
                disabled={!value || disabled}
                onClick={() => {
                  if (value) {
                    setText(value.text);
                    locate(value);
                  }
                }}
              >
                {example.id === 'sink' ? '水槽' : example.id === 'knife' ? '刀具' : example.title}
              </button>
            );
          })}
          <small>已保存的示例</small>
        </div>
      </AutoLocate>
      <section className="simple-scene" aria-label="三维场景">
        <div className="simple-scene-bar">
          <div>
            <span className="simple-scene-dot" />
            <strong>Waldo Kitchen</strong>
          </div>
          <span>{live ? '拖动旋转 · 右键平移 · 滚轮缩放' : '点击物体示例，或开始浏览场景'}</span>
        </div>
        {live && data?.rgb_run_id ? (
          <GaussianLiveViewer
            presentation
            simple
            onPause={() => setLive(false)}
            runId={data.rgb_run_id}
            scene={scene}
            selectedCameraId={selection?.anchor_image_id ?? 124}
            disabled={disabled}
            objectSelection={selection}
            selectionMode={mode}
            focusSelectionRequest={focus}
            autoStartRequest={start}
            restoreSceneRequest={restore}
          />
        ) : (
          <div className="simple-scene-preview">
            <img src="/api/demo/images/124" alt="照片重建的 Waldo Kitchen 厨房场景预览" />
            <div>
              <button className="simple-primary" disabled={!canBrowse} onClick={browse}>
                <Play size={17} />
                {searching
                  ? '正在查找物体…'
                  : data?.offline_gpu_busy
                    ? '场景正忙，请稍候'
                    : !scene
                      ? '正在连接场景…'
                      : '开始浏览'}
              </button>
            </div>
          </div>
        )}
        <div className="simple-result-bar" aria-label="物体操作">
          <strong>
            {selection
              ? selection.text === 'sink'
                ? '水槽'
                : selection.text === 'knife'
                  ? '刀具'
                  : selection.text
              : '整个场景'}
          </strong>
          <div className="simple-result-actions">
            <button
              data-testid="selection-highlight"
              disabled={!selection || disabled}
              aria-pressed={mode === 'highlight'}
              onClick={() => display('highlight')}
            >
              <Highlighter size={16} />
              高亮
            </button>
            <button
              data-testid="selection-isolate"
              disabled={!selection || disabled}
              aria-pressed={mode === 'isolate'}
              onClick={() => display('isolate')}
            >
              <Layers3 size={16} />
              单独查看
            </button>
            <button
              data-testid="selection-focus"
              disabled={!selection || disabled}
              onClick={() => {
                if (mode === 'none') setMode('highlight');
                setFocus((v) => v + 1);
                browse();
              }}
            >
              <Focus size={16} />
              回到物体
            </button>
            <button
              data-testid="selection-clear"
              disabled={!canBrowse}
              onClick={() => {
                setMode('none');
                setRestore((v) => v + 1);
                browse();
              }}
            >
              <RotateCcw size={16} />
              恢复场景
            </button>
            {selection ? (
              <a data-testid="selection-export" href={selection.export_url} download>
                <Download size={16} />
                导出物体
              </a>
            ) : (
              <button disabled>
                <Download size={16} />
                导出物体
              </button>
            )}
          </div>
        </div>
      </section>
      {error && (
        <p className="simple-error" role="alert">
          {error}
        </p>
      )}
      <footer className="simple-footer">
        当前演示：Waldo Kitchen · 仅显示模型中可见的物体表面
      </footer>
    </main>
  );
}
