import { useEffect, useState } from 'react';
import { Camera, FolderOpen, ImagePlus, LoaderCircle, Upload, X } from 'lucide-react';
import { api } from './api';
import type { Dataset } from './types';
export default function ImportDialog({
  onClose,
  onImported,
}: {
  onClose: () => void;
  onImported: (d: Dataset) => void;
}) {
  const [mode, setMode] = useState<'manifest' | 'photos'>('manifest'),
    [files, setFiles] = useState<File[]>([]),
    [manifestPath, setManifestPath] = useState(''),
    [K, setK] = useState({ fx: '', fy: '', cx: '', cy: '' }),
    [busy, setBusy] = useState(false),
    [error, setError] = useState('');
  const manifests = files.filter((f) => /\.json$/i.test(f.name));
  useEffect(() => {
    const preferred =
      files.find((f) => f.name === 'dataset.json') ?? files.find((f) => /\.json$/i.test(f.name));
    setManifestPath(preferred?.webkitRelativePath || preferred?.name || '');
  }, [files]);
  async function submit() {
    setBusy(true);
    setError('');
    try {
      if (!files.length) throw Error('请先选择数据目录或照片。');
      const data = new FormData();
      data.append('mode', mode);
      for (const f of files) {
        data.append('files', f);
        data.append('paths', f.webkitRelativePath || f.name);
      }
      if (mode === 'manifest') {
        if (!manifestPath) throw Error('所选目录中未找到 JSON 数据清单。');
        data.append('manifest_path', manifestPath);
      }
      if (mode === 'photos') {
        if (Object.values(K).some((v) => v.trim() === '' || !Number.isFinite(+v)))
          throw Error('请填写全部四个内参值。');
        if (+K.fx <= 0 || +K.fy <= 0) throw Error('fx 和 fy 必须大于 0。');
        data.append(
          'camera',
          JSON.stringify(Object.fromEntries(Object.entries(K).map(([k, v]) => [k, +v]))),
        );
      }
      const d = await api<Dataset>('/api/datasets/import', { method: 'POST', body: data });
      onImported(d);
      onClose();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div
      className="modal-backdrop"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget && !busy) onClose();
      }}
    >
      <section
        className="modal import-modal"
        role="dialog"
        aria-modal="true"
        aria-label="导入重建数据"
      >
        <div className="modal-heading">
          <div>
            <span className="eyebrow">DATA IMPORT</span>
            <h2>导入重建数据</h2>
          </div>
          <button
            className="icon-button"
            aria-label="关闭导入窗口"
            disabled={busy}
            onClick={onClose}
          >
            <X size={18} />
          </button>
        </div>
        <div className="segmented">
          <button
            className={mode === 'manifest' ? 'selected' : ''}
            onClick={() => {
              setMode('manifest');
              setFiles([]);
              setError('');
            }}
          >
            <FolderOpen size={15} />
            已有数据集
          </button>
          <button
            className={mode === 'photos' ? 'selected' : ''}
            onClick={() => {
              setMode('photos');
              setFiles([]);
              setError('');
            }}
          >
            <ImagePlus size={15} />
            照片 + 相机内参
          </button>
        </div>
        <p className="muted">
          {mode === 'manifest'
            ? '选择包含数据清单与图像文件的完整目录，保留清单中的相机内参和位姿。'
            : '导入同一台相机拍摄、已去畸变且尺寸一致的照片，并填写对应的真实内参。'}
        </p>
        <div className="upload-zone">
          <Upload size={28} />
          <strong>{files.length ? `已选择 ${files.length} 个文件` : '选择本地数据文件'}</strong>
          <span>
            {files.length
              ? files
                  .slice(0, 3)
                  .map((f) => f.name)
                  .join(' · ')
              : '照片将上传到当前工作台主机的项目目录'}
          </span>
          <div className="upload-actions">
            <label className="button outline">
              <FolderOpen size={15} />
              选择文件夹
              <input
                type="file"
                multiple
                hidden
                {...{ webkitdirectory: '' }}
                onChange={(e) => {
                  setFiles(
                    Array.from(e.target.files ?? []).filter(
                      (f) =>
                        mode === 'manifest' || /\.(png|jpg|jpeg|bmp|tif|tiff|webp)$/i.test(f.name),
                    ),
                  );
                  setError('');
                }}
              />
            </label>
            {mode === 'photos' && (
              <label className="button outline">
                <ImagePlus size={15} />
                选择照片
                <input
                  type="file"
                  accept="image/*"
                  multiple
                  hidden
                  onChange={(e) => {
                    setFiles(Array.from(e.target.files ?? []));
                    setError('');
                  }}
                />
              </label>
            )}
          </div>
        </div>
        {mode === 'manifest' && manifests.length > 0 && (
          <label className="manifest-select">
            数据清单
            <select
              aria-label="选择导入的数据清单"
              value={manifestPath}
              onChange={(e) => setManifestPath(e.target.value)}
            >
              {manifests.map((f) => (
                <option key={f.webkitRelativePath || f.name} value={f.webkitRelativePath || f.name}>
                  {f.webkitRelativePath || f.name}
                </option>
              ))}
            </select>
          </label>
        )}
        {mode === 'photos' && (
          <div className="intrinsics">
            <div className="section-label">
              <Camera size={14} />
              共享相机内参 <span>单位 px</span>
            </div>
            <div className="intrinsics-grid">
              {(['fx', 'fy', 'cx', 'cy'] as const).map((key) => (
                <label key={key}>
                  {key}
                  <input
                    type="number"
                    step="any"
                    placeholder={key === 'fx' || key === 'fy' ? '焦距' : '主点坐标'}
                    value={K[key]}
                    onChange={(e) => setK({ ...K, [key]: e.target.value })}
                  />
                </label>
              ))}
            </div>
            <p className="form-note">
              图像尺寸自动读取并校验。此入口仅添加内参，不生成位姿；NeRF
              训练前需估计或导入真实相机位姿。此界面不会猜测焦距，也不会自动去畸变。
            </p>
          </div>
        )}
        {error && <div className="inline-error">{error}</div>}
        <div className="modal-footer">
          <button className="quiet" disabled={busy} onClick={onClose}>
            取消
          </button>
          <button
            className="primary"
            disabled={busy || !files.length}
            onClick={() => void submit()}
          >
            {busy ? <LoaderCircle size={15} className="spin" /> : <Upload size={15} />}校验并导入
          </button>
        </div>
      </section>
    </div>
  );
}
