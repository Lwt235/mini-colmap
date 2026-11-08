import { useEffect, useState } from 'react';
import { api } from './api';
import LangSplatReferencePanel from './LangSplatReferencePanel';
import CodecStudyPanel from './CodecStudyPanel';

type SemanticStatus = {
  available: boolean;
  name?: string;
  config?: string;
  configs?: string[];
  prepared?: boolean;
  gpu_busy: boolean;
  offline_gpu_busy?: boolean;
  models?: { name: string; ready: boolean; size?: number }[];
  splits?: Record<string, number>;
  stages?: Record<string, { count?: number; checkpoint_exists?: boolean }>;
  exercises?: { id: string; name: string; status: string }[];
  previews?: { image_id: number; url: string }[];
  queries?: { image_id: number; text: string; mode: string; url: string }[];
  last_command?: { stage: string; status: string; message?: string };
};

const steps = [
  [
    '1. 区域与二维查询',
    '实现 L1、L6，检查原始照片上的物体查询。',
    '.venv/bin/mini-colmap semantic regions --split smoke',
  ],
  [
    '2. 场景特征压缩',
    '实现 L2、L3；仅训练语义 train 的区域特征。',
    '.venv/bin/mini-colmap semantic autoencoder',
  ],
  [
    '3. 三维融合基线',
    '实现 L7，比较简单融合与后续可学习语义场。',
    '.venv/bin/mini-colmap semantic fuse',
  ],
  [
    '4. 高斯语义训练',
    '实现 L4、L5；固定 RGB 高斯的几何、颜色和透明度。',
    '.venv/bin/mini-colmap semantic distill',
  ],
  [
    '5. 查询与评测',
    '先检查 dev，再固定配置评测 test。完整前置命令见流程文档。',
    '.venv/bin/mini-colmap semantic evaluate --split dev --mode distilled',
  ],
];

export default function SemanticPanel() {
  const [section, setSection] = useState<'reference' | 'codec' | 'learning'>('reference');
  return (
    <div className="research-sections">
      <nav aria-label="实验分类" className="demo-tabs">
        <button
          className={section === 'reference' ? 'selected' : ''}
          onClick={() => setSection('reference')}
        >
          三维模型对照
        </button>
        <button
          className={section === 'codec' ? 'selected' : ''}
          onClick={() => setSection('codec')}
        >
          编码压缩实验
        </button>
        <button
          className={section === 'learning' ? 'selected' : ''}
          onClick={() => setSection('learning')}
        >
          算法学习与数据
        </button>
      </nav>
      {section === 'reference' ? (
        <LangSplatReferencePanel />
      ) : section === 'codec' ? (
        <CodecStudyPanel />
      ) : (
        <LearningPanel />
      )}
    </div>
  );
}

function LearningPanel() {
  const [data, setData] = useState<SemanticStatus | null>(null);
  const [error, setError] = useState('');
  const [config, setConfig] = useState('');
  useEffect(() => {
    let active = true;
    const refresh = () =>
      api<SemanticStatus>(
        `/api/semantic/status${config ? `?config=${encodeURIComponent(config)}` : ''}`,
      )
        .then((value) => {
          if (active) {
            setData(value);
            setError('');
          }
        })
        .catch((reason) => {
          if (active) setError(String(reason));
        });
    void refresh();
    const timer = window.setInterval(refresh, 5000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [config]);
  return (
    <section className="semantic-panel" aria-label="语义学习框架">
      <div className="semantic-title">
        <div>
          <h2>三维语言场 · Waldo</h2>
          <p>使用现有 RGB 模型，逐步实现区域特征、语义训练与文本查询。</p>
        </div>
        <a href="/api/semantic/guide" target="_blank" rel="noreferrer">
          打开完整操作流程 ↗
        </a>
      </div>
      {error && <p role="alert">{error}</p>}
      {data && !data.available && <p>当前项目尚未配置语义实验，请先按文档准备。</p>}
      {!!data?.configs?.length && (
        <label>
          语义实验：
          <select
            value={config || data.config}
            onChange={(event) => {
              setData(null);
              setConfig(event.target.value);
            }}
          >
            {data.configs.map((name) => (
              <option key={name} value={name}>
                {name === 'semantic_waldo_repaired'
                  ? '压缩修复方案'
                  : name === 'semantic_waldo'
                    ? '原始 3 维基线'
                    : name}
              </option>
            ))}
          </select>
        </label>
      )}
      <div className="semantic-summary">
        <span>{data?.prepared ? '数据划分已准备' : '数据尚未准备'}</span>
        {data?.models?.map((model) => (
          <span key={model.name}>
            {model.name.toUpperCase()}：{model.ready ? '权重已就绪' : '尚未就绪'}
          </span>
        ))}
        <span>{data?.gpu_busy || data?.offline_gpu_busy ? 'GPU 正在使用' : 'GPU 空闲'}</span>
      </div>
      <p>
        语义 train / dev / test：{data?.splits?.train ?? '—'} / {data?.splits?.dev ?? '—'} /{' '}
        {data?.splits?.test ?? '—'} 张。标注帧不用于语言特征训练；现有 RGB/SfM 曾使用这些照片。
      </p>
      <div className="semantic-steps">
        {steps.map(([title, detail, command]) => (
          <article key={title}>
            <h3>{title}</h3>
            <p>{detail}</p>
            <code>
              {command}
              {data?.config && data.config !== 'semantic_waldo'
                ? ` --config configs/${data.config}.json`
                : ''}
            </code>
          </article>
        ))}
      </div>
      <h3>算法实现状态</h3>
      <p>以下状态来自代码中的 TODO 检查；“已填写”仍需通过验收测试。关键算法不会自动填充。</p>
      <div className="semantic-exercises">
        {data?.exercises?.map((exercise) => (
          <div key={exercise.name}>
            <strong>{exercise.id}</strong>
            <code>{exercise.name}</code>
            <span>{exercise.status === 'todo' ? '待实现' : '已填写 · 待验收'}</span>
          </div>
        ))}
      </div>
      {data?.last_command && (
        <p>
          最近命令：{data.last_command.stage} · {data.last_command.status}
          {data.last_command.message && ` · ${data.last_command.message}`}
        </p>
      )}
      <h3>已生成的数据</h3>
      <p>
        原始特征 {data?.stages?.raw?.count ?? 0} 帧 · 区域图 {data?.stages?.regions?.count ?? 0} 帧
        · 低维编码 {data?.stages?.codes?.count ?? 0} 帧
      </p>
      <p>下面显示 SAM 原始候选区域，用于检查模型与图片读取；它不是 L1 筛选结果或语义查询结果。</p>
      <div className="semantic-previews">
        {data?.previews?.map((item) => (
          <figure key={item.image_id}>
            <img src={item.url} alt={`相机 ${item.image_id} SAM 原始区域`} />
            <figcaption>相机 {item.image_id} · 原始 SAM 候选</figcaption>
          </figure>
        ))}
      </div>
      {!!data?.queries?.length && (
        <>
          <h3>实际文本查询结果</h3>
          <div className="semantic-previews">
            {data.queries.map((item) => (
              <figure key={item.url}>
                <img src={item.url} alt={item.text} />
                <figcaption>
                  {item.text} · {item.mode} · 相机 {item.image_id}
                </figcaption>
              </figure>
            ))}
          </div>
        </>
      )}
    </section>
  );
}
