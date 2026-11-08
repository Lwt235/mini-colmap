import { useEffect, useState } from 'react';
import { ArrowRight, Box, Braces, Gauge, ScanSearch } from 'lucide-react';
import { api } from './api';
type Card = {
  title: string;
  selection_id: string;
  description: string;
  preview_url: string;
  selected_gaussians: number;
  anchor_image_id: number;
};
type Showcase = {
  ready: boolean;
  release: string;
  scope: string;
  selections: Card[];
  evidence_url: string;
};
export default function ShowcasePage() {
  const [data, setData] = useState<Showcase | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    let active = true;
    void api<Showcase>('/api/showcase')
      .then((d) => {
        if (active) setData(d);
      })
      .catch((e) => {
        if (active) setError(String(e));
      });
    return () => {
      active = false;
    };
  }, []);
  function openSelection(id: string) {
    try {
      sessionStorage.setItem('minicolmap.showcase-selection', id);
    } catch {
      setError('浏览器未允许保存演示入口，请从场景演示中查询 sink。');
      return;
    }
    window.location.hash = '#demo';
  }
  return (
    <main className="showcase-page">
      <section className="showcase-hero">
        <div>
          <span className="demo-eyebrow">3D VISION · GRAPHICS · SYSTEMS</span>
          <h1>
            从照片重建空间，
            <br />
            用语言定位物体。
          </h1>
          <p>
            Mini-COLMAP 将多视图几何、三维高斯渲染、语言特征与远程 GPU 工作台串成可检查的交互流程。
          </p>
          <div className="showcase-links">
            <a href="#demo">
              自由文本探索 <ArrowRight size={16} />
            </a>
            <a href="#workspace">
              重建与渲染 <ArrowRight size={16} />
            </a>
            <a href="#research">
              实验与证据 <ArrowRight size={16} />
            </a>
          </div>
        </div>
        <div className="showcase-story">
          <Box size={30} />
          <strong>照片 → 三维模型 → 语言交互</strong>
          <p>
            Linux GPU 计算
            <br />
            SSH 隧道访问
            <br />
            浏览器操作与独立结果导出
          </p>
          <small>{data?.release ?? '演示准备中'}</small>
        </div>
      </section>
      <section className="showcase-capabilities" aria-label="项目能力">
        <article>
          <Box />
          <h2>几何重建</h2>
          <p>SfM、相机与稀疏点云检查，NeRF / 3DGS 训练与结果管理。</p>
        </article>
        <article>
          <ScanSearch />
          <h2>语言定位</h2>
          <p>粗搜与细搜相机、多视角验证、选区高亮与隔离；证据不足时保留原因。</p>
        </article>
        <article>
          <Gauge />
          <h2>交互性能</h2>
          <p>融合渲染、最新帧调度、交互时降采样，停止操作后恢复清晰画面。</p>
        </article>
      </section>
      <section className="showcase-curated">
        <span className="demo-section-label">VERIFIED WALKTHROUGH</span>
        <h2>已验证的交互案例</h2>
        <p>这里使用已验证并缓存的选区，可直接开始演示。自由文本搜索保留在“场景演示”中。</p>
        {error && <p role="alert">{error}</p>}
        <div className="showcase-case-grid">
          {data?.selections.map((card) => (
            <article key={card.selection_id}>
              <img
                src={card.preview_url}
                alt={`${card.title} · 相机 ${card.anchor_image_id} 真实三维渲染预览`}
              />
              <div>
                <h3>{card.title}</h3>
                <p>{card.description}</p>
                <small>
                  真实三维渲染预览 · {card.selected_gaussians.toLocaleString()} 个选中高斯
                </small>
                <button
                  data-testid="showcase-locate"
                  onClick={() => openSelection(card.selection_id)}
                >
                  进入三维高亮 <ArrowRight size={15} />
                </button>
              </div>
            </article>
          ))}
          <article className="showcase-evidence-card">
            <Braces size={24} />
            <h3>从失败案例解释方法</h3>
            <p>对照二维教师、压缩重建和三维渲染；查看目标出画、误选背景和杯体破碎的证据。</p>
            <a href="#research">
              打开逐层诊断 <ArrowRight size={15} />
            </a>
          </article>
        </div>
        {data && !data.ready && <p>当前模型尚未通过案例校验。可进入场景演示或实验记录检查状态。</p>}
      </section>
      <section className="showcase-measurements">
        <span className="demo-section-label">MEASURED, WITH CONTEXT</span>
        <h2>可追溯的结果</h2>
        <div>
          <article>
            <strong>47 / 47</strong>
            <span>TempleRing 相机注册</span>
            <small>640×480；6,570 点；平均重投影误差 0.398 px</small>
          </article>
          <article>
            <strong>35.87 → 42.99%</strong>
            <span>语义 Dev mIoU</span>
            <small>固定 RGB、11 个 Dev 查询、上游后处理；其他口径有退步</small>
          </article>
          <article>
            <strong>9.65 → 3.91 ms</strong>
            <span>本机 720p GPU 渲染</span>
            <small>同一模型；不包含隧道、解码与显示耗时</small>
          </article>
        </div>
        <a href={data?.evidence_url ?? '/api/showcase/guide'} target="_blank" rel="noreferrer">
          指标来源、运行步骤与项目边界 ↗
        </a>
      </section>
      <footer className="showcase-footer">
        <p>{data?.scope}</p>
        <span>
          复用 OpenCV、gsplat、SAM、CLIP 与固定 LangSplat
          组件；自研工作集中在流程、数学实现、实验、交互与工程集成。
        </span>
      </footer>
    </main>
  );
}
