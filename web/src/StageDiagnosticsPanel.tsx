import { useEffect, useState } from 'react';
import { api } from './api';

type Stage = {
  level: number;
  prediction_pixels: number;
  iou?: number;
  precision?: number;
  coverage?: number;
};
type Focus = {
  image_id: number;
  query: string;
  annotated: boolean;
  comparison_url: string;
  stages: Record<string, Stage>;
};
type Data = {
  available: boolean;
  results?: { focus: Focus[]; dev_summary: Record<string, { official: number; common: number }> };
  findings: { query: string; title: string; detail: string; next_step: string }[];
};
const names: Record<string, string> = {
  teacher: '原始二维特征',
  reconstructed: '压缩重建特征',
  field: '三维渲染特征',
  region_pooled: '区域平均对照',
};
export default function StageDiagnosticsPanel() {
  const [data, setData] = useState<Data | null>(null);
  const [key, setKey] = useState('124:dark cup');
  useEffect(() => {
    let active = true;
    void api<Data>('/api/semantic/stage-diagnostics')
      .then((v) => {
        if (active) setData(v);
      })
      .catch(() => {});
    return () => {
      active = false;
    };
  }, []);
  if (!data?.available || !data.results) return null;
  const selected =
    data.results.focus.find((r) => `${r.image_id}:${r.query}` === key) ?? data.results.focus[0];
  const finding = data.findings.find((r) => r.query === selected.query);
  const pct = (v: number | undefined) => (v === undefined ? '—' : `${(v * 100).toFixed(1)}%`);
  return (
    <section className="stage-diagnostics" aria-label="语义错误逐层诊断">
      <span className="demo-section-label">ERROR ANALYSIS</span>
      <h2>语义错误逐层诊断</h2>
      <p>
        固定当前模型、文本与评测流程，对比原始二维、压缩重建和三维渲染。此检查没有更新模型或降低定位门槛。
      </p>
      <label>
        检查目标与视角{' '}
        <select aria-label="逐层诊断视角" value={key} onChange={(e) => setKey(e.target.value)}>
          {data.results.focus.map((r) => (
            <option key={`${r.image_id}:${r.query}`} value={`${r.image_id}:${r.query}`}>
              {r.query} · 相机 {r.image_id} · {r.annotated ? 'Dev 标注' : '训练视角，无人工标注'}
            </option>
          ))}
        </select>
      </label>
      {finding && (
        <div className="diagnostic-finding">
          <strong>{finding.title}</strong>
          <p>{finding.detail}</p>
          <p>下一步：{finding.next_step}</p>
        </div>
      )}
      <div className="diagnostic-images">
        <figure>
          <img
            src={`/api/demo/images/${selected.image_id}`}
            alt={`相机 ${selected.image_id} 原始照片`}
            loading="lazy"
          />
          <figcaption>原始照片</figcaption>
        </figure>
        <figure>
          <img
            data-testid="stage-diagnostic-comparison"
            src={selected.comparison_url}
            alt={`${selected.query} 原始二维、压缩重建、三维渲染、区域平均四阶段并排对照`}
            loading="lazy"
          />
          <figcaption>
            左上：原始二维；右上：压缩重建；左下：三维渲染；右下：区域平均对照。绿色为预测，红边为人工标注。
          </figcaption>
        </figure>
      </div>
      {!selected.annotated && (
        <p>此视角没有人工标注，只展示区域大小与视觉对照，不报告准确率。候选可能出画或被遮挡。</p>
      )}
      <table>
        <thead>
          <tr>
            <th>阶段</th>
            <th>层级</th>
            <th>IoU</th>
            <th>预测内正确比例</th>
            <th>目标覆盖率</th>
            <th>选中像素</th>
          </tr>
        </thead>
        <tbody>
          {Object.entries(selected.stages).map(([stage, v]) => (
            <tr key={stage}>
              <td>{names[stage] ?? stage}</td>
              <td>L{v.level}</td>
              <td>{pct(v.iou)}</td>
              <td>{pct(v.precision)}</td>
              <td>{pct(v.coverage)}</td>
              <td>{v.prediction_pixels.toLocaleString()}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <details>
        <summary>两个推理对照与全部 Dev 查询</summary>
        <p>
          固定教师选择的层级，可改善刀具标注视角，但不能解决杯子退化。区域平均没有稳定改善，因此两个对照都没有替换正式策略。
        </p>
        <table>
          <thead>
            <tr>
              <th>阶段</th>
              <th>上游流程 mIoU</th>
              <th>原固定阈值 mIoU</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(data.results.dev_summary).map(([stage, v]) => (
              <tr key={stage}>
                <td>{names[stage] ?? stage}</td>
                <td>{pct(v.official)}</td>
                <td>{pct(v.common)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p>
          仅两张重复使用的 Dev 照片、11 个查询，不是独立泛化评测。SAM 的 GT
          上限只用于诊断，不用于生成或定位。
        </p>
      </details>
      <a
        href="/api/semantic/stage-diagnostics/artifacts/README.md"
        target="_blank"
        rel="noreferrer"
      >
        查看归因报告、可见性证据与复现步骤 ↗
      </a>
    </section>
  );
}
