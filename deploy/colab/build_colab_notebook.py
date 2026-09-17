"""生成 Google Colab 专用 notebook：deploy/colab/tts-server-colab.ipynb

把 tts-server 的 4 个 py 文件打进 tar.gz 再 base64 内嵌进 notebook，
用户在 Colab 上不用上传源码、不用 git clone，从上往下运行即可。

单元格内容放在 cells/ 目录（01_intro.md、02_params.py …），改完重新执行：
    python deploy/colab/build_colab_notebook.py
"""

from __future__ import annotations

import base64
import gzip
import io
import json
import re
import tarfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
SERVER = ROOT / "tts-server"
CELLS_DIR = HERE / "cells"
OUT = HERE / "tts-server-colab.ipynb"

PAYLOAD_FILES = ["main.py", "config.py", "engine.py", "storage.py"]
PAYLOAD_PLACEHOLDER = "__PAYLOAD__"


def build_payload() -> str:
    """把服务端代码打包成 base64(gzip(tar))，供 notebook 内嵌。"""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name in PAYLOAD_FILES:
            data = (SERVER / name).read_bytes()
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return base64.b64encode(gzip.compress(buffer.getvalue(), 9)).decode("ascii")


def cell_from_file(path: Path, payload: str) -> dict:
    source = path.read_text(encoding="utf-8").rstrip("\n")
    if PAYLOAD_PLACEHOLDER in source:
        source = source.replace(PAYLOAD_PLACEHOLDER, payload)
    kind = "markdown" if path.suffix == ".md" else "code"
    cell = {
        "cell_type": kind,
        "metadata": {},
        "source": [line + "\n" for line in source.split("\n")],
    }
    if kind == "code":
        cell["execution_count"] = None
        cell["outputs"] = []
    return cell


def main() -> int:
    payload = build_payload()
    files = sorted(CELLS_DIR.glob("*"))
    cells = [cell_from_file(path, payload) for path in files if path.suffix in (".md", ".py")]
    notebook = {
        "cells": cells,
        "metadata": {
            "colab": {"provenance": [], "toc_visible": True, "gpuType": "T4"},
            "kernelspec": {"display_name": "Python 3", "name": "python3"},
            "language_info": {"name": "python"},
        },
        "nbformat": 4,
        "nbformat_minor": 0,
    }
    OUT.write_text(json.dumps(notebook, ensure_ascii=False, indent=1), encoding="utf-8")
    size_kb = OUT.stat().st_size / 1024
    print(f"已生成 {OUT.name}：{len(cells)} 个单元格，{size_kb:.1f} KB"
          f"（内嵌服务端代码 {len(payload) / 1024:.1f} KB base64）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
