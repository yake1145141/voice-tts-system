#!/usr/bin/env bash
# =============================================================================
#  voice-tts-system · 服务端一键安装脚本（Ubuntu / Debian x86_64）
#
#   sudo bash deploy/linux/install.sh \
#        --model /root/models/MyVoice.pth \
#        --index /root/models/MyVoice.index \
#        --api-key your-secret-key
#
#  常用参数：
#     --model PATH          RVC 模型 .pth（必需）
#     --index PATH          RVC 索引 .index（可选但建议）
#     --assets DIR          内含 hubert_base.pt / rmvpe.pt 的目录（可选，省下载）
#     --api-key KEY         服务密钥（不填则随机生成并打印）
#     --port N              监听端口（默认 8080）
#     --device MODE         auto / cuda:0 / cpu（默认 auto）
#     --max-chars N         单段最大字数（默认 80；2GB 显存建议 60）
#     --min-vram-mb N       低于该显存直接用 CPU（默认 1800）
#     --webui-password P    网页控制台密码（不填则无需登录）
#     --install-dir DIR     安装目录（默认 /opt/tts-server）
#     --cpu                 安装 CPU 版 PyTorch（没有 N 卡时）
#     --dry-run             只打印步骤，不实际执行
#
#  之所以必须用 Python 3.12：Linux 上 tts-with-rvc 依赖 fairseq-fixed，
#  PyPI 只有 cp312 的 manylinux wheel，用 3.10/3.11 会退化成源码编译 fairseq。
#  之所以必须用 CUDA 12.1 的 PyTorch：Pascal（sm_61）等老卡在 cu124+ 上没有内核。
# =============================================================================
set -Eeuo pipefail

# ---------------------------------------------------------------- 默认值
INSTALL_DIR="${INSTALL_DIR:-/opt/tts-server}"
PORT="8080"
DEVICE="auto"
MAX_CHARS="80"
MIN_VRAM_MB="1800"
API_KEY=""
WEBUI_PASSWORD=""
MODEL_PATH=""
INDEX_PATH=""
ASSETS_DIR=""
VARIANT="cuda"
CUDA_TAG="cu121"
TORCH_VER="2.5.1"
PY_VERSION="3.12.14"
PY_TAG="20260901"
DRY_RUN=0

PY_MIRRORS=(
    "https://mirror.nju.edu.cn/github-release/astral-sh/python-build-standalone/${PY_TAG}"
    "https://mirrors.ustc.edu.cn/github-release/astral-sh/python-build-standalone/${PY_TAG}"
    "https://github.com/astral-sh/python-build-standalone/releases/download/${PY_TAG}"
)
PIP_INDEX="${PIP_INDEX_URL:-https://pypi.org/simple}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RELEASE_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# 源码目录：发布包里叫 server\，开发仓库里叫 tts-server\，两个都认
SERVER_SRC="$RELEASE_ROOT/server"
[ -d "$SERVER_SRC" ] || SERVER_SRC="$RELEASE_ROOT/tts-server"
SERVICE_NAME="tts-server"

# ---------------------------------------------------------------- 输出
C_G="\033[1;32m"; C_Y="\033[1;33m"; C_R="\033[1;31m"; C_C="\033[1;36m"; C_0="\033[0m"
step() { printf "${C_G}[%d/%d]${C_0} %s\n" "$1" "$2" "$3"; }
info() { printf "      %s\n" "$*"; }
warn() { printf "${C_Y}[warn]${C_0} %s\n" "$*" >&2; }
die()  { printf "${C_R}[error]${C_0} %s\n" "$*" >&2; exit 1; }

run() {
    if [ "$DRY_RUN" = "1" ]; then
        printf "${C_C}[dry-run]${C_0} %s\n" "$*"
    else
        "$@"
    fi
}

usage() { sed -n '3,27p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0; }

# ---------------------------------------------------------------- 参数解析
while [ $# -gt 0 ]; do
    case "$1" in
        --model)          MODEL_PATH="${2:-}"; shift 2 ;;
        --index)          INDEX_PATH="${2:-}"; shift 2 ;;
        --assets)         ASSETS_DIR="${2:-}"; shift 2 ;;
        --api-key)        API_KEY="${2:-}"; shift 2 ;;
        --port)           PORT="${2:-}"; shift 2 ;;
        --device)         DEVICE="${2:-}"; shift 2 ;;
        --max-chars)      MAX_CHARS="${2:-}"; shift 2 ;;
        --min-vram-mb)    MIN_VRAM_MB="${2:-}"; shift 2 ;;
        --webui-password) WEBUI_PASSWORD="${2:-}"; shift 2 ;;
        --install-dir)    INSTALL_DIR="${2:-}"; shift 2 ;;
        --cpu)            VARIANT="cpu"; shift ;;
        --cuda)           VARIANT="cuda"; CUDA_TAG="${2:-12.1}"; shift 2 ;;
        --dry-run)        DRY_RUN=1; shift ;;
        -h|--help)        usage ;;
        *)                die "未知参数：$1（用 --help 看用法）" ;;
    esac
done

TOTAL=9

# ---------------------------------------------------------------- 前置检查
[ "$(id -u)" = "0" ] || die "请用 root 运行（sudo bash $0 ...）"
[ -d "$SERVER_SRC" ] || die "找不到服务端源码目录（server/ 或 tts-server/），请在发布包根目录执行"
[ -n "$MODEL_PATH" ] || die "缺少 --model 参数（RVC 模型 .pth 路径）"
if [ "$DRY_RUN" != "1" ]; then
    [ -f "$MODEL_PATH" ] || die "模型文件不存在：$MODEL_PATH"
    [ -z "$INDEX_PATH" ] || [ -f "$INDEX_PATH" ] || die "索引文件不存在：$INDEX_PATH"
fi
[ -z "$ASSETS_DIR" ] || [ -d "$ASSETS_DIR" ] || die "assets 目录不存在：$ASSETS_DIR"

case "$(uname -m)" in
    x86_64|amd64) ;;
    *) die "本脚本只支持 x86_64；ARM 机器请参考 docs/ 用 Docker 部署" ;;
esac

if [ -z "$API_KEY" ]; then
    if [ "$DRY_RUN" = "1" ]; then
        API_KEY="(dry-run)"
    else
        API_KEY="tts-$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
        info "未指定 --api-key，已随机生成：$API_KEY"
    fi
fi

printf "\n${C_G}=== voice-tts-system 服务端安装 ===${C_0}\n"
cat <<EOF
  安装目录    : $INSTALL_DIR
  监听端口    : $PORT
  推理设备    : $DEVICE
  单段字数    : $MAX_CHARS      （显存门槛 $MIN_VRAM_MB MB）
  模型        : $MODEL_PATH
  索引        : ${INDEX_PATH:-（无）}
  PyTorch     : $VARIANT ${TORCH_VER}+${CUDA_TAG}
  控制台密码  : $( [ -n "$WEBUI_PASSWORD" ] && echo "已设置" || echo "未设置（无需登录）" )
EOF
printf "\n"

if [ "$DRY_RUN" != "1" ]; then
    sleep 1
fi

# ---------------------------------------------------------------- 1 系统依赖
step 1 $TOTAL "安装系统依赖"
if [ "$DRY_RUN" = "1" ]; then
    run apt-get update -y
    run apt-get install -y ffmpeg build-essential curl ca-certificates xz-utils espeak-ng
else
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y >/dev/null
    apt-get install -y --no-install-recommends \
        ffmpeg build-essential curl ca-certificates xz-utils >/dev/null
    # espeak-ng 是 Linux 上的离线语音兜底，装不上也不影响主流程
    apt-get install -y --no-install-recommends espeak-ng >/dev/null 2>&1 \
        && info "espeak-ng 已就绪（离线语音兜底可用）" \
        || warn "espeak-ng 安装失败，跳过离线兜底（不影响在线语音）"
fi

# ---------------------------------------------------------------- 2 显卡检查
step 2 $TOTAL "检查显卡与驱动"
if [ "$VARIANT" = "cpu" ]; then
    info "按参数要求安装 CPU 版 PyTorch"
elif command -v nvidia-smi >/dev/null 2>&1; then
    GPU_LINE="$(nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader | head -1)"
    info "检测到显卡：$GPU_LINE"
    VRAM_MB="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -1)"
    if [ -n "$VRAM_MB" ] && [ "$VRAM_MB" -lt "$MIN_VRAM_MB" ] 2>/dev/null; then
        warn "显存 ${VRAM_MB}MB 低于门槛 ${MIN_VRAM_MB}MB，服务会按配置走 CPU"
    fi
else
    warn "没有检测到 nvidia-smi。若确实没有 N 卡，请加 --cpu 参数重跑。"
fi

# ---------------------------------------------------------------- 3 Python 3.12
step 3 $TOTAL "准备 Python ${PY_VERSION} 运行时"
ASSET="cpython-${PY_VERSION}+${PY_TAG}-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
run mkdir -p "$INSTALL_DIR"/{app,models,bin,output,logs} "$INSTALL_DIR/.cache"
if [ -x "$INSTALL_DIR/python/bin/python3.12" ]; then
    info "已存在 $INSTALL_DIR/python，跳过下载"
elif [ "$DRY_RUN" = "1" ]; then
    run curl -fL -o "$INSTALL_DIR/.cache/$ASSET" "${PY_MIRRORS[0]}/$ASSET"
    run tar -xzf "$INSTALL_DIR/.cache/$ASSET" -C "$INSTALL_DIR"
else
    if [ ! -f "$INSTALL_DIR/.cache/$ASSET" ]; then
        ok=0
        for base in "${PY_MIRRORS[@]}"; do
            info "下载：$base/$ASSET"
            if curl -fL --retry 2 --connect-timeout 15 -m 900 \
                    -o "$INSTALL_DIR/.cache/$ASSET.part" "$base/$ASSET"; then
                mv "$INSTALL_DIR/.cache/$ASSET.part" "$INSTALL_DIR/.cache/$ASSET"
                ok=1; break
            fi
            warn "该镜像失败，换下一个"
            rm -f "$INSTALL_DIR/.cache/$ASSET.part"
        done
        [ "$ok" = "1" ] || die "独立 Python 下载失败，请手动放到 $INSTALL_DIR/.cache/"
    fi
    tar -xzf "$INSTALL_DIR/.cache/$ASSET" -C "$INSTALL_DIR"
    rm -rf "$INSTALL_DIR/python/lib/python3.12/test"
fi
[ "$DRY_RUN" = "1" ] || info "$("$INSTALL_DIR/python/bin/python3.12" -V)"

# ---------------------------------------------------------------- 4 虚拟环境
step 4 $TOTAL "创建虚拟环境"
if [ ! -x "$INSTALL_DIR/venv/bin/python" ]; then
    run "$INSTALL_DIR/python/bin/python3.12" -m venv "$INSTALL_DIR/venv"
fi
run "$INSTALL_DIR/venv/bin/python" -m pip install --upgrade pip wheel -i "$PIP_INDEX"
# setuptools 81+ 删掉了 pkg_resources，而 librosa 还在用它，必须钉住
run "$INSTALL_DIR/venv/bin/python" -m pip install "setuptools<81" -i "$PIP_INDEX"

# ---------------------------------------------------------------- 5 PyTorch
step 5 $TOTAL "安装 PyTorch（约 2.4GB，最慢的一步）"
if [ "$VARIANT" = "cpu" ]; then
    TORCH_INDEX="https://download.pytorch.org/whl/cpu"
    run "$INSTALL_DIR/venv/bin/python" -m pip install \
        torch torchaudio --index-url "$TORCH_INDEX"
else
    TORCH_INDEX="https://download.pytorch.org/whl/${CUDA_TAG}"
    run "$INSTALL_DIR/venv/bin/python" -m pip install \
        --no-cache-dir \
        "torch==${TORCH_VER}+${CUDA_TAG}" "torchaudio==${TORCH_VER}+${CUDA_TAG}" \
        --index-url "$TORCH_INDEX"
fi

# ---------------------------------------------------------------- 6 依赖
step 6 $TOTAL "安装 tts-with-rvc 与其它依赖"
# python-build-standalone 的 sysconfig 里 CC/CXX 默认是 clang，构建机上一般只有 gcc
export CC="${CC:-$(command -v gcc || command -v cc)}"
export CXX="${CXX:-$(command -v g++ || command -v c++)}"
run "$INSTALL_DIR/venv/bin/python" -m pip install -i "$PIP_INDEX" "tts-with-rvc>=0.1.9"
[ -f "$SERVER_SRC/requirements.txt" ] && \
    run "$INSTALL_DIR/venv/bin/python" -m pip install -i "$PIP_INDEX" -r "$SERVER_SRC/requirements.txt"

# ---------------------------------------------------------------- 7 代码与配置
step 7 $TOTAL "部署服务端代码与配置"
run mkdir -p "$INSTALL_DIR/app/static"
# 直接拷贝源码目录里的全部 .py，避免以后新增模块忘了同步
for f in "$SERVER_SRC"/*.py; do
    run cp "$f" "$INSTALL_DIR/app/$(basename "$f")"
done
[ -d "$SERVER_SRC/static" ] && run cp -r "$SERVER_SRC/static/." "$INSTALL_DIR/app/static/"
run cp "$SERVER_SRC/requirements.txt" "$INSTALL_DIR/requirements.txt"

CONFIG="$INSTALL_DIR/config.yaml"
run cp "$SERVER_SRC/config.yaml" "$CONFIG"
if [ "$DRY_RUN" != "1" ]; then
    MODEL_NAME="$(basename "$MODEL_PATH")"
    INDEX_NAME=""
    [ -n "$INDEX_PATH" ] && INDEX_NAME="$(basename "$INDEX_PATH")"
    sed -i -E "s#^  port: .*#  port: ${PORT}#"                            "$CONFIG"
    sed -i -E "s#^  api_key: .*#  api_key: \"${API_KEY}\"#"              "$CONFIG"
    sed -i -E "s#^  model: .*#  model: \"${MODEL_NAME}\"#"               "$CONFIG"
    sed -i -E "s#^  index: .*#  index: \"${INDEX_NAME}\"#"               "$CONFIG"
    sed -i -E "s#^  device: .*#  device: \"${DEVICE}\"#"                 "$CONFIG"
    sed -i -E "s#^  min_vram_mb: .*#  min_vram_mb: ${MIN_VRAM_MB}#"      "$CONFIG"
    sed -i -E "s#^  max_chars: .*#  max_chars: ${MAX_CHARS}#"            "$CONFIG"
    if [ -n "$WEBUI_PASSWORD" ]; then
        sed -i -E "s#^  password: .*#  password: \"${WEBUI_PASSWORD}\"#" "$CONFIG"
    fi
    info "配置已写入：$CONFIG"

    # 模型与索引
    cp "$MODEL_PATH" "$INSTALL_DIR/models/$MODEL_NAME"
    [ -n "$INDEX_PATH" ] && cp "$INDEX_PATH" "$INSTALL_DIR/models/$INDEX_NAME"
    info "模型已放入：$INSTALL_DIR/models/"

    # hubert_base.pt / rmvpe.pt：优先从 --assets 拷，其次从发布包同级目录找
    for weight in hubert_base.pt rmvpe.pt; do
        if [ -f "$INSTALL_DIR/$weight" ]; then
            continue
        elif [ -n "$ASSETS_DIR" ] && [ -f "$ASSETS_DIR/$weight" ]; then
            cp "$ASSETS_DIR/$weight" "$INSTALL_DIR/$weight"
            info "已拷贝 $weight（来自 --assets）"
        elif [ -f "$RELEASE_ROOT/$weight" ]; then
            cp "$RELEASE_ROOT/$weight" "$INSTALL_DIR/$weight"
            info "已拷贝 $weight（来自发布包）"
        elif [ -f "$SERVER_SRC/models/$weight" ]; then
            cp "$SERVER_SRC/models/$weight" "$INSTALL_DIR/$weight"
            info "已拷贝 $weight（来自 server/models）"
        else
            warn "没找到 $weight，服务首次启动会尝试联网下载（国内可能很慢）"
        fi
    done
fi

# ---------------------------------------------------------------- 8 systemd
step 8 $TOTAL "注册 systemd 服务"
if [ "$DRY_RUN" != "1" ]; then
    cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=voice-tts-system · Edge TTS + RVC 语音合成服务
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
Group=root
WorkingDirectory=${INSTALL_DIR}
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONIOENCODING=utf-8
# 本地已有 hubert_base.pt / rmvpe.pt 时禁止 huggingface_hub 联网检查，加快启动
Environment=HF_HUB_OFFLINE=1
Environment=HF_HUB_DISABLE_TELEMETRY=1
ExecStart=${INSTALL_DIR}/venv/bin/python ${INSTALL_DIR}/app/main.py --config ${INSTALL_DIR}/config.yaml
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE_NAME}
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable --now "$SERVICE_NAME" >/dev/null 2>&1 || true
    info "服务已注册并启动：systemctl status $SERVICE_NAME"
fi

# ---------------------------------------------------------------- 9 自检
step 9 $TOTAL "自检"
if [ "$DRY_RUN" = "1" ]; then
    info "dry-run 结束，未做任何修改"
    exit 0
fi
info "等待引擎加载模型（首次约 10~30 秒）…"
ready=0
for _ in $(seq 1 60); do
    if curl -sf -m 3 "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
        ready=1; break
    fi
    sleep 2
done

if [ "$ready" != "1" ]; then
    warn "服务还没起来，看看日志：journalctl -u $SERVICE_NAME -n 50 --no-pager"
    exit 1
fi

curl -s -m 8 "http://127.0.0.1:${PORT}/api/health" | head -c 400; echo
CODE="$(curl -s -o /tmp/_tts_selftest.wav -w '%{http_code}' -m 300 \
    -X POST "http://127.0.0.1:${PORT}/api/tts/file" \
    -H "X-API-Key: ${API_KEY}" -H 'Content-Type: application/json' \
    -d '{"text":"语音服务安装完成，这是一条自检语音。"}')"
SIZE="$(stat -c%s /tmp/_tts_selftest.wav 2>/dev/null || echo 0)"
if [ "$CODE" = "200" ] && [ "$SIZE" -gt 1000 ]; then
    info "✓ 合成成功（HTTP 200，$((SIZE/1024)) KB）→ /tmp/_tts_selftest.wav"
else
    warn "✗ 合成失败（HTTP $CODE，$SIZE 字节），看 journalctl -u $SERVICE_NAME"
fi

cat <<EOF

${C_G}安装完成${C_0}

  网页控制台 : http://<服务器IP>:${PORT}/
  API 地址   : http://<服务器IP>:${PORT}/api/tts/file
  API Key    : ${API_KEY}
  安装目录   : ${INSTALL_DIR}

  常用命令：
    systemctl status  ${SERVICE_NAME}
    systemctl restart ${SERVICE_NAME}      # 改完 config.yaml 重启
    journalctl -u ${SERVICE_NAME} -f       # 实时日志
    bash deploy/linux/uninstall.sh         # 卸载（保留模型）

  把上面的地址和 API Key 填到 AstrBot 插件配置里即可使用。
EOF
