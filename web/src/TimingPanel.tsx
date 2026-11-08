import { ChevronDown, Clock3 } from 'lucide-react';
import { number, stageName } from './api';
import type { TimingReport } from './types';

export default function TimingPanel({ timing }: { timing: TimingReport }) {
  return (
    <details className="timing-panel" data-testid="pipeline-timings" open>
      <summary>
        <Clock3 size={14} /> Pipeline 计时 <strong>{number(timing.total_seconds, 3)} 秒</strong>
        <ChevronDown size={12} />
      </summary>
      <table>
        <thead>
          <tr>
            <th>阶段</th>
            <th>耗时 / 秒</th>
            <th>占总耗时</th>
          </tr>
        </thead>
        <tbody>
          {Object.entries(timing.stages_seconds).map(([name, seconds]) => (
            <tr key={name}>
              <td>{stageName(name)}</td>
              <td>{number(seconds, 3)}</td>
              <td>
                {timing.total_seconds > 0
                  ? `${number((seconds / timing.total_seconds) * 100, 1)}%`
                  : '—'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="timing-details">
        {Object.entries(timing.details).map(([name, item]) => (
          <span key={name}>
            {name === 'descriptor_matching'
              ? '描述子匹配'
              : name === 'fundamental_ransac'
                ? 'F-RANSAC'
                : name}
            ：{number(item.seconds, 3)} 秒 / {item.calls} 次
          </span>
        ))}
      </div>
      <p>墙钟时间，包含该阶段的文件读写与快照开销；匹配子项已包含在阶段总耗时中，不重复相加。</p>
      {timing.status !== 'completed' && (
        <p>
          {timing.status === 'failed'
            ? '任务失败，以上仅统计已执行部分。'
            : '以上为最近一次阶段检查点，不代表完整运行耗时。'}
        </p>
      )}
    </details>
  );
}
