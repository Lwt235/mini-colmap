// Full SfM and radiance-field workspace; mounted directly as the training view.
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Activity,
  ArrowDownToLine,
  Box,
  Check,
  ChevronDown,
  ChevronRight,
  ChevronsUpDown,
  Clock,
  FileJson,
  FolderInput,
  FolderOpen,
  History,
  Image as ImageIcon,
  Info,
  LoaderCircle,
  PanelLeftClose,
  PanelRightClose,
  Play,
  Plus,
  Settings2,
  Square,
  Terminal,
  TriangleAlert,
  Trash2,
  X,
} from 'lucide-react';
import type {
  Artifact,
  Config,
  Dataset,
  DatasetSummary,
  DisplayOptions,
  LogEntry,
  Reports,
  Run,
  Scene,
  Selection,
} from './types';
import { api, json, number, stageName, snapshotName } from './api';
import Viewer from './Viewer';
import ConfigPanel from './ConfigPanel';
import Inspector from './Inspector';
import TimingPanel from './TimingPanel';
import ImportDialog from './ImportDialog';
import RenderingPanel from './RenderingPanel';
import { isRenderingRun } from './rendering';
import type { RenderCamera } from './rendering';
import type { RenderPreview } from './types';
import type { SearchScene } from './sceneTypes';

const statusNames: Record<string, string> = {
  running: '重建进行中',
  completed: '重建完成',
  failed: '重建失败',
  cancelled: '已取消',
  interrupted: '运行中断',
  imported: '已导入模型',
  todo: '运行未完成',
};
function runStatus(run: Run) {
  if (isRenderingRun(run)) {
    return (
      (
        {
          running: '训练进行中',
          completed: '训练完成',
          failed: '训练失败',
          todo: '运行未完成',
        } as Record<string, string>
      )[run.status] ??
      statusNames[run.status] ??
      run.status
    );
  }
  if (
    run.status === 'completed' &&
    run.registered_images != null &&
    run.image_count != null &&
    run.registered_images < run.image_count
  ) {
    return `部分重建 ${run.registered_images}/${run.image_count}`;
  }
  return statusNames[run.status] ?? run.status;
}
const initialOptions: DisplayOptions = {
  points: true,
  cameras: true,
  axes: true,
  pointSize: 2.5,
  cameraScale: 1,
  color: 'rgb',
  maxError: 0,
  minTrack: 0,
};
const phases = [
  ['features', '特征'],
  ['pair_geometry', '匹配'],
  ['tracks', '轨迹'],
  ['initialization', '初始化'],
  ['incremental_mapping', '建图'],
  ['bundle_adjustment', 'BA'],
  ['export', '导出'],
];
function logText(v: Record<string, unknown>): string {
  if (typeof v.message === 'string') return v.message;
  if (typeof v.text === 'string') return v.text;
  const stage = typeof v.stage === 'string' ? stageName(v.stage) : '';
  const details = Object.entries(v)
    .filter(([k]) => !['kind', 'type', 'stage', 'timestamp', 'time', 'seq', 'snapshot'].includes(k))
    .map(([k, value]) => `${k}: ${typeof value === 'object' ? JSON.stringify(value) : value}`)
    .join(' · ');
  return [stage, details].filter(Boolean).join(' · ') || String(v.kind ?? '状态更新');
}
function timeLabel(seconds: number | undefined) {
  if (seconds == null) return '—';
  const s = Math.round(seconds);
  return s < 60 ? `${s} 秒` : `${Math.floor(s / 60)} 分 ${s % 60} 秒`;
}

export default function Workbench({ onOpenScene }: { onOpenScene: (scene: SearchScene) => void }) {
  const [deletion, setDeletion] = useState<{
    run_id: string;
    run_name: string;
    paths: string[];
    bytes: number;
    files: number;
    blockers: string[];
    token: string;
  } | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [datasets, setDatasets] = useState<DatasetSummary[]>([]),
    [runs, setRuns] = useState<Run[]>([]),
    [defaults, setDefaults] = useState<Config | null>(null),
    [config, setConfig] = useState<Config | null>(null),
    [datasetId, setDatasetId] = useState(''),
    [dataset, setDataset] = useState<Dataset | null>(null),
    [runId, setRunId] = useState(''),
    [run, setRun] = useState<Run | null>(null),
    [scene, setScene] = useState<Scene | null>(null),
    [snapshot, setSnapshot] = useState('latest'),
    [reports, setReports] = useState<Reports | null>(null),
    [artifacts, setArtifacts] = useState<Artifact[]>([]),
    [selection, setSelection] = useState<Selection>(null),
    [cameraViewRequest, setCameraViewRequest] = useState<{
      imageId: number;
      version: number;
    } | null>(null),
    [viewCameraId, setViewCameraId] = useState<number | null>(null),
    [workspaceMode, setWorkspaceMode] = useState<'scene' | 'rendering'>('scene'),
    [options, setOptions] = useState(initialOptions),
    [tab, setTab] = useState<'images' | 'config' | 'history'>('images'),
    [bottomTab, setBottomTab] = useState<'log' | 'diagnostics'>('log'),
    [logs, setLogs] = useState<LogEntry[]>([]),
    [banner, setBanner] = useState<{ text: string; error: boolean } | null>(null),
    [booting, setBooting] = useState(true),
    [busy, setBusy] = useState(false),
    [sceneLoading, setSceneLoading] = useState(false),
    [importing, setImporting] = useState(false),
    [leftHidden, setLeftHidden] = useState(false),
    [rightHidden, setRightHidden] = useState(false),
    [bottomExpanded, setBottomExpanded] = useState(false),
    [help, setHelp] = useState(false),
    [connection, setConnection] = useState(''),
    [refresh, setRefresh] = useState(0),
    [reportRefresh, setReportRefresh] = useState(0);
  const activeRun = runs.find((r) => r.status === 'running');
  const modelInput = useRef<HTMLInputElement>(null),
    logEnd = useRef<HTMLDivElement>(null),
    reloadScene = useRef<(() => void) | null>(null);
  const notify = useCallback((text: string, error = true) => setBanner({ text, error }), []);
  const updateRun = useCallback((r: Run) => {
    setRun(r);
    setRuns((old) => [r, ...old.filter((o) => o.id !== r.id)]);
  }, []);
  const selectRun = useCallback((r: Run) => {
    try {
      localStorage.setItem('minicolmap.lastRun', r.id);
    } catch {
      /* Storage may be disabled by the browser. */
    }
    setRunId(r.id);
    setWorkspaceMode(isRenderingRun(r) ? 'rendering' : 'scene');
    setRun(r);
    setSnapshot('latest');
    setSelection(null);
    setScene(null);
    setReports(null);
    setArtifacts([]);
    setLogs([]);
    setRefresh((value) => value + 1);
    if (r.dataset_id) setDatasetId(r.dataset_id);
  }, []);
  const selectDataset = useCallback((id: string) => {
    setDatasetId(id);
    setWorkspaceMode('scene');
    setDataset((current) => (current?.id === id ? current : null));
    setRunId('');
    setRun(null);
    setScene(null);
    setSceneLoading(false);
    setSelection(null);
    setReports(null);
    setArtifacts([]);
    setLogs([]);
    setSnapshot('latest');
    setTab('images');
  }, []);
  useEffect(() => {
    let cancelled = false;
    api<{ datasets: DatasetSummary[]; runs: Run[]; defaults: Config }>('/api/bootstrap')
      .then((data) => {
        if (cancelled) return;
        setDatasets(data.datasets);
        setRuns(data.runs);
        setDefaults(data.defaults);
        setConfig(structuredClone(data.defaults));
        let remembered = '';
        try {
          remembered = localStorage.getItem('minicolmap.lastRun') ?? '';
        } catch {
          /* Storage is optional. */
        }
        const first =
          data.runs.find((r) => r.status === 'running') ??
          data.runs.find((r) => r.id === remembered) ??
          data.runs.find((r) => r.name === 'temple_ring_8_arc_no_ba_01') ??
          data.runs.find((r) => r.name.includes('temple_ring_8_arc_no_ba')) ??
          data.runs[0];
        if (first) selectRun(first);
        const preferred = data.datasets.find((d) => d.name.includes('8')) ?? data.datasets[0];
        if (!first?.dataset_id && preferred) setDatasetId(preferred.id);
      })
      .catch((e) => {
        if (!cancelled) notify(String(e));
      })
      .finally(() => {
        if (!cancelled) setBooting(false);
      });
    return () => {
      cancelled = true;
    };
  }, [notify, selectRun]);
  useEffect(() => {
    if (!datasetId) {
      setDataset(null);
      return;
    }
    let cancelled = false;
    api<Dataset>(`/api/datasets/${encodeURIComponent(datasetId)}`)
      .then((d) => {
        if (!cancelled) setDataset(d);
      })
      .catch((e) => {
        if (!cancelled) notify(String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [datasetId, notify]);
  useEffect(() => {
    if (!runId) return;
    let cancelled = false,
      loading = false,
      pending = false;
    const controller = new AbortController();
    const load = async () => {
      if (cancelled) return;
      if (loading) {
        pending = true;
        return;
      }
      loading = true;
      setSceneLoading(true);
      try {
        const value = await api<Scene>(
          `/api/runs/${encodeURIComponent(runId)}/scene?snapshot=${encodeURIComponent(snapshot)}`,
          { signal: controller.signal },
        );
        if (!cancelled) setScene(value);
      } catch {
        if (!cancelled) setScene(null);
      } finally {
        loading = false;
        if (!cancelled) {
          setSceneLoading(false);
          if (pending) {
            pending = false;
            void load();
          }
        }
      }
    };
    reloadScene.current = () => void load();
    return () => {
      cancelled = true;
      controller.abort();
      reloadScene.current = null;
    };
  }, [runId, snapshot]);
  useEffect(() => {
    reloadScene.current?.();
  }, [runId, snapshot, refresh]);
  useEffect(() => {
    if (!runId) return;
    let cancelled = false;
    Promise.all([
      api<Reports>(`/api/runs/${encodeURIComponent(runId)}/reports`),
      api<Artifact[]>(`/api/runs/${encodeURIComponent(runId)}/artifacts`),
    ])
      .then(([r, a]) => {
        if (!cancelled) {
          setReports(r);
          setArtifacts(a);
        }
      })
      .catch((e) => {
        if (!cancelled && run?.status !== 'running') notify(String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [runId, run?.status, reportRefresh, notify]);
  useEffect(() => {
    if (!runId || run?.status !== 'running') {
      setConnection('');
      return;
    }
    const source = new EventSource(`/api/runs/${encodeURIComponent(runId)}/events`);
    let timer: ReturnType<typeof setTimeout> | undefined,
      closed = false,
      sceneChanged = false,
      reportsChanged = false;
    const refreshRun = (sceneUpdate = false, reportUpdate = false) => {
      sceneChanged ||= sceneUpdate;
      reportsChanged ||= reportUpdate;
      if (timer) return;
      timer = setTimeout(() => {
        timer = undefined;
        api<Run>(`/api/runs/${encodeURIComponent(runId)}`)
          .then((r) => {
            if (!closed) updateRun(r);
          })
          .catch(() => {});
        if (sceneChanged) setRefresh((n) => n + 1);
        if (reportsChanged) setReportRefresh((n) => n + 1);
        sceneChanged = reportsChanged = false;
      }, 300);
    };
    source.onopen = () => {
      setConnection('已连接');
      refreshRun(true, true);
    };
    source.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data) as Record<string, unknown>;
        setLogs((old) =>
          [
            ...old,
            {
              time: new Date(
                typeof data.time === 'number' ? data.time * 1000 : Date.now(),
              ).toLocaleTimeString('zh-CN', { hour12: false }),
              kind: String(data.kind ?? data.type ?? 'info'),
              text: logText(data),
            },
          ].slice(-500),
        );
        const kind = String(data.kind ?? '');
        const terminal = ['complete', 'terminal', 'error', 'cancelled', 'todo'].includes(kind);
        refreshRun(
          kind === 'snapshot' || terminal,
          kind === 'timing' || kind === 'warning' || terminal,
        );
      } catch {
        /* Ignore empty keep-alive lines. */
      }
    };
    source.onerror = () => {
      setConnection('连接恢复中…');
    };
    return () => {
      closed = true;
      source.close();
      if (timer) clearTimeout(timer);
    };
  }, [runId, run?.status, updateRun]);
  useEffect(() => {
    if (!activeRun) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = () => {
      api<Run>(`/api/runs/${encodeURIComponent(activeRun.id)}`)
        .then((current) => {
          if (cancelled) return;
          setRuns((old) => old.map((item) => (item.id === current.id ? current : item)));
          if (current.id === runId) {
            setRun(current);
            if (current.status !== 'running') {
              setRefresh((n) => n + 1);
              setReportRefresh((n) => n + 1);
            }
          }
        })
        .catch(() => {})
        .finally(() => {
          if (!cancelled) timer = setTimeout(poll, 2000);
        });
    };
    timer = setTimeout(poll, 2000);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [activeRun?.id, runId]);
  useEffect(() => {
    logEnd.current?.scrollIntoView({ block: 'nearest' });
  }, [logs]);
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setSelection(null);
        setHelp(false);
      }
    };
    window.addEventListener('keydown', key);
    return () => window.removeEventListener('keydown', key);
  }, []);
  async function prepareDeletion(id: string) {
    try {
      setDeletion(await api(`/api/runs/${encodeURIComponent(id)}/deletion`));
    } catch (reason) {
      notify(String(reason));
    }
  }
  async function deleteRun() {
    if (!deletion) return;
    setDeleting(true);
    try {
      const removed = await api<{ cleanup_pending: boolean }>(
        `/api/runs/${encodeURIComponent(deletion.run_id)}`,
        {
          method: 'DELETE',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ token: deletion.token }),
        },
      );
      const remaining = runs.filter((r) => r.id !== deletion.run_id);
      setRuns(remaining);
      if (runId === deletion.run_id) {
        if (remaining[0]) selectRun(remaining[0]);
        else {
          selectDataset(datasetId);
          try {
            localStorage.removeItem('minicolmap.lastRun');
          } catch {}
        }
      }
      setDeletion(null);
      notify(
        removed.cleanup_pending ? '任务已移除，部分文件待清理' : '任务及关联文件已删除',
        removed.cleanup_pending,
      );
    } catch (reason) {
      notify(String(reason));
      setDeletion(null);
    } finally {
      setDeleting(false);
    }
  }
  async function openSearchScene() {
    if (!run || run.pipeline !== '3dgs') return;
    setBusy(true);
    try {
      const created = await api<SearchScene>(
        '/api/scenes',
        json({ rgb_run_id: run.id, name: dataset?.name ?? run.name }),
      );
      const catalogue = await api<{ scenes: SearchScene[] }>('/api/scenes');
      onOpenScene(
        catalogue.scenes.find((s) => s.id === created.id) ?? {
          ...created,
          ready: false,
          preview_url: `/api/scenes/${created.id}/photo`,
        },
      );
    } catch (reason) {
      notify(String(reason));
    } finally {
      setBusy(false);
    }
  }
  async function start() {
    if (!datasetId || !config) return;
    setBusy(true);
    setBanner(null);
    try {
      const checked = await api<Config>('/api/config/validate', json(config));
      setConfig(checked);
      const r = await api<Run>('/api/runs', json({ dataset_id: datasetId, config: checked }));
      updateRun(r);
      selectRun(r);
      setBottomTab('log');
    } catch (e) {
      notify(String(e));
    } finally {
      setBusy(false);
    }
  }
  async function cancel() {
    const active = runs.find((r) => r.status === 'running');
    if (!active) return;
    setBusy(true);
    try {
      const result = await api<Run>(`/api/runs/${encodeURIComponent(active.id)}/cancel`, {
        method: 'POST',
      });
      if (result.id === runId) {
        updateRun(result);
        setRefresh((n) => n + 1);
      } else {
        setRuns((old) => [result, ...old.filter((item) => item.id !== result.id)]);
      }
    } catch (e) {
      notify(String(e));
    } finally {
      setBusy(false);
    }
  }
  async function importModel(file: File) {
    setBusy(true);
    try {
      const data = new FormData();
      data.append('file', file);
      const r = await api<Run>('/api/results/import', { method: 'POST', body: data });
      updateRun(r);
      selectRun(r);
      notify('模型已导入。原始照片缺失时仍可检查点云与位姿。', false);
    } catch (e) {
      notify(String(e));
    } finally {
      setBusy(false);
    }
  }
  async function renderFreeView(camera: RenderCamera) {
    if (!run || !isRenderingRun(run) || run.status !== 'completed') return;
    setBusy(true);
    try {
      const preview = await api<RenderPreview>(
        `/api/rendering/runs/${encodeURIComponent(run.id)}/render`,
        json({ camera }),
      );
      updateRun({ ...run, latest_preview: preview });
      setWorkspaceMode('rendering');
    } catch (error) {
      notify(String(error));
    } finally {
      setBusy(false);
    }
  }
  const images =
    scene?.cameras ??
    dataset?.images.map((i) => ({
      ...i,
      image_id: i.id,
      registered: false,
      observations: 0,
      K: dataset.cameras.find((c) => c.id === i.camera_id)?.K ?? [],
    })) ??
    [];
  const summary = scene?.summary,
    registered = summary?.registered_images ?? run?.registered_images ?? 0,
    total = summary?.image_count ?? run?.image_count ?? dataset?.image_count ?? 0,
    snapshots = scene?.snapshots?.length ? scene.snapshots : (run?.snapshots ?? []);
  const currentPhase = phases.findIndex(
    ([key]) =>
      key === run?.stage ||
      (key === 'bundle_adjustment' && run?.stage === 'ba') ||
      (key === 'export' && run?.stage === 'exporting') ||
      (key === 'features' && run?.stage === 'feature_extraction') ||
      (key === 'incremental_mapping' && ['registration', 'mapping'].includes(run?.stage ?? '')) ||
      (key === 'pair_geometry' && run?.stage === 'matching'),
  );
  const missingImages = run && !run.images_available;
  return (
    <div
      className={`app ${leftHidden ? 'left-hidden' : ''} ${rightHidden || workspaceMode !== 'scene' ? 'right-hidden' : ''}`}
    >
      <header className="topbar">
        <div className="project-heading">
          <span className="project-divider" />
          <FolderOpen size={15} />
          <span>{dataset?.name ?? '未选择数据集'}</span>
          <span className="local-pill">
            <i />
            WORKSPACE
          </span>
        </div>
        <div className="top-actions">
          <button
            className="quiet"
            data-testid="import-model"
            onClick={() => modelInput.current?.click()}
            disabled={busy}
          >
            <FileJson size={15} />
            打开模型
          </button>
          <input
            ref={modelInput}
            hidden
            type="file"
            accept=".json"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) void importModel(f);
              e.target.value = '';
            }}
          />
          <details className="export-menu">
            <summary className={`button quiet ${!run ? 'disabled' : ''}`}>
              <ArrowDownToLine size={15} />
              导出
              <ChevronDown size={12} />
            </summary>
            <div className="dropdown">
              {artifacts.length ? (
                artifacts.map((a) => (
                  <a href={a.url} key={a.url} download>
                    {/\.png|\.jpe?g/i.test(a.name) ? (
                      <ImageIcon size={13} />
                    ) : (
                      <FileJson size={13} />
                    )}
                    <span>{a.name}</span>
                    <ArrowDownToLine size={12} />
                  </a>
                ))
              ) : (
                <p>当前还没有可下载的结果。</p>
              )}
            </div>
          </details>
          <button
            className={`quiet ${workspaceMode === 'rendering' ? 'selected' : ''}`}
            data-testid="open-rendering"
            onClick={() => setWorkspaceMode(workspaceMode === 'rendering' ? 'scene' : 'rendering')}
          >
            NeRF / 3DGS
          </button>
          {run?.pipeline === '3dgs' && ['completed', 'imported'].includes(run.status) && (
            <button
              className="quiet"
              data-testid="open-scene-search"
              disabled={busy}
              onClick={() => void openSearchScene()}
            >
              用于语义检索
            </button>
          )}
          <span className="action-divider" />
          {activeRun ? (
            <button
              className="cancel-button"
              data-testid="cancel-run"
              disabled={busy}
              onClick={() => void cancel()}
            >
              <Square size={13} fill="currentColor" />
              {isRenderingRun(activeRun) ? '取消训练' : '取消重建'}
            </button>
          ) : (
            <button
              className="primary start-button"
              data-testid="start-run"
              disabled={busy || !datasetId || booting || workspaceMode !== 'scene'}
              onClick={() => void start()}
            >
              {busy ? (
                <LoaderCircle size={15} className="spin" />
              ) : (
                <Play size={14} fill="currentColor" />
              )}
              开始 SfM 重建
            </button>
          )}
          <button
            className="icon-button help-button"
            aria-label="操作说明"
            title="操作说明"
            onClick={() => setHelp(true)}
          >
            <Info size={17} />
          </button>
        </div>
      </header>
      {banner && (
        <div className={`notice ${banner.error ? 'error' : 'success'}`} role="alert">
          {banner.error ? <TriangleAlert size={15} /> : <Check size={15} />}
          <span>{banner.text}</span>
          <button aria-label="关闭通知" onClick={() => setBanner(null)}>
            <X size={14} />
          </button>
        </div>
      )}
      <div className="workspace">
        <aside className="left-panel">
          <div className="workspace-label">
            <span>工作区</span>
            <button
              className="icon-button"
              title="收起左侧栏"
              aria-label="收起左侧栏"
              onClick={() => setLeftHidden(true)}
            >
              <PanelLeftClose size={15} />
            </button>
          </div>
          <div className="dataset-picker">
            <label>当前输入数据</label>
            <div className="select-wrapper">
              <FolderOpen size={15} />
              <select
                aria-label="选择输入数据集"
                value={datasetId}
                disabled={!!activeRun}
                onChange={(e) => {
                  selectDataset(e.target.value);
                }}
              >
                <option value="" disabled>
                  选择数据集
                </option>
                {datasets.map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.name} · {d.image_count} 张
                  </option>
                ))}
              </select>
              <ChevronsUpDown size={12} />
            </div>
            <button
              className="outline full import-dataset"
              data-testid="import-dataset"
              disabled={!!activeRun}
              onClick={() => setImporting(true)}
            >
              <Plus size={14} />
              导入数据集
            </button>
          </div>
          <nav className="side-tabs">
            <button className={tab === 'images' ? 'selected' : ''} onClick={() => setTab('images')}>
              <ImageIcon size={14} />
              照片
            </button>
            <button className={tab === 'config' ? 'selected' : ''} onClick={() => setTab('config')}>
              <Settings2 size={14} />
              参数
            </button>
            <button
              className={tab === 'history' ? 'selected' : ''}
              onClick={() => setTab('history')}
            >
              <History size={14} />
              历史
            </button>
          </nav>
          <div className="left-content">
            {tab === 'images' ? (
              <>
                <div className="list-heading">
                  <span>{scene ? '当前模型图像' : '输入照片'}</span>
                  <span>{images.length} 张</span>
                </div>
                {images.length ? (
                  <div className="image-list">
                    {images.map((image) => (
                      <button
                        className={`image-row ${selection?.kind === 'camera' && selection.id === image.image_id ? 'selected' : ''}`}
                        key={image.image_id}
                        data-testid={`image-${image.image_id}`}
                        onClick={() => {
                          if (scene || dataset) {
                            setSelection({ kind: 'camera', id: image.image_id });
                            setRightHidden(false);
                          }
                        }}
                      >
                        <div className="image-thumbnail">
                          {image.image_url ? (
                            <img
                              loading="lazy"
                              src={image.image_url}
                              alt=""
                              onError={(e) => {
                                e.currentTarget.style.visibility = 'hidden';
                              }}
                            />
                          ) : (
                            <ImageIcon size={17} />
                          )}
                        </div>
                        <div>
                          <strong>{image.name}</strong>
                          <small>
                            #{image.image_id}{' '}
                            <span>
                              {image.registered
                                ? `${number(image.observations, 0)} 观测`
                                : `${image.width} × ${image.height}`}
                            </span>
                          </small>
                        </div>
                        <span
                          className={`registration-dot ${image.registered ? 'registered' : ''}`}
                          title={image.registered ? '已注册' : '尚未注册'}
                        />
                      </button>
                    ))}
                  </div>
                ) : (
                  <div className="sidebar-empty">
                    <FolderInput size={26} />
                    <p>导入照片以开始重建</p>
                  </div>
                )}
                {scene && (
                  <div className="registration-key">
                    <i className="registration-dot registered" />
                    已注册{' '}
                    <span>
                      {registered} / {total}
                    </span>
                  </div>
                )}
              </>
            ) : tab === 'config' && config && defaults ? (
              <ConfigPanel
                config={config}
                defaults={defaults}
                disabled={!!activeRun}
                onChange={setConfig}
                onError={notify}
                onValidate={() => {
                  api<Config>('/api/config/validate', json(config))
                    .then((v) => {
                      setConfig(v);
                      notify('配置检查通过，可以开始重建。', false);
                    })
                    .catch((e) => notify(String(e)));
                }}
              />
            ) : tab === 'history' ? (
              <>
                <div className="list-heading">
                  <span>本地任务记录</span>
                  <button
                    className="text-button"
                    aria-label="刷新历史运行"
                    title="刷新并清除输出目录已删除的历史记录"
                    onClick={() => {
                      api<Run[]>('/api/runs?discover=true')
                        .then((items) => {
                          const removed = runs.filter(
                            (old) => !items.some((item) => item.id === old.id),
                          ).length;
                          setRuns(items);
                          if (runId && !items.some((item) => item.id === runId)) {
                            if (items[0]) selectRun(items[0]);
                            else {
                              setRunId('');
                              setRun(null);
                              setScene(null);
                              setSceneLoading(false);
                              setSelection(null);
                              setReports(null);
                              setArtifacts([]);
                              setLogs([]);
                              setSnapshot('latest');
                              setWorkspaceMode('scene');
                              try {
                                localStorage.removeItem('minicolmap.lastRun');
                              } catch {
                                /* Storage is optional. */
                              }
                            }
                          }
                          notify(
                            removed ? `已清理 ${removed} 条失效历史记录` : '历史已刷新',
                            false,
                          );
                        })
                        .catch((e) => notify(String(e)));
                    }}
                  >
                    刷新
                  </button>
                </div>
                <div className="run-list">
                  {runs.map((r) => (
                    <div className="run-history-item" key={r.id}>
                      <button
                        className={`run-row ${runId === r.id ? 'selected' : ''}`}
                        onClick={() => selectRun(r)}
                      >
                        <div className="run-row-top">
                          <span
                            className={`status-dot ${r.status === 'completed' || r.status === 'imported' ? 'good' : r.status === 'running' ? 'live' : r.status === 'failed' ? 'bad' : ''}`}
                          />
                          <strong>{r.name}</strong>
                        </div>
                        <span>{runStatus(r)}</span>
                        <small>
                          {isRenderingRun(r) ? (
                            `${r.pipeline?.toUpperCase()} · ${r.training_metrics?.step ?? r.completed ?? 0} / ${r.training_metrics?.total ?? r.total ?? '—'} 步`
                          ) : (
                            <>
                              {r.registered_images ?? '—'}/{r.image_count ?? '—'} 相机 ·{' '}
                              {number(r.point_count, 0)} 点
                            </>
                          )}
                        </small>
                      </button>
                      <button
                        className="run-remove"
                        aria-label={`删除任务 ${r.name}`}
                        title="删除任务及关联文件"
                        disabled={r.status === 'running'}
                        onClick={() => void prepareDeletion(r.id)}
                      >
                        <Trash2 size={14} />
                      </button>
                    </div>
                  ))}
                </div>
              </>
            ) : null}
          </div>
          <div className="sidebar-footer">
            <span className="live-dot" />
            重建与训练
          </div>
        </aside>
        <main className="main-panel">
          <div className="scene-topline">
            <div className="scene-breadcrumb">
              {leftHidden && (
                <button
                  className="icon-button"
                  title="展开左侧栏"
                  aria-label="展开左侧栏"
                  onClick={() => setLeftHidden(false)}
                >
                  <ChevronRight size={16} />
                </button>
              )}
              <Box size={14} />
              <span>{run?.name ?? '尚无重建结果'}</span>
              {run && (
                <span
                  className={`run-state ${run.status} ${runStatus(run).startsWith('部分重建') ? 'partial' : ''}`}
                >
                  <i />
                  {runStatus(run)}
                </span>
              )}
              {sceneLoading && <LoaderCircle size={12} className="spin" />}
            </div>
            <div className="scene-actions">
              <div className="workspace-mode" aria-label="工作区视图">
                <button
                  className={workspaceMode === 'scene' ? 'selected' : ''}
                  onClick={() => setWorkspaceMode('scene')}
                >
                  {run?.source_kind === 'posed_dataset'
                    ? '相机位姿场景'
                    : isRenderingRun(run)
                      ? '源 SfM 场景'
                      : '三维场景'}
                </button>
                <button
                  className={workspaceMode === 'rendering' ? 'selected' : ''}
                  onClick={() => setWorkspaceMode('rendering')}
                >
                  训练 / 渲染
                </button>
              </div>
              <label className="snapshot-select">
                <Clock size={13} />
                <select
                  aria-label="选择场景快照"
                  value={snapshot}
                  disabled={!snapshots.length}
                  onChange={(e) => {
                    setSnapshot(e.target.value);
                    setSelection(null);
                  }}
                >
                  <option value="latest">
                    最新结果{run?.status === 'running' ? ' · 实时' : ''}
                  </option>
                  {snapshots.map((s) => (
                    <option key={s.id} value={s.id}>
                      {snapshotName(s.label)}
                    </option>
                  ))}
                </select>
              </label>
              <button
                className="icon-button"
                aria-label={rightHidden ? '展开检查器' : '收起检查器'}
                onClick={() => setRightHidden(!rightHidden)}
              >
                <PanelRightClose size={15} />
              </button>
            </div>
          </div>
          {missingImages && (
            <div className="image-warning">
              <Info size={12} />
              此模型未关联原始照片；点云和相机位姿仍可查看。
            </div>
          )}
          {workspaceMode === 'rendering' ? (
            <RenderingPanel
              runs={runs}
              datasets={datasets}
              selectedDatasetId={datasetId}
              run={run}
              scene={scene}
              activeRun={activeRun}
              selectedCameraId={selection?.kind === 'camera' ? selection.id : null}
              onStarted={(created) => {
                updateRun(created);
                selectRun(created);
                setBottomTab('log');
              }}
              onError={notify}
              onPrepareSfM={(id) => {
                if (id) selectDataset(id);
                setWorkspaceMode('scene');
                setTab('images');
                setLeftHidden(false);
              }}
              onImportModel={() => modelInput.current?.click()}
              onImportDataset={() => setImporting(true)}
            />
          ) : (
            <Viewer
              scene={scene}
              runId={runId}
              options={options}
              setOptions={setOptions}
              selection={selection}
              cameraViewRequest={cameraViewRequest}
              onCameraViewChange={setViewCameraId}
              onRenderCamera={
                isRenderingRun(run) && run?.status === 'completed'
                  ? (camera) => void renderFreeView(camera)
                  : undefined
              }
              renderBusy={busy || !!activeRun}
              renderMaxEdge={run?.pipeline === '3dgs' ? (run.preview_max_edge ?? 512) : 256}
              onSelect={(s) => {
                setSelection(s);
                if (s) setRightHidden(false);
              }}
            />
          )}
          <section className={`bottom-panel ${bottomExpanded ? 'expanded' : ''}`}>
            <div className="bottom-heading">
              <div className="bottom-tabs">
                <button
                  className={bottomTab === 'log' ? 'selected' : ''}
                  onClick={() => setBottomTab('log')}
                >
                  <Terminal size={13} />
                  运行日志{run?.status === 'running' && <i className="live-dot" />}
                </button>
                <button
                  className={bottomTab === 'diagnostics' ? 'selected' : ''}
                  onClick={() => setBottomTab('diagnostics')}
                >
                  <Activity size={13} />
                  质量与诊断
                </button>
              </div>
              <div className="bottom-status">
                {run?.status === 'running' && (
                  <span className="actual-progress" data-testid="actual-progress">
                    {stageName(run.stage)}
                    {run.total != null && run.total > 0
                      ? ` ${run.completed ?? 0} / ${run.total}`
                      : ''}
                  </span>
                )}
                {connection && (
                  <span>
                    <i className="live-dot" />
                    {connection}
                  </span>
                )}
                <span>
                  <Clock size={12} />
                  {timeLabel(run?.elapsed_seconds)}
                </span>
                <button
                  className="icon-button"
                  title={bottomExpanded ? '收起日志区' : '展开日志区'}
                  aria-label="调整日志区域"
                  onClick={() => setBottomExpanded(!bottomExpanded)}
                >
                  <ChevronDown
                    size={14}
                    style={{ transform: bottomExpanded ? 'none' : 'rotate(180deg)' }}
                  />
                </button>
              </div>
            </div>
            {bottomTab === 'log' ? (
              <>
                <div className="phase-strip">
                  {(isRenderingRun(run)
                    ? [
                        ['preparing', '准备数据'],
                        ['training', '训练'],
                        ['evaluation', '验证'],
                        ['export', '导出'],
                      ]
                    : phases
                  ).map(([key, name], i) => {
                    const skipped =
                      key === 'bundle_adjustment' && run?.status === 'completed' && !reports?.ba;
                    const phase = isRenderingRun(run)
                      ? ['preparing', 'training', 'evaluation', 'export'].indexOf(run?.stage ?? '')
                      : currentPhase;
                    const complete = !skipped && (run?.status === 'completed' || phase > i);
                    return (
                      <div
                        key={key}
                        className={`${run?.status === 'running' && i === phase ? 'current' : ''} ${complete ? 'complete' : ''}`}
                        title={skipped ? '本次未启用最终 BA' : name}
                      >
                        <span>
                          {complete ? (
                            <Check size={10} />
                          ) : skipped ? (
                            '—'
                          ) : (
                            String(i + 1).padStart(2, '0')
                          )}
                        </span>
                        {name}
                        {i < phases.length - 1 && <ChevronRight size={10} />}
                      </div>
                    );
                  })}
                </div>
                <div className="log-scroll" role="log" aria-label="运行日志">
                  {!logs.length && (
                    <div className="log-line">
                      <time>—</time>
                      <span className="log-tag">SYSTEM</span>
                      <span>
                        {run?.status === 'running'
                          ? '正在连接实际运行事件…'
                          : run
                            ? `已加载本地结果：${run.name}。${run.status === 'imported' ? '此模型由文件导入。' : '完整诊断与原始文件可在“质量与诊断”和“导出”中查看。'}`
                            : '就绪。选择数据集并开始重建。'}
                      </span>
                    </div>
                  )}
                  {logs.map((entry, i) => (
                    <div
                      className={`log-line ${entry.kind === 'warning' || entry.kind === 'error' ? 'warning' : ''}`}
                      key={i}
                    >
                      <time>{entry.time}</time>
                      <span className="log-tag">{entry.kind.toUpperCase()}</span>
                      <span>{entry.text}</span>
                    </div>
                  ))}
                  {run?.error && (
                    <div className="log-line warning">
                      <time>ERROR</time>
                      <span>{run.error}</span>
                    </div>
                  )}
                  {run?.warnings?.map((w, i) => (
                    <div className="log-line warning" key={`w${i}`}>
                      <time>WARN</time>
                      <span>{w}</span>
                    </div>
                  ))}
                  <div ref={logEnd} />
                </div>
              </>
            ) : (
              <div className="diagnostics-scroll">
                <div className="metric-strip">
                  <div>
                    <span>注册相机</span>
                    <strong>
                      {registered} / {total}
                    </strong>
                  </div>
                  <div>
                    <span>三维点</span>
                    <strong>{number(summary?.point_count, 0)}</strong>
                  </div>
                  <div>
                    <span>有效观测</span>
                    <strong>{number(summary?.observation_count, 0)}</strong>
                  </div>
                  <div>
                    <span>中位误差</span>
                    <strong>
                      {number(summary?.median_reprojection_error_px)} <small>px</small>
                    </strong>
                  </div>
                  <div>
                    <span>P95 误差</span>
                    <strong>
                      {number(summary?.p95_reprojection_error_px)} <small>px</small>
                    </strong>
                  </div>
                  <div>
                    <span>正深度比例</span>
                    <strong>
                      {summary?.positive_depth_ratio == null
                        ? '—'
                        : `${number(summary.positive_depth_ratio * 100, 1)}%`}
                    </strong>
                  </div>
                </div>
                {reports?.ba && (
                  <div
                    className={`ba-notice ${reports.ba.solver_success ? 'converged' : ''}`}
                    data-testid="ba-status"
                  >
                    <Activity size={15} />
                    <strong>{reports.ba.accepted ? 'BA 候选已接受' : 'BA 候选未接受'}</strong>
                    <span>{reports.ba.solver_success ? '求解器已收敛' : '求解器尚未收敛'}</span>
                    <span>{reports.ba.nfev} 次评估</span>
                    <span>
                      {number(reports.ba.before_mean_px)} → {number(reports.ba.candidate_mean_px)}{' '}
                      px
                    </span>
                    <p>{reports.ba.rejection_reason ?? reports.ba.message}</p>
                  </div>
                )}
                {reports?.timings && <TimingPanel timing={reports.timings} />}
                {run && runStatus(run).startsWith('部分重建') && (
                  <div className="info-note">
                    流程完成，但仅 {run.registered_images}/{run.image_count}{' '}
                    张图像成功注册。请检查未注册照片与匹配质量。
                  </div>
                )}
                <div className="report-disclosures">
                  {(['pairs', 'initialization'] as const).map((key) => (
                    <details key={key}>
                      <summary>
                        {key === 'pairs' ? '图像对匹配与几何验证' : '初始化候选与筛选诊断'}
                        <ChevronDown size={12} />
                      </summary>
                      <pre>
                        {reports?.[key]
                          ? JSON.stringify(reports[key], null, 2)
                          : '暂无该阶段报告。'}
                      </pre>
                    </details>
                  ))}
                </div>
                {artifacts.some((a) => /\.(png|jpe?g)$/i.test(a.name)) && (
                  <details className="match-previews">
                    <summary>查看真实匹配预览</summary>
                    <div>
                      {artifacts
                        .filter((a) => /\.(png|jpe?g)$/i.test(a.name))
                        .map((a) => (
                          <a href={a.url} target="_blank" rel="noreferrer" key={a.url}>
                            <img src={a.url} loading="lazy" alt={a.name} />
                            <span>{a.name}</span>
                          </a>
                        ))}
                    </div>
                  </details>
                )}
              </div>
            )}
          </section>
        </main>
        {!rightHidden && workspaceMode === 'scene' && (
          <Inspector
            scene={scene}
            inputDataset={dataset}
            runId={runId}
            snapshot={scene?.snapshot_id ?? snapshot}
            selection={selection}
            onSelect={setSelection}
            onViewCamera={(imageId) =>
              setCameraViewRequest((old) => ({ imageId, version: (old?.version ?? 0) + 1 }))
            }
            viewCameraId={viewCameraId}
          />
        )}
      </div>
      <footer className="statusbar">
        <span>
          <span className="status-dot good" />
          本地工作台 <i />
          127.0.0.1
        </span>
        <span>
          {run?.status === 'running' ? stageName(run.stage) : '就绪'}
          <i />
          {run?.gpu_index != null ? `GPU ${run.gpu_index}` : '工作台'}
        </span>
      </footer>
      {deletion && (
        <div className="modal-backdrop">
          <section
            className="modal delete-run-modal"
            role="dialog"
            aria-modal="true"
            aria-label="删除历史任务"
          >
            <div className="modal-heading">
              <h2>删除任务及文件</h2>
              <button
                className="icon-button"
                aria-label="关闭删除窗口"
                disabled={deleting}
                onClick={() => setDeletion(null)}
              >
                <X size={18} />
              </button>
            </div>
            <strong>{deletion.run_name}</strong>
            {deletion.blockers.length ? (
              <div role="alert" className="delete-blockers">
                {deletion.blockers.map((text) => (
                  <p key={text}>{text}</p>
                ))}
              </div>
            ) : (
              <>
                <p>
                  将清理 {deletion.files} 个文件，约 {(deletion.bytes / 1024 / 1024).toFixed(1)}{' '}
                  MB。原始照片与共享数据集保留。
                </p>
                <ul>
                  {deletion.paths.map((path) => (
                    <li key={path}>{path}</li>
                  ))}
                </ul>
                <p>删除后无法在工作台中恢复。</p>
              </>
            )}
            <div className="delete-actions">
              <button disabled={deleting} onClick={() => setDeletion(null)}>
                取消
              </button>
              <button
                data-testid="confirm-delete-run"
                className="danger-button"
                disabled={deleting || !!deletion.blockers.length}
                onClick={() => void deleteRun()}
              >
                {deleting ? '正在删除…' : '删除任务及文件'}
              </button>
            </div>
          </section>
        </div>
      )}
      {booting && (
        <div className="boot-overlay">
          <LoaderCircle size={26} className="spin" />
          <span>正在连接本地重建引擎…</span>
        </div>
      )}
      {importing && (
        <ImportDialog
          onClose={() => setImporting(false)}
          onImported={(d) => {
            setDatasets((old) => [d, ...old.filter((x) => x.id !== d.id)]);
            selectDataset(d.id);
            if (workspaceMode === 'rendering') setWorkspaceMode('rendering');
            setDataset(d);
            setTab('images');
            notify(
              `已导入 ${d.image_count} 张图像。${(d.pose_count ?? 0) >= 2 ? `已保留 ${d.pose_count} 台真实相机位姿，可直接训练 NeRF。` : '尚无相机位姿；可先估计位姿或导入含位姿的清单。'}`,
              false,
            );
          }}
        />
      )}
      {help && (
        <div
          className="modal-backdrop"
          onMouseDown={(e) => {
            if (e.target === e.currentTarget) setHelp(false);
          }}
        >
          <section
            className="modal help-modal"
            role="dialog"
            aria-modal="true"
            aria-label="工作台操作说明"
          >
            <div className="modal-heading">
              <div>
                <span className="eyebrow">WORKBENCH GUIDE</span>
                <h2>从照片到稀疏重建</h2>
              </div>
              <button
                className="icon-button"
                onClick={() => setHelp(false)}
                aria-label="关闭操作说明"
              >
                <X size={18} />
              </button>
            </div>
            <ol>
              <li>
                <strong>选择或导入数据</strong>
                <p>使用项目数据集，或导入已去畸变照片与真实内参。</p>
              </li>
              <li>
                <strong>检查参数并运行</strong>
                <p>调整参数后开始运行，进度与预览将在工作区更新。</p>
              </li>
              <li>
                <strong>拖拽检查三维结果</strong>
                <p>左键旋转、右键平移、滚轮缩放；点击相机或点查看原始信息。Esc 清除选择。</p>
              </li>
              <li>
                <strong>对照快照与质量</strong>
                <p>切换初始化、增量建图和 BA 前后快照。BA 接受与求解器收敛分别展示。</p>
              </li>
              <li>
                <strong>导出真实结果</strong>
                <p>右上角下载模型、PLY 和其他实际生成的文件。视图调整不修改地图。</p>
              </li>
            </ol>
          </section>
        </div>
      )}
    </div>
  );
}
