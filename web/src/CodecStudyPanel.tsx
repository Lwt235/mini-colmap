import { useEffect, useState } from 'react';
import { api } from './api';

type Metric = {
  results: { common: { mean_iou: number }; official: { mean_iou: number } };
  reconstruction: { mean_cosine: number };
};
type Study = {
  available: boolean;
  complete?: boolean;
  progress?: { stage: string; variant: string; epoch?: number; epochs?: number };
  variants?: { name: string; dim: number; trained: boolean; metrics: Record<string, Metric> }[];
  selection?: { best_overall: string; best_native3: string };
  images?: { name: string; heatmap_url: string; mask_url: string }[];
  has_report?: boolean;
};

const names: Record<string, string> = {
  stock3: '官方配方重训',
  row_cos001: '逐区域余弦',
  row_cos1: '余弦权重 1',
  no_latent_norm: '取消编码单位化',
  no_output_norm: '取消训练输出单位化',
  row_dim8: '逐区域损失 · 8维',
  row_dim16: '逐区域损失 · 16维',
  stock_dim16: '官方损失 · 16维',
};

export default function CodecStudyPanel() {
  const [study, setStudy] = useState<Study | null>(null);
  useEffect(() => {
    let active = true;
    const refresh = () =>
      api<Study>('/api/semantic/codec-study')
        .then((value) => {
          if (active) setStudy(value);
        })
        .catch(() => {});
    void refresh();
    const timer = window.setInterval(refresh, 5000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, []);
  if (!study?.available) return null;
  const progress = study.progress;
  return (
    <section aria-label="压缩器单因素诊断">
      <h3>压缩器单因素诊断</h3>
      <p>固定 173 张训练图、相同区域和训练预算。这里只比较二维压缩重建查询，三维语言场保持原样。</p>
      <p>
        {study.complete
          ? '全部完成'
          : progress?.stage === 'fit'
            ? `${names[progress.variant] ?? progress.variant}：${progress.epoch} / ${progress.epochs} 轮`
            : '正在评估与记录结果'}
      </p>
      <a href="/api/semantic/codec-study/guide" target="_blank" rel="noreferrer">
        查看实验与使用说明 ↗
      </a>
      {study.has_report && (
        <p>
          <a href="/api/semantic/codec-study/artifacts/README.md" target="_blank" rel="noreferrer">
            查看实际结果报告 ↗
          </a>
        </p>
      )}
      <table>
        <thead>
          <tr>
            <th>方案</th>
            <th>维数</th>
            <th>状态</th>
            <th>开发集 IoU（官方口径）</th>
            <th>开发集 IoU（原口径）</th>
            <th>区域余弦</th>
          </tr>
        </thead>
        <tbody>
          {study.variants?.map((variant) => {
            const dev = variant.metrics.dev;
            return (
              <tr key={variant.name}>
                <td>{names[variant.name] ?? variant.name}</td>
                <td>{variant.dim}</td>
                <td>
                  {dev
                    ? '已评估'
                    : variant.trained
                      ? '已训练'
                      : progress?.variant === variant.name
                        ? '训练中'
                        : '等待'}
                </td>
                <td>{dev ? `${(dev.results.official.mean_iou * 100).toFixed(2)}%` : '—'}</td>
                <td>{dev ? `${(dev.results.common.mean_iou * 100).toFixed(2)}%` : '—'}</td>
                <td>{dev ? dev.reconstruction.mean_cosine.toFixed(4) : '—'}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {study.selection && (
        <p>
          开发集候选：{names[study.selection.best_overall]}；保持官方三维表示约束的候选：
          {names[study.selection.best_native3]}。新压缩器需要重新训练对应语言场，才能用于三维查询。
        </p>
      )}
      {!!study.images?.length && (
        <div className="semantic-previews">
          {study.images.map((item) => (
            <figure key={item.heatmap_url}>
              <img loading="lazy" src={item.heatmap_url} alt={`${item.name} 热图`} />
              <img loading="lazy" src={item.mask_url} alt={`${item.name} 掩码`} />
              <figcaption>{item.name} · 上：热图；下：掩码</figcaption>
            </figure>
          ))}
        </div>
      )}
    </section>
  );
}
