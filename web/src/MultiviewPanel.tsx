import { useEffect, useState } from 'react';
import { api } from './api';
type Row = {
  query: string;
  image_id: number;
  anchor_id: number;
  level: number;
  adaptive: { reprojection_iou: number | null; anchor_foreground_rays: number };
};
export default function MultiviewPanel() {
  const [rows, setRows] = useState<Row[]>([]);
  useEffect(() => {
    let active = true;
    void api<{ results?: { per_view: Row[] } }>('/api/semantic/multiview')
      .then((data) => {
        if (active) setRows(data.results?.per_view ?? []);
      })
      .catch(() => {});
    return () => {
      active = false;
    };
  }, []);
  if (!rows.length) return null;
  return (
    <section className="field-comparison">
      <span className="demo-section-label">MULTI-VIEW AUDIT</span>
      <h2>跨视角查询一致性</h2>
      <p>
        每个目标检查六个相机视角。几何重投影一致性用于诊断选区漂移，不等于人工标注准确率；不可见目标单独列为样本不足。
      </p>
      {['sink', 'dark cup', 'knife'].map((text) => {
        const selected = rows.filter((r) => r.query === text && r.image_id !== r.anchor_id);
        const values = selected.flatMap((r) =>
          r.adaptive.reprojection_iou === null ? [] : [r.adaptive.reprojection_iou],
        );
        return (
          <details key={text} className="multiview-object">
            <summary>
              <strong>{text}</strong>
              <span>
                {text === 'sink'
                  ? '较稳定，可优先演示'
                  : text === 'dark cup'
                    ? '杯体选区破碎，跨视角仍不稳'
                    : '部分视角误选墙面和台面，暂不宜自动选取'}
              </span>
            </summary>
            <p>
              有效对照 {values.length} / {selected.length} 个；重投影 IoU 范围{' '}
              {values.length
                ? `${Math.min(...values).toFixed(3)}–${Math.max(...values).toFixed(3)}`
                : '样本不足'}
              。
            </p>
            <img
              src={`/api/semantic/multiview/artifacts/${text.replaceAll(' ', '_')}-views.jpg`}
              alt={`${text} 六视角查询检查拼图`}
              loading="lazy"
            />
          </details>
        );
      })}
      <a href="/api/semantic/multiview/artifacts/README.md" target="_blank" rel="noreferrer">
        完整检查数据与方法 ↗
      </a>
    </section>
  );
}
