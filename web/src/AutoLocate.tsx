import { useEffect, useRef, useState, type ReactNode } from 'react';
import { LoaderCircle, Search } from 'lucide-react';
import { api, json } from './api';
import type { ObjectSelection } from './objectSelection';
import { canonicalText } from './SelectionTasks';

export type LocateCandidate = {
  id: string;
  image_id: number;
  thumbnail_url: string;
  support_views: number[];
  ranking_score: number;
};
export type AutoLocateJob = {
  id: string;
  status: 'running' | 'completed' | 'rejected' | 'needs_choice' | 'failed';
  request: { model: string; text: string };
  message: string;
  progress?: number;
  searched_views?: number[];
  selection_id?: string;
  candidates?: LocateCandidate[];
  report_url: string;
};
export function matchingAutoLocation(job: AutoLocateJob, text: string, model: string) {
  return job.request.model === model && canonicalText(job.request.text) === canonicalText(text);
}
export function autoLocationReady(job: AutoLocateJob, text: string, model: string) {
  return job.status === 'completed' && model === 'raw3' && matchingAutoLocation(job, text, model);
}
export default function AutoLocate({
  model,
  text,
  onTextChange,
  jobs,
  selections,
  busy,
  pending,
  onRefresh,
  onLocate,
  beforeSubmit,
  onSearching,
  onFinished,
  children,
}: {
  model: string;
  text: string;
  onTextChange: (text: string) => void;
  jobs: AutoLocateJob[];
  selections: ObjectSelection[];
  busy: boolean;
  pending: boolean;
  onRefresh: () => void;
  onLocate: (selection: ObjectSelection) => void;
  beforeSubmit: () => Promise<void>;
  onSearching: (searching: boolean) => void;
  onFinished: () => void;
  children?: ReactNode;
}) {
  const [local, setLocal] = useState<AutoLocateJob | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const requested = useRef<string | null>(null);
  const handled = useRef(new Set<string>());
  const finished = useRef(new Set<string>());
  const job = local && matchingAutoLocation(local, text, model) ? local : null;
  // Keep tracking a running job even if the surrounding page refreshes its catalogue.
  const active = local?.status === 'running' ? local : null;
  const working = submitting || !!active;
  useEffect(() => {
    onSearching(working);
  }, [working, onSearching]);
  useEffect(() => {
    if (!active) return;
    let alive = true;
    const timer = window.setInterval(() => {
      void api<AutoLocateJob>(`/api/demo/auto-locate/${active.id}`)
        .then((next) => {
          if (!alive) return;
          setLocal(next);
          if (next.status !== 'running') onRefresh();
        })
        .catch(() => {
          if (alive) setError('连接暂时中断，正在重试…');
        });
    }, 1500);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [active?.id, onRefresh]);
  useEffect(() => {
    // Reattach to current-version work already running when the browser was refreshed.
    if (!local) {
      const running = jobs.find((j) => j.status === 'running');
      if (running) {
        setLocal(running);
        onTextChange(running.request.text);
        requested.current = running.id;
      }
    }
  }, [jobs, local, onTextChange]);
  useEffect(() => {
    if (!job || working || job.status === 'needs_choice' || finished.current.has(job.id)) return;
    finished.current.add(job.id);
    onFinished();
  }, [job, working, onFinished]);
  useEffect(() => {
    if (
      !job ||
      !autoLocationReady(job, text, model) ||
      busy ||
      requested.current !== job.id ||
      handled.current.has(job.id)
    )
      return;
    const selection = selections.find((s) => s.id === job.selection_id);
    if (selection) {
      handled.current.add(job.id);
      onLocate(selection);
    }
  }, [job, selections, busy, onLocate, text, model]);
  async function submit(candidate?: string) {
    if (working || (candidate && !job)) return;
    setError('');
    setSubmitting(true);
    try {
      await beforeSubmit();
      const next = await api<AutoLocateJob>(
        candidate ? `/api/demo/auto-locate/${job!.id}/choose` : '/api/demo/auto-locate',
        json(candidate ? { candidate_id: candidate } : { model, text }),
      );
      requested.current = next.id;
      handled.current.delete(next.id);
      finished.current.delete(next.id);
      setLocal(next);
      onRefresh();
    } catch (reason) {
      setError(String(reason));
      onFinished();
    } finally {
      setSubmitting(false);
    }
  }
  return (
    <section className="simple-search" aria-label="查找物体">
      <form
        className="simple-search-row"
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <label className="simple-search-input">
          <Search size={20} />
          <span className="sr-only">物体描述</span>
          <input
            value={text}
            maxLength={160}
            disabled={working}
            placeholder="输入英文物体名称，例如 sink 或 knife"
            onChange={(event) => {
              onTextChange(event.target.value);
              setError('');
            }}
          />
        </label>
        <button
          className="simple-primary"
          data-testid="auto-locate-submit"
          type="submit"
          disabled={!text.trim() || pending || busy || working}
        >
          {working ? <LoaderCircle className="spin" size={18} /> : <Search size={18} />}
          {working ? '正在查找…' : '查找物体'}
        </button>
      </form>
      {children}
      {busy && !working && (
        <p className="simple-notice" role="status">
          场景正在处理任务，请稍后再试。
        </p>
      )}
      {job && (
        <div
          className="simple-search-status"
          role="status"
          data-testid={`auto-locate-${job.status}`}
        >
          <strong>
            {job.status === 'running'
              ? '正在搜索并确认物体…'
              : job.status === 'completed'
                ? `已找到 ${job.request.text}`
                : job.status === 'needs_choice'
                  ? '请选择你要找的区域'
                  : job.status === 'rejected'
                    ? '暂时无法可靠定位这个物体，请尝试其他描述。'
                    : '查找失败，请重试。'}
          </strong>
          {active && <progress max={1} value={job.progress ?? 0} />}
          {job.status === 'needs_choice' && (
            <div className="simple-candidates">
              {job.candidates?.map((candidate, index) => (
                <button
                  type="button"
                  key={candidate.id}
                  data-testid="auto-locate-choose"
                  disabled={busy || working}
                  onClick={() => void submit(candidate.id)}
                >
                  <img
                    src={candidate.thumbnail_url}
                    alt={`候选物体 ${index + 1}，绿色标出目标区域`}
                  />
                  <span>选择这个区域</span>
                </button>
              ))}
            </div>
          )}
        </div>
      )}
      {error && (
        <p className="simple-error" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}
