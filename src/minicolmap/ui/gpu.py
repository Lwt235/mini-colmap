"""Explicit physical-GPU selection without importing CUDA in the web process."""
import csv
import io
import os
from pathlib import Path
import subprocess
from .store import read_json
from .worker import atomic_json


def gpu_path(root):
    return Path(root) / ".cache/workbench-gpu.json"


def selected_gpu(root):
    value = read_json(gpu_path(root), {}).get("index", 7)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 255:
        raise ValueError("无效的显卡配置")
    return value


def worker_environment(root):
    return dict(os.environ, CUDA_VISIBLE_DEVICES=str(selected_gpu(root)))


def inventory(root):
    devices = []
    message = None
    try:
        completed = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,uuid,name,memory.total,memory.free",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        for row in csv.reader(io.StringIO(completed.stdout)):
            if len(row) != 5:
                continue
            index, uuid, name, total, free = [v.strip() for v in row]
            devices.append(
                dict(
                    index=int(index),
                    uuid=uuid,
                    name=name,
                    total_mb=int(total),
                    free_mb=int(free),
                )
            )
    except (OSError, ValueError, subprocess.SubprocessError):
        message = "暂时无法读取显卡列表"
    return dict(selected=selected_gpu(root), devices=devices, message=message)


def select_gpu(root, index):
    if isinstance(index, bool) or not isinstance(index, int):
        raise ValueError("请选择物理显卡编号")
    devices = inventory(root)["devices"]
    if index not in {d["index"] for d in devices}:
        raise ValueError("所选显卡不存在或不可用")
    atomic_json(gpu_path(root), dict(index=index))
    return inventory(root)
