export async function api<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) {
    let message = `请求失败 (${response.status})`;
    try {
      const body = await response.json();
      message = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail ?? body);
    } catch {
      /* HTTP error without JSON */
    }
    throw new Error(message);
  }
  return response.json() as Promise<T>;
}
export const json = (value: unknown): RequestInit => ({
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(value),
});
export function downloadJSON(name: string, value: unknown) {
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(value, null, 2)], { type: 'application/json' }),
  );
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
export const number = (value: number | null | undefined, digits = 3) =>
  value == null || !Number.isFinite(value)
    ? '—'
    : value.toLocaleString('en-US', { maximumFractionDigits: digits });
export const stageName = (stage: string) =>
  ({
    loading: '读取数据',
    dataset_loading: '数据加载与准备',
    features: '提取特征',
    feature_extraction: '提取特征',
    matching: '特征匹配',
    pair_geometry: '匹配与几何验证',
    geometry: '几何验证',
    tracks: '构建轨迹',
    initialization: '两视图初始化',
    mapping: '增量建图',
    incremental_mapping: '增量建图',
    registration: '注册相机',
    bundle_adjustment: '束调整 BA',
    ba: '束调整 BA',
    export: '导出结果',
    exporting: '导出结果',
    done: '处理完成',
    starting: '正在启动',
    queued: '等待启动',
    preparing: '准备训练数据',
    training: '训练',
    evaluation: '验证与渲染',
    todo: '运行未完成',
  })[stage] ?? stage;
export function snapshotName(label: string) {
  const registration = /^registered_(\d+)$/.exec(label);
  if (registration) return `注册图像 #${registration[1]}`;
  return (
    {
      initialization: '初始化',
      incremental_complete: '增量建图完成',
      before_ba: 'BA 前',
      after_ba: 'BA 后',
      final: '最终结果',
    }[label] ?? label
  );
}
