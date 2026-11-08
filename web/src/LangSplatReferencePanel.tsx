import { useEffect, useState } from 'react';
import { api } from './api';
import FieldComparisonPanel from './FieldComparisonPanel';
import StageDiagnosticsPanel from './StageDiagnosticsPanel';
import MultiviewPanel from './MultiviewPanel';

type Metrics = { mean_iou: number; bbox_localization_rate?: number; polygon_peak_rate?: number };
type Protocol = {
  controller?: { status: string; stage?: string[]; log?: string };
  progress?: { stage: string; epoch?: number; iteration?: number; level?: number };
  models: { level: number; iteration: number; total: number; completed: boolean }[];
  metrics: Record<string, { official: Metrics; common: Metrics }>;
  images: { name: string; heatmap_url: string; mask_url: string }[];
  complete: boolean;
};
type Reference = {
  available: boolean;
  extraction?: { completed: number; total: number };
  protocols?: Record<string, Protocol>;
};

export default function LangSplatReferencePanel() {
  const [data, setData] = useState<Reference | null>(null);
  useEffect(() => {
    let active = true;
    const refresh = () =>
      api<Reference>('/api/semantic/reference')
        .then((value) => {
          if (active) setData(value);
        })
        .catch(() => {});
    void refresh();
    const timer = window.setInterval(refresh, 5000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, []);
  if (!data?.available) return null;
  return (
    <>
      <FieldComparisonPanel />
      <StageDiagnosticsPanel />
      <MultiviewPanel />
      <section aria-label="LangSplat 官方语义参考">
        <h3>LangSplat 官方语义参考</h3>
        <a href="/api/semantic/reference/guide" target="_blank" rel="noreferrer">
          查看实验说明与操作方法 ↗
        </a>
        <p>
          四层区域提取：{data.extraction?.completed} / {data.extraction?.total} 张。实验依次使用 GPU
          7，结果单独保存。
        </p>
        <p>
          匹配留出对照用 173 张训练图；全视角对照用 187 张图，包含评测图的 SAM/CLIP
          弱监督。全视角结果不能代表未见视角泛化。
        </p>
        {Object.entries(data.protocols ?? {}).map(([name, protocol]) => (
          <article key={name}>
            <h4>{name === 'matched' ? '匹配留出对照' : '全视角对照'}</h4>
            <p>
              {protocol.complete
                ? '已完成'
                : protocol.controller?.status === 'failed'
                  ? '阶段失败，请查看实验日志'
                  : protocol.controller?.status?.startsWith('waiting')
                    ? '等待前置阶段'
                    : protocol.progress?.stage === 'autoencoder'
                      ? `压缩器训练：${(protocol.progress.epoch ?? -1) + 1} / 100 轮`
                      : protocol.progress?.stage === 'evaluate'
                        ? '正在评估'
                        : '正在执行'}
            </p>
            <p>
              {protocol.models
                .map((model) => `层级 ${model.level}：${model.iteration} / ${model.total}`)
                .join(' · ')}
            </p>
            {protocol.controller?.status === 'failed' && protocol.controller.log && (
              <a
                href={`/api/semantic/reference/artifacts/${name === 'matched' ? '' : 'full-view-protocol/'}${encodeURIComponent(protocol.controller.log)}`}
                target="_blank"
                rel="noreferrer"
              >
                查看失败阶段日志
              </a>
            )}
            {Object.entries(protocol.metrics).map(([split, metric]) => (
              <p key={split}>
                {name === 'full_view'
                  ? split === 'dev'
                    ? '开发帧（参与弱监督）'
                    : '原测试帧（参与弱监督）'
                  : split === 'dev'
                    ? '开发集'
                    : '测试集'}
                ：官方口径 IoU {metric.official.mean_iou.toFixed(3)}，框内定位{' '}
                {((metric.official.bbox_localization_rate ?? 0) * 100).toFixed(1)}%； 当前口径 IoU{' '}
                {metric.common.mean_iou.toFixed(3)}。
              </p>
            ))}
            {!!protocol.images.length && (
              <div className="semantic-previews">
                {protocol.images.map((image) => (
                  <figure key={image.heatmap_url}>
                    <img
                      loading="lazy"
                      src={image.heatmap_url}
                      alt={`${image.name} 原始相关性热图`}
                    />
                    <img loading="lazy" src={image.mask_url} alt={`${image.name} 官方后处理掩码`} />
                    <figcaption>{image.name} · 上：原始相关性热图；下：官方后处理掩码</figcaption>
                  </figure>
                ))}
              </div>
            )}
          </article>
        ))}
      </section>
    </>
  );
}
