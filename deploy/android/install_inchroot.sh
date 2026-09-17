#!/bin/bash
# 在 chroot 内执行的安装脚本（由 2_install_server.sh 调用）
# 设计：不依赖发行版 python，直接用自带的 python-build-standalone（arm64），
#       这样只需 apt 装 ffmpeg 和编译工具，避免极简 rootfs 里 python/CA 的坑。
set -Eeuo pipefail

# chroot 从安卓继承了 TMPDIR=/data/local/tmp（不存在），必须改到 /tmp
export TMPDIR=/tmp
export HOME=/root
export LANG=C.UTF-8
mkdir -p /tmp

MIRROR="${UBUNTU_MIRROR:-http://mirrors.tuna.tsinghua.edu.cn/ubuntu-ports}"
PIP_MIRROR="${PIP_MIRROR:-https://pypi.tuna.tsinghua.edu.cn/simple}"
TTS_DIR=/opt/tts-server
PY_DIR="$TTS_DIR/python"
PY_TARBALL=/root/python-arm64.tar.gz

log() { printf '\n=== [%s] %s ===\n' "$(date '+%H:%M:%S')" "$*"; }

log "1/7 配置 apt 镜像（$MIRROR）"
KEYRING=/usr/share/keyrings/ubuntu-archive-keyring.gpg
mkdir -p /etc/apt/sources.list.d
rm -f /etc/apt/sources.list.d/ubuntu.sources
{
    echo "Types: deb"
    echo "URIs: $MIRROR"
    echo "Suites: noble noble-updates"
    echo "Components: main restricted universe multiverse"
    if [ -f "$KEYRING" ]; then
        echo "Signed-By: $KEYRING"
    else
        # 极简 rootfs 可能没有 keyring，只能标记为受信任源
        echo "Trusted: yes"
    fi
} > /etc/apt/sources.list.d/tts.sources
cat /etc/apt/sources.list.d/tts.sources
printf '' > /etc/apt/sources.list
export DEBIAN_FRONTEND=noninteractive

log "2/7 更新索引"
if ! apt-get update -qq > /tmp/apt-update.log 2>&1; then
    tail -5 /tmp/apt-update.log
    log "换清华镜像重试"
    sed -i 's#^URIs:.*#URIs: http://mirrors.tuna.tsinghua.edu.cn/ubuntu-ports#' /etc/apt/sources.list.d/tts.sources
    apt-get update -qq > /tmp/apt-update.log 2>&1 || { tail -5 /tmp/apt-update.log; exit 1; }
fi
apt-cache policy | grep -c ubuntu-ports || true

log "3/7 安装系统依赖（ffmpeg + 编译工具）"
apt-get install -y --no-install-recommends \
    ffmpeg build-essential libsndfile1 ca-certificates \
    > /tmp/apt-install.log 2>&1 || { tail -20 /tmp/apt-install.log; exit 1; }
tail -3 /tmp/apt-install.log
ffmpeg -version | head -1
gcc --version | head -1

log "4/7 解压自带 Python 运行时"
cd "$TTS_DIR"
[ -x "$PY_DIR/bin/python3" ] || {
    mkdir -p "$PY_DIR"
    tar -xzf "$PY_TARBALL" -C "$TTS_DIR"
}
PY="$PY_DIR/bin/python3"
"$PY" --version

log "5/7 安装 pip 依赖（torch arm64 CPU 版，先装）"
# python-build-standalone 默认 CC/CXX 是 clang，这里统一改成 gcc/g++
export CC="$(command -v gcc)"
export CXX="$(command -v g++)"
echo "CC=$CC CXX=$CXX"
"$PY" -m pip install --upgrade pip -i "$PIP_MIRROR" 2>&1 | tail -2
# 注意：PyPI 上的 aarch64 轮子是带 CUDA 的大包（会把 5GB 的 nvidia 库也装进来），
# 必须用 PyTorch 官方的 CPU 索引，体积小很多且更适合手机
if ! "$PY" -m pip install --index-url https://download.pytorch.org/whl/cpu torch torchaudio 2>&1 | tail -3; then
    echo "CPU 索引失败，回退到 PyPI（会安装 CUDA 版，体积较大）"
    "$PY" -m pip install -i "$PIP_MIRROR" torch torchaudio 2>&1 | tail -3
fi
"$PY" -c "import torch; print('torch', torch.__version__, '| threads', torch.get_num_threads())"

log "6/7 安装服务端依赖（tts-with-rvc 等，pyworld/fairseq 会源码编译）"
if ! "$PY" -m pip install -i "$PIP_MIRROR" \
        fastapi "uvicorn[standard]" pydantic PyYAML "httpx>=0.24" 2>&1 | tail -3; then
    echo "基础依赖安装失败"
    exit 1
fi
if ! "$PY" -m pip install -i "$PIP_MIRROR" "tts-with-rvc>=0.1.9" > /tmp/pip-tts.log 2>&1; then
    echo
    echo "!! tts-with-rvc 安装失败（多半是 fairseq/pyworld 编译问题），报错见 /tmp/pip-tts.log"
    grep -E "error|ERROR|Failed|failed" /tmp/pip-tts.log | tail -12
    exit 2
fi
tail -2 /tmp/pip-tts.log

log "7/7 自检"
"$PY" - <<'PY'
import importlib, sys
ok = True
for name in ("torch", "fastapi", "uvicorn", "yaml", "tts_with_rvc"):
    try:
        mod = importlib.import_module(name)
        print(f"  OK {name} {getattr(mod, '__version__', '')}")
    except Exception as exc:
        ok = False
        print(f"  FAIL {name}: {exc}")
sys.exit(0 if ok else 1)
PY

log "安装完成"
du -sh "$PY_DIR" "$TTS_DIR" 2>/dev/null || true
