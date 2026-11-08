import { useEffect, useState } from 'react';
import { ArrowRight, Box, LoaderCircle } from 'lucide-react';
import { api, json } from './api';
import type { ObjectSelection } from './objectSelection';

export type SelectionJob = {
  id: string;
  selection_id: string;
  status: 'running' | 'completed' | 'rejected' | 'failed';
  request: { model: string; image_id: number; text: string };
  stage?: string;
  progress?: number;
  message?: string;
  cached?: boolean;
  selected_gaussians?: number;
  report_url: string;
};
export const canonicalText = (text: string) => text.trim().toLowerCase().replace(/\s+/g, ' ');
export function matchingSelection(selections: ObjectSelection[], text: string, imageId: number) {
  return [...selections]
    .reverse()
    .find(
      (entry) =>
        canonicalText(entry.text) === canonicalText(text) && entry.anchor_image_id === imageId,
    );
}
const statusNames = {
  running: '生成中',
  completed: '检查通过 · 待目视确认',
  rejected: '证据不足',
  failed: '任务失败',
};

export default function SelectionTasks({
  query,
  jobs,
  selections,
  busy,
  onLocate,
  onRefresh,
  historyOnly = false,
}: {
  query: { model: string; image_id: number; text: string } | null;
  jobs: SelectionJob[];
  selections: ObjectSelection[];
  busy: boolean;
  onLocate: (selection: ObjectSelection) => void;
  onRefresh: () => void;
  historyOnly?: boolean;
}) {
  const [local, setLocal] = useState<SelectionJob | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const active = historyOnly
    ? undefined
    : local?.status === 'running'
      ? local
      : jobs.find((job) => job.status === 'running');
  useEffect(() => {
    if (!active) return;
    let alive = true;
    const timer = window.setInterval(() => {
      void api<SelectionJob>(`/api/demo/selection-jobs/${active.id}`)
        .then((next) => {
          if (!alive) return;
          setLocal(next);
          if (next.status !== 'running') onRefresh();
        })
        .catch((reason) => {
          if (alive) setError(String(reason));
        });
    }, 1500);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [active?.id, onRefresh]);
  const entries = local ? [local, ...jobs.filter((job) => job.id !== local.id)] : jobs;
  const existing = query ? matchingSelection(selections, query.text, query.image_id) : undefined;
  async function submit() {
    if (!query) return;
    setError('');
    setSubmitting(true);
    try {
      setLocal(await api<SelectionJob>('/api/demo/selections', json(query)));
      onRefresh();
    } catch (reason) {
      setError(String(reason));
    } finally {
      setSubmitting(false);
    }
  }
  return (
    <section className="selection-tasks" aria-label="文本三维选区">
      {!historyOnly &&
        query?.model === 'raw3' &&
        (existing ? (
          <button
            type="button"
            className="demo-locate-selection"
            data-testid="locate-selection"
            disabled={busy || submitting}
            onClick={() => onLocate(existing)}
          >
            <Box size={16} />
            在三维场景中定位{existing.name}
            <ArrowRight size={15} />
          </button>
        ) : (
          <>
            <button
              type="button"
              className="demo-locate-selection"
              data-testid="generate-selection"
              disabled={busy || submitting || !!active}
              onClick={() => void submit()}
            >
              {submitting || active ? (
                <LoaderCircle size={16} className="spin" />
              ) : (
                <Box size={16} />
              )}
              {submitting ? '正在提交…' : '生成三维选区'}
            </button>
            <p className="demo-input-help">
              后台融合多视角证据，通过一致性检查后开放三维查看。请先关闭实时渲染。
            </p>
          </>
        ))}
      {active && (
        <div className="selection-job-progress" role="status">
          <strong>
            {active.request.text} · {statusNames.running}
          </strong>
          <progress max={1} value={active.progress ?? 0} />
          <span>
            {active.message} · {Math.round((active.progress ?? 0) * 100)}%
          </span>
          <small>任务使用 GPU 7，期间可浏览已保存的查询和报告。</small>
        </div>
      )}
      {error && (
        <p className="demo-error" role="alert">
          {error}
        </p>
      )}
      {historyOnly && !!entries.length && (
        <details className="selection-job-history" open>
          <summary>三维选区记录</summary>
          <div className="selection-history-grid">
            {entries.slice(0, 6).map((entry) => {
              const selection = selections.find((item) => item.id === entry.selection_id);
              return (
                <article key={entry.id} data-testid={`selection-job-${entry.status}`}>
                  <strong>
                    {entry.request.text}
                    <small>相机 {entry.request.image_id}</small>
                  </strong>
                  <span>{statusNames[entry.status]}</span>
                  {entry.status !== 'running' && <p>{entry.message}</p>}
                  {selection && (
                    <button type="button" disabled={busy} onClick={() => onLocate(selection)}>
                      查看三维选区
                    </button>
                  )}
                  {entry.status === 'completed' || entry.status === 'rejected' ? (
                    <>
                      <a href={entry.report_url} target="_blank" rel="noreferrer">
                        诊断报告 ↗
                      </a>
                      {entry.selected_gaussians && (
                        <details>
                          <summary>五视角验证图</summary>
                          <img
                            loading="lazy"
                            src={`/api/demo/selections/${entry.selection_id}/validation-image`}
                            alt={`${entry.request.text} 五视角选区验证；绿色为候选选区`}
                          />
                        </details>
                      )}
                    </>
                  ) : null}
                </article>
              );
            })}
          </div>
          <p className="demo-input-help">
            检查衡量与二维语言查询的一致性，不代表人工三维准确率。错误目标也可能保持一致，需目视确认。
          </p>
        </details>
      )}
    </section>
  );
}
