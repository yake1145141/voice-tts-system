#!/usr/bin/env bash
# Linux 一键启动脚本（首次运行会自动创建虚拟环境并安装依赖）
#
# 用法：
#   chmod +x deploy/start_tts_server.sh
#   ./deploy/start_tts_server.sh              # 前台启动
#   nohup ./deploy/start_tts_server.sh > tts.log 2>&1 &   # 后台启动
#
# 前置条件：
#   1. 已安装 Python 3.10 ~ 3.12 以及 ffmpeg
#   2. 已按 https://pytorch.org/get-started/locally/ 安装对应 CUDA 版本的 PyTorch

set -euo pipefail

SERVER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../tts-server" && pwd)"
cd "$SERVER_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="$SERVER_DIR/.venv"

if [ ! -d "$VENV_DIR" ]; then
    echo "[start] 创建虚拟环境: $VENV_DIR"
    "$PYTHON_BIN" -m venv "$VENV_DIR"
    "$VENV_DIR/bin/python" -m pip install --upgrade pip
    echo "[start] 安装依赖（若尚未安装 PyTorch，请先手动安装 CUDA 版本）"
    "$VENV_DIR/bin/python" -m pip install -r requirements.txt
fi

if [ ! -f "config.yaml" ]; then
    echo "[start] 缺少 config.yaml"
    exit 1
fi

exec "$VENV_DIR/bin/python" main.py "$@"
