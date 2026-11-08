import { useEffect, useState } from 'react';
import { api } from './api';

type Metrics = { common: { mean_iou: number }; official: { mean_iou: number } };
type Comparison = Record<string, Record<string, Metrics>>;
type Model = {
  id: string;
  name: string;
  ready: boolean;
  evaluation_ready: boolean;
  dev_iou: number | null;
  levels: { iteration: number; ready: boolean }[];
  controller?: { status: string; stage?: string[]; log?: string };
};

export default function FieldComparisonPanel() {
  const [models, setModels] = useState<Model[]>([]);
  const [comparison, setComparison] = useState<Comparison | null>(null);
  useEffect(() => {
    let active = true;
    const refresh = () => {
      void api<{ models: Model[] }>('/api/demo')
        .then((data) => {
          if (active) setModels(data.models);
        })
        .catch(() => {});
      void api<Comparison>('/api/demo/artifacts/raw3/comparison.json')
        .then((data) => {
          if (active) setComparison(data);
        })
        .catch(() => {});
    };
    refresh();
    const timer = window.setInterval(refresh, 5000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, []);
  return (
    <section className="field-comparison">
      <span className="demo-section-label">CURRENT EXPERIMENT</span>
      <h2>新编码与三维语言场验证</h2>
      <p>
        同一冻结 RGB、173 张语言训练视图、每层 30,000 步。新模型使用 no_latent_norm
        编码，并取消逐高斯特征归一化。
      </p>
      <div className="field-progress-grid">
        {models.map((model) => (
          <article key={model.id}>
            <strong>{model.name}</strong>
            <span>
              {model.controller?.status === 'failed'
                ? `失败：${model.controller.stage?.join(' ')}`
                : model.evaluation_ready
                  ? 'Dev 评测已完成'
                  : model.ready
                    ? '训练已完成，评测中'
                    : '三层特征场训练中'}
            </span>
            <progress max={90000} value={model.levels.reduce((sum, l) => sum + l.iteration, 0)} />
            <small>
              {model.levels.map((l, i) => `L${i + 1}: ${l.iteration.toLocaleString()}`).join(' / ')}
            </small>
          </article>
        ))}
      </div>
      {comparison ? (
        <table>
          <thead>
            <tr>
              <th>划分</th>
              <th>模型</th>
              <th>固定阈值 IoU</th>
              <th>官方评测流程 IoU</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(comparison).flatMap(([split, variants]) =>
              Object.entries(variants).map(([name, metrics]) => (
                <tr key={split + name}>
                  <td>{split}</td>
                  <td>{name === 'raw3' ? '新编码模型' : '官方参考模型'}</td>
                  <td>{(100 * metrics.common.mean_iou).toFixed(2)}%</td>
                  <td>{(100 * metrics.official.mean_iou).toFixed(2)}%</td>
                </tr>
              )),
            )}
          </tbody>
        </table>
      ) : (
        <p>完成训练与 dev/test 评测后，这里将显示真实三维指标。二维编码指标不能替代三维结果。</p>
      )}
      <a href="/api/demo/guide" target="_blank" rel="noreferrer">
        工作台使用与实验流程 ↗
      </a>
    </section>
  );
}
