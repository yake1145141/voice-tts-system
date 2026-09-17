#!/usr/bin/env bash
# =============================================================================
# Ubuntu 22.04 语音服务端引导脚本（第一段）
#   系统依赖 + 独立 Python 3.12 运行时 + 虚拟环境
#
#   bash bootstrap.sh
#
# 为什么必须是 Python 3.12：
#   依赖里的 fairseq 在 Linux 上走 fairseq-fixed，而 PyPI 上只有
#   cp312 的 manylinux_2_28 wheel。3.10 / 3.11 会退化成源码编译 fairseq，基本必失败。
#
# 为什么不用 deadsnakes PPA：
#   国内机器取 launchpad 的签名密钥经常超时；这里直接用 python-build-standalone
#   （也是本项目 Linux 便携包用的同一个东西），自带 pip，解压即用，不污染系统 Python。
#
# 本脚本可重复执行（幂等）。
# =============================================================================
set -Eeuo pipefail

export DEBIAN_FRONTEND=noninteractive

APP_DIR="${APP_DIR:-/opt/tts-server}"
PY_VERSION="${PY_VERSION:-3.12.14}"
PY_TAG="${PY_TAG:-20260901}"
ASSET="cpython-${PY_VERSION}+${PY_TAG}-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"

# 优先国内镜像：GitHub 在国内常见几十 KB/s，30MB 要等十来分钟
PY_MIRRORS=(
    "https://mirror.nju.edu.cn/github-release/astral-sh/python-build-standalone/${PY_TAG}"
    "https://mirrors.ustc.edu.cn/github-release/astral-sh/python-build-standalone/${PY_TAG}"
    "https://github.com/astral-sh/python-build-standalone/releases/download/${PY_TAG}"
)

PIP_INDEX="${PIP_INDEX_URL:-https://pypi.org/simple}"
CACHE_DIR="$APP_DIR/.cache"

log()  { printf '\033[1;32m[bootstrap]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[bootstrap]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[bootstrap]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "请用 root 运行（sudo bash bootstrap.sh）"

log "1/4 安装系统依赖（ffmpeg 用于 RVC 读写音频）"
apt-get update -y
apt-get install -y --no-install-recommends \
    ffmpeg \
    build-essential \
    curl \
    ca-certificates \
    xz-utils

mkdir -p "$APP_DIR"/{app,models,bin,output,logs} "$CACHE_DIR"

log "2/4 准备独立 Python ${PY_VERSION}"
if [ -x "$APP_DIR/python/bin/python3.12" ]; then
    log "    已存在 $APP_DIR/python，跳过下载"
else
    if [ ! -f "$CACHE_DIR/$ASSET" ]; then
        ok=0
        for base in "${PY_MIRRORS[@]}"; do
            log "    尝试下载: $base/$ASSET"
            if curl -fL --retry 2 --connect-timeout 15 -m 900 \
                    -o "$CACHE_DIR/$ASSET.part" "$base/$ASSET"; then
                mv "$CACHE_DIR/$ASSET.part" "$CACHE_DIR/$ASSET"
                ok=1
                break
            fi
            warn "    该镜像失败，换下一个"
            rm -f "$CACHE_DIR/$ASSET.part"
        done
        [ "$ok" = "1" ] || die "所有镜像都下载失败；请手动把 $ASSET 放到 $CACHE_DIR/"
    else
        log "    使用缓存 $CACHE_DIR/$ASSET"
    fi
    log "    解压到 $APP_DIR"
    tar -xzf "$CACHE_DIR/$ASSET" -C "$APP_DIR"
    rm -rf "$APP_DIR/python/lib/python3.12/test"
fi
"$APP_DIR/python/bin/python3.12" -V

log "3/4 创建虚拟环境 $APP_DIR/venv"
if [ ! -x "$APP_DIR/venv/bin/python" ]; then
    "$APP_DIR/python/bin/python3.12" -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/python" -V

log "4/4 升级 pip / setuptools / wheel"
# 注意：setuptools 81+ 移除了 pkg_resources，而 librosa/tts-with-rvc 还在用它，
# 所以这里必须钉住 setuptools<81，否则服务启动就 ModuleNotFoundError: pkg_resources
"$APP_DIR/venv/bin/python" -m pip install --upgrade pip wheel -i "$PIP_INDEX"
"$APP_DIR/venv/bin/python" -m pip install "setuptools<81" -i "$PIP_INDEX"
"$APP_DIR/venv/bin/python" -m pip -V

log "引导完成 → 下一步执行 install_deps.sh"
