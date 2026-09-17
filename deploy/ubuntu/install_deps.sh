#!/usr/bin/env bash
# =============================================================================
# Ubuntu 22.04 语音服务端引导脚本（第二段）：安装 PyTorch 与全部 Python 依赖
#
#   bash install_deps.sh
#
# P106-100 / P4 / P40 这类 Pascal 卡是 compute capability **sm_61**，
# 而 cu124 之后的 PyTorch 已经不再编译 sm_61 内核（报 no kernel image /
# CUDA error: operation not supported）。所以这里固定用 cu121：
#   torch 2.5.1+cu121 的 arch_list 覆盖 sm_50 ~ sm_90，Pascal 可用。
#
# 本脚本可重复执行（幂等）。
# =============================================================================
set -Eeuo pipefail

APP_DIR="${APP_DIR:-/opt/tts-server}"
TORCH_VER="${TORCH_VER:-2.5.1}"
CUDA_TAG="${CUDA_TAG:-cu121}"
TORCH_INDEX="${TORCH_INDEX_URL:-https://download.pytorch.org/whl/${CUDA_TAG}}"
PIP_INDEX="${PIP_INDEX_URL:-https://pypi.org/simple}"

VENV_PY="$APP_DIR/venv/bin/python"

log()  { printf '\033[1;32m[deps]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[deps]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[deps]\033[0m %s\n' "$*" >&2; exit 1; }

[ -x "$VENV_PY" ] || die "虚拟环境不存在：$VENV_PY（先跑 bootstrap.sh）"

# python-build-standalone 的 sysconfig 里 CC/CXX 默认写的是 clang/clang++，
# 构建机上通常只有 gcc/g++，会让 pyworld 这类需要现场编译的包直接失败。
export CC="${CC:-$(command -v gcc || command -v cc)}"
export CXX="${CXX:-$(command -v g++ || command -v c++)}"
log "编译器: CC=$CC CXX=$CXX"

log "1/4 安装 PyTorch ${TORCH_VER}+${CUDA_TAG}（约 2.4GB，最慢的一步）"
"$VENV_PY" -m pip install \
    --no-cache-dir \
    "torch==${TORCH_VER}+${CUDA_TAG}" "torchaudio==${TORCH_VER}+${CUDA_TAG}" \
    --index-url "$TORCH_INDEX"

log "2/4 安装 tts-with-rvc 及依赖"
# 注意：这里必须显式列出包名，不能直接用 "$@"（无参数时会变成空列表，pip 直接报错）
REQUIREMENTS=("tts-with-rvc>=0.1.9")
if [ "$#" -gt 0 ]; then
    REQUIREMENTS+=("$@")
fi
"$VENV_PY" -m pip install -i "$PIP_INDEX" "${REQUIREMENTS[@]}"

log "3/4 安装 HTTP 服务依赖"
if [ -f "$APP_DIR/requirements.txt" ]; then
    "$VENV_PY" -m pip install -i "$PIP_INDEX" -r "$APP_DIR/requirements.txt"
fi

# setuptools 81+ 删掉了 pkg_resources，librosa / tts-with-rvc 还在 import 它，
# 少了这一步服务启动会直接 ModuleNotFoundError: No module named 'pkg_resources'
"$VENV_PY" -m pip install -i "$PIP_INDEX" "setuptools<81"

log "4/4 自检"
"$VENV_PY" - <<'PY'
import torch, torchaudio, sys
print("python      :", sys.version.split()[0])
print("torch       :", torch.__version__)
print("torchaudio  :", torchaudio.__version__)
print("cuda_available:", torch.cuda.is_available())
if torch.cuda.is_available():
    print("device      :", torch.cuda.get_device_name(0))
    print("capability  :", torch.cuda.get_device_capability(0))
    print("arch_list   :", torch.cuda.get_arch_list())
else:
    print("!! CUDA 不可用，检查驱动")
for mod in ("fastapi", "uvicorn", "yaml", "edge_tts", "fairseq", "tts_with_rvc"):
    try:
        __import__(mod)
        print(f"import {mod:<12}: ok")
    except Exception as exc:
        print(f"import {mod:<12}: FAIL {type(exc).__name__}: {exc}")
PY

log "依赖安装完成"
