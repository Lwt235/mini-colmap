"""Optional local visual workbench. Importing this module needs no UI dependencies."""

from pathlib import Path


def create_app(project_root: Path | None = None):
    try:
        import multipart  # noqa: F401
        import uvicorn  # noqa: F401
        from .app import create_app as factory
    except ImportError as exc:
        raise RuntimeError('界面依赖未安装，请运行 python -m pip install -e ".[ui]"') from exc
    return factory(project_root)


def run(*, host: str = "127.0.0.1", port: int = 8765,
        open_browser: bool = True, project_root: Path | None = None):
    if host != "127.0.0.1":
        raise ValueError("工作台仅允许监听 127.0.0.1")
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    app = create_app(project_root)
    import uvicorn
    if open_browser:
        import threading
        import webbrowser
        timer = threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}"))
        timer.daemon = True
        timer.start()
    # JPEG frames are already compressed; compressing them again costs latency.
    # Camera requests stay small and bounded even during rapid pointer input.
    uvicorn.run(app, host=host, port=port, access_log=False,
                ws_max_size=65536, ws_max_queue=2, ws_per_message_deflate=False)
