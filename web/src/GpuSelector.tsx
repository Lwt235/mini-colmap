import { useEffect, useState } from 'react';
import { api, json } from './api';
type Status = {
  selected: number;
  busy: boolean;
  devices: { index: number; name: string; free_mb: number; total_mb: number }[];
  message?: string;
};
export default function GpuSelector() {
  const [status, setStatus] = useState<Status | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    let alive = true;
    const load = () => {
      void api<Status>('/api/gpu')
        .then((v) => {
          if (alive) setStatus(v);
        })
        .catch(() => {});
    };
    load();
    const t = setInterval(load, 5000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);
  return (
    <label
      className="gpu-picker"
      title={
        error ||
        status?.message ||
        (status?.busy ? '任务运行时不能切换显卡' : '为后续训练、检索与实时渲染指定物理显卡')
      }
    >
      <span>显卡</span>
      <select
        aria-label="选择物理显卡"
        value={status?.selected ?? 7}
        disabled={!status || pending || status.busy || !status.devices.length}
        onChange={async (e) => {
          setPending(true);
          setError('');
          try {
            setStatus(await api<Status>('/api/gpu', json({ index: Number(e.target.value) })));
          } catch (e) {
            setError(String(e));
          } finally {
            setPending(false);
          }
        }}
      >
        {!status?.devices.some((d) => d.index === status.selected) && (
          <option value={status?.selected ?? 7}>
            GPU {status?.selected ?? 7}
            {status ? ' · 不可用' : ''}
          </option>
        )}
        {status?.devices.map((d) => (
          <option key={d.index} value={d.index}>
            GPU {d.index} · {d.name} · 空闲 {(d.free_mb / 1024).toFixed(1)} GB
          </option>
        ))}
      </select>
      {error && (
        <span className="gpu-error" role="alert">
          {error}
        </span>
      )}
    </label>
  );
}
