"""FastAPI routes for the local-only Mini-COLMAP workbench."""
from __future__ import annotations

import asyncio
import io
import json
import time
import uuid
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path, PureWindowsPath
from urllib.parse import quote

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ..config import resolve_config
from ..export import write_colmap_text, write_ply
from ..io import load_dataset, load_reconstruction, save_reconstruction
from ..metrics import reconstruction_summary
from ..models import Camera
from .scene import make_scene, point_details
from .store import Store, read_json, stable_id
from .worker import atomic_json


def safe_relative(raw: str) -> Path:
    """Uploaded relative paths use either Windows or browser POSIX separators."""
    windows = PureWindowsPath(raw)
    path = Path(raw.replace("\\", "/"))
    if (not raw or "\x00" in raw or windows.drive or windows.root or path.is_absolute()
            or any(part in ("..", ".") for part in path.parts) or not path.parts):
        raise ValueError("文件路径必须是目录内的相对路径")
    return path


def validate_dimensions(manifest: Path):
    dataset = load_dataset(manifest)
    if len(dataset["images"]) < 2:
        raise ValueError("重建至少需要两张图像")
    for image_id, path in dataset["image_paths"].items():
        pixels = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
        if pixels is None:
            raise ValueError(f"图像无法解码：{path.name}")
        camera = dataset["cameras"][dataset["images"][image_id].camera_id]
        if pixels.shape[:2] != (camera.height, camera.width):
            raise ValueError(f"图像尺寸与内参不匹配：{path.name}，应为 {camera.width}×{camera.height}")
    return dataset


def artifact_paths(output: Path):
    allowed = {".json", ".ply", ".txt", ".png", ".npz", ".pt", ".log"}
    if not output.exists():
        return {}
    return {path.relative_to(output).as_posix(): path for path in output.rglob("*")
            if path.is_file() and path.suffix.lower() in allowed
            and "ui" not in path.relative_to(output).parts
            and path.resolve().is_relative_to(output.resolve())}


def create_app(project_root: Path | None = None):
    root = Path(project_root).resolve() if project_root is not None else Path.cwd().resolve()
    store = Store(root)
    from .live import LiveSessions
    live = LiveSessions(store)
    from .demo import DemoQueries
    demo_queries = DemoQueries(root, lambda: live.active is not None or any(r['status']=='running' for r in store.list_runs()))
    from .selection_jobs import SelectionJobs
    selection_jobs = SelectionJobs(root, demo_queries.busy)
    from .auto_locate import AutoLocateJobs
    auto_locate_jobs = AutoLocateJobs(root, demo_queries.busy)

    from .scenes import SceneJobs
    scene_jobs = SceneJobs(root, store, live)

    @asynccontextmanager
    async def lifespan(app):
        yield
        scene_jobs.close()
        demo_queries.close()
        selection_jobs.close()
        auto_locate_jobs.close()
        live.close()
        store.close()

    app = FastAPI(title="Mini-COLMAP 本地工作台", lifespan=lifespan)
    app.state.store = store
    app.state.live = live
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    @app.middleware("http")
    async def local_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            # Reject cross-origin POSTs, including simple multipart requests.
            return JSONResponse({"detail": "只允许工作台同源请求"}, status_code=403)
        return await call_next(request)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({"detail": str(exc).strip("'\"")}, status_code=404)

    @app.exception_handler(OSError)
    async def unavailable(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    def gpu_busy():
        from .gpu_lease import active_lease
        return bool(live.active is not None or active_lease(root) or any(r['status']=='running' for r in store.list_runs()))

    @app.get('/api/gpu')
    def gpu_inventory():
        from .gpu import inventory
        return {**inventory(root),'busy':gpu_busy()}

    @app.post('/api/gpu')
    async def choose_gpu(request: Request):
        from .gpu import select_gpu
        value=await request.json()
        if not isinstance(value,dict) or set(value)!={'index'}:raise ValueError('请选择显卡编号')
        with store.lock:
            if gpu_busy():raise HTTPException(409,'任务运行或实时渲染时不能切换显卡')
            return {**select_gpu(root,value['index']),'busy':False}

    @app.get('/api/runs/{run_id}/deletion')
    def deletion_preview(run_id: str):
        from .deletion import plan
        with store.lock:return plan(store,run_id,live)

    @app.delete('/api/runs/{run_id}')
    async def delete_run(run_id: str,request: Request):
        from .deletion import delete
        value=await request.json()
        if not isinstance(value,dict) or set(value)!={'token'}:raise ValueError('请先确认文件清理范围')
        try:result=delete(store,run_id,value['token'],live)
        except RuntimeError as exc:raise HTTPException(409,str(exc)) from exc
        for job_id in result['scene_job_ids']:
            scene_jobs.processes.pop(job_id,None);scene_jobs.tokens.pop(job_id,None)
        return result

    @app.get("/api/bootstrap")
    def bootstrap():
        return store.bootstrap()

    @app.get('/api/demo')
    def demo_catalogue():
        from .demo import catalogue
        demo_queries.reap()
        result = catalogue(root)
        from ..semantics.selection import catalogue as selections
        result['selections']=selections(root)
        result['selection_jobs']=selection_jobs.history()
        result['auto_locate_jobs']=auto_locate_jobs.history()
        rgb_config = read_json(root/'configs/semantic_waldo.json',{})
        rgb_path = (root/rgb_config.get('rgb_run','__missing__')).resolve()
        result['rgb_run_id'] = next((r['id'] for r in store.list_runs() if Path(r.get('output_path','')).resolve()==rgb_path),None)
        result['offline_gpu_busy'] = result['gpu_busy']
        result['gpu_busy'] = result['gpu_busy'] or demo_queries.busy() or bool(demo_queries.process and demo_queries.process.poll() is None)
        return result

    @app.get('/api/scenes')
    def scenes_catalogue():
        return {'scenes': scene_jobs.catalogue()}

    @app.post('/api/scenes')
    async def create_scene(request: Request):
        value=await request.json()
        if not isinstance(value,dict) or not {'rgb_run_id'} <= set(value) or set(value)-{'rgb_run_id','name'} or not isinstance(value['rgb_run_id'],str):
            raise ValueError('请选择已完成的 3DGS 模型')
        return scene_jobs.create(value['rgb_run_id'],value.get('name'))

    @app.post('/api/scenes/{scene_id}/{action}')
    async def start_scene_job(scene_id: str, action: str, request: Request):
        value=await request.json()
        if not isinstance(value,dict) or set(value) != ({'text'} if action=='query' else set()):
            raise ValueError('无效的场景处理请求')
        try:return scene_jobs.start(scene_id,action,value.get('text'))
        except RuntimeError as exc:raise HTTPException(409,str(exc)) from exc

    @app.get('/api/scene-jobs/{job_id}')
    def scene_job(job_id: str):return scene_jobs.job(job_id)

    @app.post('/api/scene-jobs/{job_id}/cancel')
    def cancel_scene_job(job_id: str):return scene_jobs.cancel(job_id)

    @app.get('/api/scenes/{scene_id}/photo')
    def scene_photo(scene_id: str):
        record,_=scene_jobs.scene(scene_id)
        run=store.get(record['rgb_run_id']);matched=store.image_dataset(run)
        if not matched:raise HTTPException(404,'场景照片不可用')
        return FileResponse(matched[1]['image_paths'][record['anchor_image_id']])

    @app.get('/api/scenes/{scene_id}/artifacts/{relative:path}')
    def scene_artifact(scene_id: str,relative: str):
        _,folder=scene_jobs.scene(scene_id)
        path=(folder/safe_relative(relative)).resolve()
        if not path.is_relative_to(folder) or path.suffix!='.png' or not path.is_file():
            raise HTTPException(404,'图片不存在')
        return FileResponse(path)

    @app.get('/api/scenes/{scene_id}/selections/{selection_id}/export')
    def scene_export(scene_id: str, selection_id: str):
        from ..semantics.selection import load_selection,selection_directory
        scene_jobs.scene(scene_id)
        metadata,_=load_selection(root,selection_id)
        if metadata.get('source_scene')!=scene_id:raise HTTPException(404,'选区不属于此场景')
        directory=selection_directory(root,selection_id).resolve();path=(directory/'selection.zip').resolve()
        if not path.is_relative_to(directory) or not path.is_file():raise HTTPException(404,'选区导出不可用')
        return FileResponse(path,filename=f'{scene_id}-selection.zip')

    @app.get('/api/showcase')
    def project_showcase():
        from .showcase import catalogue
        return catalogue(root)

    @app.get('/api/showcase/guide')
    def project_showcase_guide():
        return FileResponse(root/'docs/evaluation.md',media_type='text/plain; charset=utf-8')

    @app.get('/api/showcase/preview/{case_id}')
    def project_showcase_preview(case_id: str):
        from .demo import read
        row=next((r for r in read(root/'configs/showcase.json',{}).get('selections',[]) if r.get('id')==case_id),None)
        if not row or not isinstance(row.get('preview_file'),str):raise HTTPException(404,'演示预览不存在')
        base=(root/'docs/assets').resolve();path=(base/row['preview_file']).resolve()
        if not path.is_relative_to(base) or path.suffix.lower() not in ('.jpg','.png') or not path.is_file():raise HTTPException(404,'演示预览不可用')
        return FileResponse(path)

    @app.post('/api/demo/auto-locate')
    async def auto_locate(request: Request):
        try:payload=await request.json()
        except ValueError as exc:raise HTTPException(400,'无效的自动定位请求') from exc
        try:return auto_locate_jobs.submit(payload)
        except RuntimeError as exc:raise HTTPException(409,str(exc)) from exc

    @app.get('/api/demo/auto-locate/{job_id}')
    def auto_locate_job(job_id: str):
        return auto_locate_jobs.job(job_id)

    @app.post('/api/demo/auto-locate/{job_id}/choose')
    async def auto_locate_choose(job_id: str,request: Request):
        try:payload=await request.json()
        except ValueError as exc:raise HTTPException(400,'无效的候选请求') from exc
        try:return auto_locate_jobs.choose(job_id,payload)
        except RuntimeError as exc:raise HTTPException(409,str(exc)) from exc

    @app.get('/api/demo/auto-locate/{job_id}/report')
    def auto_locate_report(job_id: str):
        auto_locate_jobs.job(job_id)
        path=root/'data/auto_locate'/job_id/'README.md'
        if not path.is_file():raise HTTPException(404,'自动定位报告尚未完成')
        return FileResponse(path,media_type='text/plain; charset=utf-8')

    @app.get('/api/demo/auto-locate/{job_id}/images/{candidate_id}')
    def auto_locate_image(job_id: str,candidate_id: str):
        value=auto_locate_jobs.job(job_id)
        if candidate_id not in {c['id'] for c in value.get('candidates',[])}:raise HTTPException(404,'候选图不存在')
        # IDs originate in worker data, and still require path containment.
        directory=(root/'data/auto_locate'/job_id).resolve()
        path=(directory/f'{candidate_id}.jpg').resolve()
        if not path.is_relative_to(directory) or not path.is_file():raise HTTPException(404,'候选图不存在')
        return FileResponse(path)

    @app.post('/api/demo/query')
    async def demo_query(request: Request):
        try:
            payload = await request.json()
        except ValueError as exc:
            raise HTTPException(400,'无效的查询请求') from exc
        if not isinstance(payload,dict):
            raise HTTPException(400,'查询请求必须是对象')
        try:
            return demo_queries.submit(payload)
        except RuntimeError as exc:
            raise HTTPException(409,str(exc)) from exc

    @app.get('/api/demo/jobs/{job_id}')
    def demo_job(job_id: str):
        return demo_queries.job(job_id)

    @app.get('/api/demo/guide')
    def demo_guide():
        return FileResponse(root/'docs/usage.md',media_type='text/plain; charset=utf-8')

    @app.get('/api/demo/selections/{selection_id}/export')
    def selection_export(selection_id: str):
        from ..semantics.selection import selection_directory,load_selection
        load_selection(root,selection_id)
        directory=selection_directory(root,selection_id).resolve()
        path=(directory/'selection.zip').resolve()
        if not path.is_relative_to(directory) or not path.is_file():raise HTTPException(404,'选区导出不存在')
        return FileResponse(path,media_type='application/zip',filename=f'waldo-{selection_id}.zip')

    @app.get('/api/demo/selections/{selection_id}/report')
    def selection_report(selection_id: str):
        from ..semantics.selection import selection_directory
        directory=selection_directory(root,selection_id)
        path=root/'reports/sink-selection-20261021/README.md' if selection_id=='sink-v5' else directory/'README.md'
        if not path.is_file():raise HTTPException(404,'选区报告尚未完成')
        return FileResponse(path,media_type='text/plain; charset=utf-8')

    @app.post('/api/demo/selections')
    async def generate_selection(request: Request):
        try:payload=await request.json()
        except ValueError as exc:raise HTTPException(400,'无效的选区请求') from exc
        try:return selection_jobs.submit(payload)
        except RuntimeError as exc:raise HTTPException(409,str(exc)) from exc

    @app.get('/api/demo/selection-jobs/{job_id}')
    def selection_job(job_id: str):
        return selection_jobs.job(job_id)

    @app.get('/api/demo/selections/{selection_id}/validation-image')
    def selection_validation_image(selection_id: str):
        from ..semantics.selection import selection_directory
        directory=selection_directory(root,selection_id)
        path=root/'reports/sink-selection-20261021/validation-views.jpg' if selection_id=='sink-v5' else directory/'validation-views.jpg'
        if not path.is_file():raise HTTPException(404,'验证图尚未生成')
        return FileResponse(path)

    @app.get('/api/demo/images/{image_id}')
    def demo_image(image_id: int):
        from .demo import read
        data = read(root/'data/semantic_waldo/project.json',{})
        frame = next((f for f in data.get('frames',[]) if f['image_id']==image_id),None)
        if not frame:
            raise HTTPException(404,'场景图像不存在')
        path = Path(frame['path']).resolve()
        if not path.is_relative_to((root/'data').resolve()) or not path.is_file():
            raise HTTPException(404,'场景图像不可用')
        return FileResponse(path)

    @app.get('/api/demo/artifacts/{model}/{relative:path}')
    def demo_artifact(model: str, relative: str):
        from .demo import model_report
        report = model_report(root,model).resolve()
        path = (report/safe_relative(relative)).resolve()
        if not path.is_relative_to(report) or path.suffix.lower() not in {'.png','.json','.md','.csv'} or not path.is_file():
            raise HTTPException(404,'结果不存在')
        return FileResponse(path)

    def semantic_config_path(name=None):
        if name is None:
            name = 'semantic_waldo_repaired' if (root / 'configs/semantic_waldo_repaired.json').is_file() else 'semantic_waldo'
        if not name.startswith('semantic_') or any(not (c.isascii() and (c.isalnum() or c in '_-')) for c in name):
            raise HTTPException(400, 'Invalid semantic configuration name')
        path = root / 'configs' / f'{name}.json'
        if not path.resolve().is_relative_to((root / 'configs').resolve()):
            raise HTTPException(400, 'Invalid semantic configuration path')
        return path

    @app.get('/api/semantic/status')
    def semantic_status(config: str | None = None):
        from ..semantics.project import load_config
        from ..semantics.status import status
        from .gpu_lease import active_lease
        config_path = semantic_config_path(config)
        if not config_path.is_file():
            return {'available': False, 'gpu_busy': live.active is not None or any(r['status'] == 'running' for r in store.list_runs())}
        config = load_config(config_path, root=root)
        result = status(config)
        suffix = '?config=' + quote(config_path.stem)
        for item in [*result['previews'], *result['queries']]:
            item['url'] += suffix
        return {'available': True, **result, 'config': config_path.stem,
                'configs': [p.stem for p in sorted((root / 'configs').glob('semantic_*.json'))],
                'gpu_busy': live.active is not None or any(r['status'] == 'running' for r in store.list_runs()),
                'offline_gpu_busy': active_lease(root) is not None}

    @app.get('/api/semantic/guide')
    def semantic_guide():
        return FileResponse(root / 'docs/semantics.md', media_type='text/plain; charset=utf-8')

    @app.get('/api/semantic/multiview')
    def semantic_multiview():
        from .demo import read
        report=root/'reports/semantic-multiview-20261021'
        results=read(report/'results.json')
        return {'available':bool(results),'results':results,'progress':read(report/'progress.json')}

    @app.get('/api/semantic/stage-diagnostics')
    def semantic_stage_diagnostics():
        from .demo import read
        report=root/'reports/semantic-stage-diagnostics-20261022'
        results=read(report/'results.json')
        return {'available':bool(results and results.get('status')=='completed'),
                'results':results,'findings':read(report/'findings.json',[]),
                'visibility':read(report/'visibility.json',[]),'progress':read(report/'progress.json')}

    @app.get('/api/semantic/stage-diagnostics/artifacts/{relative:path}')
    def semantic_stage_diagnostic_artifact(relative: str):
        report=(root/'reports/semantic-stage-diagnostics-20261022').resolve()
        path=(report/safe_relative(relative)).resolve()
        if not path.is_relative_to(report) or path.suffix.lower() not in {'.jpg','.json','.md'} or not path.is_file():
            raise HTTPException(404,'逐层诊断文件不存在')
        return FileResponse(path)

    @app.get('/api/semantic/multiview/artifacts/{relative:path}')
    def semantic_multiview_artifact(relative: str):
        report=(root/'reports/semantic-multiview-20261021').resolve()
        path=(report/safe_relative(relative)).resolve()
        if not path.is_relative_to(report) or path.suffix.lower() not in {'.png','.jpg','.json','.md'} or not path.is_file():
            raise HTTPException(404,'检查结果不存在')
        return FileResponse(path)

    @app.get('/api/semantic/codec-study')
    def semantic_codec_study():
        from ..semantics.reference_status import codec_status
        return codec_status(root)

    @app.get('/api/semantic/codec-study/guide')
    def semantic_codec_guide():
        path=root/'docs/semantics.md'
        if not path.is_file():
            raise HTTPException(404,'Codec study guide not found')
        return FileResponse(path,media_type='text/plain; charset=utf-8')

    @app.get('/api/semantic/codec-study/artifacts/{relative:path}')
    def semantic_codec_artifact(relative: str):
        report=(root/'reports/semantic-codec-study-20261021').resolve()
        path=(report/safe_relative(relative)).resolve()
        if not path.is_relative_to(report) or path.suffix not in ('.png','.json','.md','.csv','.log','.txt') or not path.is_file():
            raise HTTPException(404,'Codec study artifact not found')
        return FileResponse(path)

    @app.get('/api/semantic/reference')
    def semantic_reference():
        from ..semantics.reference_status import status
        return status(root)

    @app.get('/api/semantic/reference/guide')
    def semantic_reference_guide():
        path = root / 'docs/semantics.md'
        if not path.is_file():
            raise HTTPException(404, 'Reference guide not found')
        return FileResponse(path, media_type='text/plain; charset=utf-8')

    @app.get('/api/semantic/reference/artifacts/{relative:path}')
    def semantic_reference_artifact(relative: str):
        report = (root / 'reports/langsplat-reference-20261021').resolve()
        path = (report / safe_relative(relative)).resolve()
        if not path.is_relative_to(report) or path.suffix not in ('.png','.json','.md','.csv','.log','.txt') or not path.is_file():
            raise HTTPException(404, 'Reference artifact not found')
        return FileResponse(path)

    @app.get('/api/semantic/artifacts/{relative:path}')
    def semantic_artifact(relative: str, config: str | None = None):
        from ..semantics.project import load_config
        config = load_config(semantic_config_path(config), root=root)
        workspace = Path(config['workspace']).resolve()
        path = (workspace / safe_relative(relative)).resolve()
        if (not path.is_relative_to(workspace) or path.suffix not in ('.png', '.json')
                or path.relative_to(workspace).parts[0] not in ('raw', 'queries', 'evaluation') or not path.is_file()):
            raise HTTPException(404, 'Semantic artifact not found')
        return FileResponse(path)

    @app.get("/api/datasets/{dataset_id}")
    def dataset(dataset_id: str):
        return store.dataset_info(dataset_id)

    @app.get("/api/datasets/{dataset_id}/images/{image_id}")
    def dataset_image(dataset_id: str, image_id: int):
        _, dataset = store.dataset(dataset_id)
        return FileResponse(dataset["image_paths"][image_id])

    @app.post("/api/config/validate")
    async def config_validate(request: Request):
        return resolve_config(await request.json())

    @app.post("/api/datasets/import")
    async def import_dataset(request: Request):
        form = await request.form()
        mode = form.get("mode", "manifest")
        if mode not in ("manifest", "photos"):
            raise ValueError("mode 必须是 manifest 或 photos")
        files = form.getlist("files")
        paths = form.getlist("paths")
        if not files or any(not hasattr(file, "read") for file in files):
            raise ValueError("请选择数据文件")
        if paths and len(paths) != len(files):
            raise ValueError("paths 必须与 files 一一对应")
        incoming = root / "data" / "ui_imports" / uuid.uuid4().hex[:16]
        incoming.mkdir(parents=True, exist_ok=False)
        received = []
        total_bytes = 0
        seen = set()
        try:
            for index, file in enumerate(files):
                relative = safe_relative(str(paths[index] if paths else file.filename))
                target = incoming / relative
                if relative.as_posix().casefold() in seen:
                    raise ValueError("上传文件路径重复")
                seen.add(relative.as_posix().casefold())
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as stream:
                    while chunk := await file.read(1024 * 1024):
                        total_bytes += len(chunk)
                        if total_bytes > 1024 * 1024 * 1024:
                            raise ValueError("单次导入不得超过 1 GB")
                        stream.write(chunk)
                received.append(target)
            if mode == "manifest":
                selected = form.get("manifest_path")
                if selected:
                    manifest = incoming / safe_relative(str(selected))
                else:
                    candidates = [p for p in received if p.name.lower() == "dataset.json"]
                    if len(candidates) != 1:
                        raise ValueError("请选择唯一的 dataset.json，或指定 manifest_path")
                    manifest = candidates[0]
                if manifest not in received:
                    raise ValueError("指定的清单不在上传文件中")
            else:
                raw_camera = form.get("camera")
                if not isinstance(raw_camera, str):
                    raise ValueError("请填写共享内参 fx、fy、cx、cy")
                camera = json.loads(raw_camera)
                if not isinstance(camera, dict):
                    raise ValueError("camera 必须是 JSON 对象")
                photo_paths = [p for p in received if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp")]
                if len(photo_paths) < 2:
                    raise ValueError("请选择至少两张照片")
                first = cv2.imdecode(np.fromfile(photo_paths[0], dtype=np.uint8), cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
                if first is None:
                    raise ValueError("照片无法解码")
                height, width = first.shape[:2]
                for dimension, actual in (("width", width), ("height", height)):
                    if dimension in camera and camera[dimension] != actual:
                        raise ValueError("填写的图像尺寸与照片不匹配")
                try:
                    calibrated = Camera(1, width, height, [[camera["fx"], 0, camera["cx"]],
                                                         [0, camera["fy"], camera["cy"]], [0, 0, 1]])
                except KeyError as exc:
                    raise ValueError("缺少内参 fx、fy、cx 或 cy") from exc
                manifest = incoming / "dataset.json"
                if manifest.exists():
                    raise ValueError("照片导入不能包含已有 dataset.json，请选择清单导入")
                atomic_json(manifest, {"cameras": [{"id": 1, "width": width, "height": height,
                                                    "K": calibrated.K.tolist()}],
                                       "images": [{"id": i + 1, "name": path.relative_to(incoming).as_posix(),
                                                   "camera_id": 1, "path": path.relative_to(incoming).as_posix()}
                                                  for i, path in enumerate(photo_paths)]})
            validate_dimensions(manifest)
            dataset_id = stable_id("dataset-", manifest)
            store.datasets[dataset_id] = manifest
            return store.dataset_info(dataset_id)
        except Exception:
            # Preserve failed imports for inspection, but never discover them as usable datasets.
            (incoming / ".invalid-import").write_text("Import validation failed", encoding="utf-8")
            if (incoming / "dataset.json").exists():
                (incoming / "dataset.json").rename(incoming / "dataset.invalid")
            for path in received:
                if path.name.lower() == "dataset.json" and path.exists():
                    path.rename(path.with_suffix(".invalid"))
            raise

    @app.get("/api/runs")
    def runs(discover: bool = False):
        # Live status reads must not rescan every historical model and dataset.
        # Bootstrap and the explicit history refresh discover external results.
        if discover:
            store.discover()
        return store.list_runs()

    @app.post("/api/runs")
    async def start_run(request: Request):
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get("dataset_id"), str):
            raise ValueError("请先选择数据集")
        settings = resolve_config(body.get("config"))
        try:
            with store.lock:
                live.ensure_idle()
                return store.start(body["dataset_id"], settings)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/runs/{run_id}")
    def run_info(run_id: str):
        return store.public(store.get(run_id))

    @app.get("/api/rendering/capabilities")
    def rendering_capabilities():
        from .rendering import capabilities
        return capabilities()

    @app.post("/api/rendering/config/validate")
    async def rendering_config_validate(request: Request):
        from ..rendering.config import resolve_render_config
        return resolve_render_config(await request.json())

    @app.post("/api/rendering/runs")
    async def start_rendering(request: Request):
        from ..rendering.config import resolve_render_config
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("请选择位姿数据集或已有 SfM 结果")
        sources = [key for key in ("source_run_id", "dataset_id") if key in body]
        if len(sources) != 1 or not isinstance(body[sources[0]], str):
            raise ValueError("source_run_id 和 dataset_id 必须且只能指定一项")
        if "photo_dataset_id" in body and not isinstance(body["photo_dataset_id"], str):
            raise ValueError("photo_dataset_id 必须是已导入照片数据集 ID")
        settings = resolve_render_config(body.get("config"))
        try:
            with store.lock:
                live.ensure_idle()
                return store.start_rendering(body.get("source_run_id"), settings,
                                             dataset_id=body.get("dataset_id"), photo_dataset_id=body.get("photo_dataset_id"))
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/rendering/runs/{run_id}/result")
    def rendering_result(run_id: str):
        from .rendering import result
        return result(store, run_id)

    @app.websocket("/api/rendering/runs/{run_id}/live")
    async def live_rendering(websocket: WebSocket, run_id: str):
        await live.handle(websocket, run_id)

    @app.post("/api/rendering/runs/{run_id}/render")
    async def rendering_preview(run_id: str, request: Request):
        from ..rendering.pipeline import render_checkpoint
        from .rendering import public_preview
        run = store.get(run_id)
        if run.get("pipeline") not in ("nerf", "3dgs"):
            raise ValueError("该任务不是 NeRF 或 3DGS 训练")
        if run["status"] == "running" or any(item["status"] == "running" for item in store.list_runs()):
            raise HTTPException(409, "请先等待训练结束或取消当前任务，再渲染新视图")
        if live.active is not None:
            raise HTTPException(409, "请先关闭实时渲染，再生成离线预览")
        body = await request.json()
        if (not isinstance(body, dict) or set(body) - {"camera", "image_id"}
                or (("camera" in body) == ("image_id" in body))):
            raise ValueError("请提供 image_id 或 camera 中的一项")
        output = Path(run["output_path"])
        if not (output / "checkpoint.pt").is_file():
            raise ValueError("该任务尚未产生真实训练 checkpoint")
        # The renderer owns numerical/camera validation. Running in a thread
        # keeps status, SSE, and cancellation endpoints responsive.
        try:
            settings = read_json(output / 'config.json', {})
            if run['pipeline'] == '3dgs' and settings.get('gaussian', {}).get('backend') == 'gsplat':
                from .live import ProcessWorker
                from .rendering import preview_worker_main
                try:
                    lease, _ = live.claim(run_id)
                except RuntimeError as exc:
                    raise HTTPException(409, str(exc)) from exc
                try:
                    lease['worker'] = ProcessWorker(output, target=preview_worker_main, extra_args=(body,))
                    result = await lease['worker'].receive(120)
                    if result.get('type') != 'preview':
                        raise ValueError(result.get('message', 'CUDA 预览失败'))
                    preview = result['preview']
                finally:
                    live.release(lease)
            else:
                preview = await asyncio.to_thread(render_checkpoint, output,
                                                 camera=body.get("camera"), image_id=body.get("image_id"))
        except NotImplementedError as exc:
            raise HTTPException(422, {"status": "todo", "message": str(exc)}) from exc
        except ImportError as exc:
            raise ValueError(str(exc)) from exc
        public = public_preview(run_id, output, preview)
        if public is None:
            raise ValueError("渲染器未生成有效预览文件")
        with store.lock:
            run["latest_preview_record"] = preview
            run["latest_preview"] = public
            atomic_json(store.state / "runs" / f"{run_id}.json", run)
        return public

    @app.post("/api/runs/{run_id}/cancel")
    def cancel(run_id: str):
        return store.cancel(run_id)

    @app.get("/api/runs/{run_id}/events")
    async def events(run_id: str, request: Request):
        store.get(run_id)
        try:
            after = int(request.headers.get("last-event-id", request.query_params.get("after", "0")))
        except ValueError as exc:
            raise ValueError("事件 ID 必须是整数") from exc

        async def stream():
            cursor = after
            last_ping = time.monotonic()
            while not await request.is_disconnected():
                for item in store.events(run_id):
                    if item["seq"] > cursor:
                        cursor = item["seq"]
                        yield f"id: {cursor}\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
                run = store.get(run_id)
                if run["status"] != "running":
                    yield "data: " + json.dumps({"kind": "terminal", "status": run["status"],
                                                 "stage": run["stage"]}, ensure_ascii=False) + "\n\n"
                    break
                if time.monotonic() - last_ping > 10:
                    yield ": keepalive\n\n"
                    last_ping = time.monotonic()
                await asyncio.sleep(0.25)
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    def run_image_urls(run):
        available = store.image_paths(run)
        return (lambda image_id: f"/api/runs/{run['id']}/images/{image_id}" if image_id in available else None)

    @app.get("/api/runs/{run_id}/scene")
    def scene(run_id: str, snapshot: str = "latest"):
        model, run, snapshot_id = store.model(run_id, snapshot)
        return make_scene(model, f"{run_id}:{snapshot_id}", snapshot_id, run["snapshots"], run_image_urls(run))

    @app.get("/api/runs/{run_id}/points/{point_id}")
    def point(run_id: str, point_id: int, snapshot: str = "latest"):
        model, run, _ = store.model(run_id, snapshot)
        return point_details(model, point_id, run_image_urls(run))

    @app.get("/api/runs/{run_id}/images/{image_id}")
    def run_image(run_id: str, image_id: int):
        return FileResponse(store.image_paths(store.get(run_id))[image_id])

    @app.post("/api/results/import")
    async def import_model(request: Request):
        form = await request.form()
        file = form.get("file")
        if not hasattr(file, "read"):
            raise ValueError("请选择 model.json")
        raw = await file.read(200 * 1024 * 1024 + 1)
        if len(raw) > 200 * 1024 * 1024:
            raise ValueError("模型文件不得超过 200 MB")
        # Parse before creating an output, then use the project's model validator.
        json.loads(raw)
        run_id = uuid.uuid4().hex[:16]
        output = root / "outputs" / ("imported_" + run_id)
        output.mkdir(parents=True)
        model_path = output / "model.json"
        model_path.write_bytes(raw)
        try:
            model = load_reconstruction(model_path)
            summary = reconstruction_summary(model)
            save_reconstruction(model, model_path)
            write_ply(model, output / "points.ply")
            write_colmap_text(model, output / "sparse" / "0")
        except (KeyError, TypeError) as exc:
            model_path.rename(output / "invalid-model.txt")
            raise ValueError(f"模型格式无效：{exc}") from exc
        except ValueError:
            model_path.rename(output / "invalid-model.txt")
            raise
        atomic_json(output / "summary.json", summary)
        run = {"id": run_id, "name": str(file.filename or "model.json"), "status": "imported", "stage": "done",
               "output_path": str(output), "images_available": False,
               "warnings": ["导入模型不含原始照片，照片预览不可用"],
               "snapshots": [{"id": "final", "label": "导入模型", "stage": "done"}],
               **{key: summary[key] for key in ("registered_images", "image_count", "point_count")}}
        store.runs[run_id] = run
        atomic_json(store.state / "runs" / f"{run_id}.json", run)
        return store.public(run)

    @app.get("/api/runs/{run_id}/reports")
    def reports(run_id: str):
        output = Path(store.get(run_id)["output_path"])
        result = {name: read_json(output / f"{name}.json") for name in ("pairs", "initialization", "ba", "summary", "timings")}
        result["log"] = None
        log_path = store.state / "logs" / f"{run_id}.log"
        if log_path.exists():
            result["log"] = log_path.read_text(encoding="utf-8", errors="replace")[-20000:]
        return result

    @app.get("/api/runs/{run_id}/artifacts")
    def artifacts(run_id: str):
        output = Path(store.get(run_id)["output_path"])
        names = list(artifact_paths(output))
        if any(name.startswith("sparse/") for name in names):
            names.append("sparse.zip")
        if names:
            names.append("results.zip")
        return [{"name": name, "url": f"/api/runs/{run_id}/artifacts/{name}"} for name in names]

    @app.get("/api/runs/{run_id}/artifacts/{filename:path}")
    def artifact(run_id: str, filename: str):
        output = Path(store.get(run_id)["output_path"])
        if filename in ("sparse.zip", "results.zip"):
            paths = artifact_paths(output)
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
                for name, path in paths.items():
                    if filename != "sparse.zip" or name.startswith("sparse/"):
                        zipped.write(path, name)
            return Response(archive.getvalue(), media_type="application/zip",
                            headers={"Content-Disposition": f'attachment; filename="{filename}"'})
        try:
            relative = safe_relative(filename)
        except ValueError as exc:
            raise KeyError("结果文件不存在") from exc
        candidate = (output / relative).resolve()
        if (not candidate.is_relative_to(output.resolve()) or 'ui' in relative.parts
                or candidate.suffix.lower() not in {'.json', '.ply', '.txt', '.png', '.npz', '.pt', '.log'}
                or not candidate.is_file()):
            raise KeyError("结果文件不存在")
        if candidate.suffix.lower() == '.png':
            # Close the file before sending to a potentially slow HTTP reader.
            # FileResponse can otherwise hold a Windows delete-sharing lock for
            # the lifetime of the response while the worker publishes a preview.
            return Response(candidate.read_bytes(), media_type='image/png', headers={
                'Content-Disposition': f"attachment; filename*=utf-8''{quote(candidate.name)}"})
        return FileResponse(candidate, filename=candidate.name)

    static = Path(__file__).parent / "static"
    if (static / "index.html").exists():
        app.mount("/", StaticFiles(directory=static, html=True), name="workbench")
    else:
        @app.get("/")
        def missing_frontend():
            return HTMLResponse("<h1>Mini-COLMAP</h1><p>前端资源尚未构建，请按开发说明运行前端构建。</p>", status_code=503)
    return app
