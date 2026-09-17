#!/usr/bin/env bash
# =============================================================================
#  构建「Linux amd64 免安装便携包」——解压即可运行，目标机无需装 Python/依赖
#
#  用法（在 Linux x86_64 上执行，或在 Docker 里执行，见 deploy/README-LINUX.md）：
#     ./deploy/build_linux_bundle.sh --cpu                  # CPU 版（默认，体积最小）
#     ./deploy/build_linux_bundle.sh --cuda 12.8            # CUDA 12.8 版（需要 GPU 驱动）
#     ./deploy/build_linux_bundle.sh --cpu --dry-run        # 只打印步骤，不真正执行
#
#  可选参数：
#     --model  /path/voice.pth      内置你的 RVC 模型
#     --index  /path/voice.index    内置对应的索引文件
#     --assets /path/to/assets      内含 hubert_base.pt 与 rmvpe.pt 的目录（内置可免下载）
#     --name   my-tts-server        产物名称
#     --out-dir dist                输出目录
#     --no-ffmpeg                   不内置 ffmpeg（目标机需自己装）
#     --python 3.12.14              指定 Python 版本
#
#  产物：
#     <out-dir>/<name>/                免安装目录（直接 ./run.sh 启动）
#     <out-dir>/<name>.tar.gz          可直接分发到 Linux amd64 机器的压缩包
# =============================================================================

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVER_DIR="$PROJECT_ROOT/tts-server"

# 注意：Linux 上 tts-with-rvc 依赖 fairseq-fixed，而 PyPI 上只有 cp312 的 manylinux wheel；
# 用 Python 3.11 会退化成从源码编译 fairseq（极难成功），因此这里固定 3.12。
PYTHON_VERSION="3.12.14"
VARIANT="cpu"
CUDA_VERSION=""
MODEL_PATH=""
INDEX_PATH=""
ASSETS_DIR=""
BUNDLE_NAME=""
OUT_DIR="$PROJECT_ROOT/dist"
WITH_FFMPEG=1
DRY_RUN=0

PYTHON_STANDALONE_TAG="20260901"       # astral-sh/python-build-standalone release tag
FFMPEG_URL="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"

# ---------------------------------------------------------------------------
# 输出工具
# ---------------------------------------------------------------------------
log()  { printf '\033[1;32m[build]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[build]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[build]\033[0m %s\n' "$*" >&2; exit 1; }
run()  {
    if [ "$DRY_RUN" = "1" ]; then
        printf '\033[1;36m[dry-run]\033[0m %s\n' "$*"
    else
        "$@"
    fi
}

usage() {
    sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 0
}

# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------
while [ $# -gt 0 ]; do
    case "$1" in
        --cpu)          VARIANT="cpu"; shift ;;
        --cuda)         VARIANT="cuda"; CUDA_VERSION="${2:-12.8}"; shift 2 ;;
        --cuda=*)       VARIANT="cuda"; CUDA_VERSION="${1#*=}"; shift ;;
        --model)        MODEL_PATH="${2:-}"; shift 2 ;;
        --index)        INDEX_PATH="${2:-}"; shift 2 ;;
        --assets)       ASSETS_DIR="${2:-}"; shift 2 ;;
        --name)         BUNDLE_NAME="${2:-}"; shift 2 ;;
        --out-dir)      OUT_DIR="${2:-}"; shift 2 ;;
        --python)       PYTHON_VERSION="${2:-}"; shift 2 ;;
        --no-ffmpeg)    WITH_FFMPEG=0; shift ;;
        --dry-run)      DRY_RUN=1; shift ;;
        -h|--help)      usage ;;
        *)              die "未知参数: $1（用 --help 查看用法）" ;;
    esac
done

[ -n "$BUNDLE_NAME" ] || BUNDLE_NAME="tts-server-linux-amd64-$VARIANT"
BUNDLE_DIR="$OUT_DIR/$BUNDLE_NAME"
CACHE_DIR="$PROJECT_ROOT/.build-cache"

# ---------------------------------------------------------------------------
# 环境检查
# ---------------------------------------------------------------------------
[ -f "$SERVER_DIR/main.py" ] || die "找不到 $SERVER_DIR/main.py，请在项目根目录执行本脚本"

if [ "$DRY_RUN" != "1" ]; then
    OS="$(uname -s)"
    ARCH="$(uname -m)"
    [ "$OS" = "Linux" ] || die "本脚本必须在 Linux 上运行（当前: $OS）。
  - Windows 用户请在 Docker 里构建：见 deploy/README-LINUX.md
  - PyInstaller 等工具无法从 Windows/macOS 交叉编译出 Linux 可执行文件"
    case "$ARCH" in
        x86_64|amd64) ;;
        *) die "只支持 x86_64/amd64（当前: $ARCH）" ;;
    esac
    for tool in curl tar python3; do
        command -v "$tool" >/dev/null 2>&1 || die "缺少命令: $tool"
    done
    # pyworld（tts-with-rvc 依赖）在 Linux 上没有预编译 wheel，需要现场编译
    if ! command -v cc >/dev/null 2>&1 && ! command -v gcc >/dev/null 2>&1; then
        warn "未找到 C 编译器（gcc/cc）。tts-with-rvc 依赖的 pyworld 需要从源码编译，
      请先安装：apt install -y build-essential 或 yum install -y gcc gcc-c++
      或者使用 Docker 构建：deploy/Dockerfile.build"
    fi
fi

PY_ASSET="cpython-${PYTHON_VERSION}+${PYTHON_STANDALONE_TAG}-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
PY_ASSET_FALLBACK="cpython-${PYTHON_VERSION}+${PYTHON_STANDALONE_TAG}-x86_64-unknown-linux-gnu-install_only.tar.gz"
PY_BASE="https://github.com/astral-sh/python-build-standalone/releases/download/${PYTHON_STANDALONE_TAG}"
PY_URL="$PY_BASE/$PY_ASSET"

log "变体        : $VARIANT${CUDA_VERSION:+ (CUDA $CUDA_VERSION)}"
log "Python      : $PYTHON_VERSION（独立运行时，随包分发）"
log "产物目录    : $BUNDLE_DIR"
log "压缩包      : $BUNDLE_DIR.tar.gz"
[ "$WITH_FFMPEG" = "1" ] && log "内置 ffmpeg : 是" || log "内置 ffmpeg : 否"
[ -n "$MODEL_PATH" ] && log "内置模型    : $MODEL_PATH"
echo

# ---------------------------------------------------------------------------
# 1) 准备目录
# ---------------------------------------------------------------------------
log "1/9 准备目录"
run rm -rf "$BUNDLE_DIR"
run mkdir -p "$BUNDLE_DIR"/{app,bin,models,output} "$CACHE_DIR"

# ---------------------------------------------------------------------------
# 2) 下载独立 Python 运行时
# ---------------------------------------------------------------------------
log "2/9 下载独立 Python 运行时（python-build-standalone）"
if [ ! -f "$CACHE_DIR/$PY_ASSET" ]; then
    rm -f "$CACHE_DIR/$PY_ASSET"
    if [ "$DRY_RUN" = "1" ]; then
        run curl -fL --retry 3 -o "$CACHE_DIR/$PY_ASSET" "$PY_URL"
    elif ! curl -fL --retry 3 -o "$CACHE_DIR/$PY_ASSET" "$PY_URL"; then
        rm -f "$CACHE_DIR/$PY_ASSET"
        warn "stripped 版本不存在，改用完整版 Python 运行时"
        PY_ASSET="$PY_ASSET_FALLBACK"
        run curl -fL --retry 3 -o "$CACHE_DIR/$PY_ASSET" "$PY_BASE/$PY_ASSET"
    fi
else
    log "    使用缓存: $CACHE_DIR/$PY_ASSET"
fi
run tar -xzf "$CACHE_DIR/$PY_ASSET" -C "$BUNDLE_DIR"
run rm -rf "$BUNDLE_DIR/python/lib/python${PYTHON_VERSION%.*}/test"
BUNDLED_PY="$BUNDLE_DIR/python/bin/python3"

# ---------------------------------------------------------------------------
# 3) 安装依赖（含 tts-with-rvc）
# ---------------------------------------------------------------------------
log "3/9 安装依赖（这一步最慢，需要下载 2GB 左右）"
# python-build-standalone 的 sysconfig 默认 CC/CXX 是 clang/clang++；
# 构建机上没有 clang 时（Debian/Ubuntu 默认只装 gcc），需要现场编译的包（如 pyworld）
# 会直接失败，这里显式回退到 gcc/g++。
if ! command -v clang++ >/dev/null 2>&1 && command -v g++ >/dev/null 2>&1; then
    export CC="${CC:-$(command -v gcc || command -v cc)}"
    export CXX="${CXX:-$(command -v g++)}"
    log "    使用编译器: CC=$CC CXX=$CXX（构建机无 clang）"
fi
if [ "$DRY_RUN" != "1" ]; then
    "$BUNDLED_PY" -m pip --version >/dev/null 2>&1 || "$BUNDLED_PY" -m ensurepip --upgrade
fi
run "$BUNDLED_PY" -m pip install --upgrade pip

if [ "$VARIANT" = "cpu" ]; then
    run "$BUNDLED_PY" -m pip install \
        torch torchaudio --index-url "${TORCH_INDEX_URL:-https://download.pytorch.org/whl/cpu}"
else
    INDEX="https://download.pytorch.org/whl/cu${CUDA_VERSION//./}"
    run "$BUNDLED_PY" -m pip install torch torchaudio --index-url "${TORCH_INDEX_URL:-$INDEX}"
fi
run "$BUNDLED_PY" -m pip install -r "$SERVER_DIR/requirements.txt"
run "$BUNDLED_PY" -m pip cache purge || true

# ---------------------------------------------------------------------------
# 4) 拷贝服务端代码
# ---------------------------------------------------------------------------
log "4/9 拷贝服务端代码"
# 直接遍历源码目录里的 *.py，新增模块（如 webui.py）会自动带上
for f in "$SERVER_DIR"/*.py; do
    run cp "$f" "$BUNDLE_DIR/app/$(basename "$f")"
done
if [ -d "$SERVER_DIR/static" ]; then
    run cp -r "$SERVER_DIR/static" "$BUNDLE_DIR/app/static"
fi
run cp "$SERVER_DIR/config.yaml" "$BUNDLE_DIR/config.yaml"

# ---------------------------------------------------------------------------
# 5) 内置 ffmpeg（静态版，避免目标机安装）
# ---------------------------------------------------------------------------
if [ "$WITH_FFMPEG" = "1" ]; then
    log "5/9 内置 ffmpeg 静态二进制"
    FFMPEG_TAR="$CACHE_DIR/ffmpeg-release-amd64-static.tar.xz"
    if [ ! -f "$FFMPEG_TAR" ]; then
        run curl -fL --retry 3 -o "$FFMPEG_TAR" "$FFMPEG_URL"
    else
        log "    使用缓存: $FFMPEG_TAR"
    fi
    if [ "$DRY_RUN" != "1" ]; then
        TMP_X="$(mktemp -d)"
        tar -xJf "$FFMPEG_TAR" -C "$TMP_X"
        find "$TMP_X" -name ffmpeg -type f -exec cp {} "$BUNDLE_DIR/bin/ffmpeg" \;
        rm -rf "$TMP_X"
        chmod +x "$BUNDLE_DIR/bin/ffmpeg"
    else
        run tar -xJf "$FFMPEG_TAR" -C "<临时目录>"
    fi
else
    log "5/9 跳过 ffmpeg（目标机需自行安装）"
fi

# ---------------------------------------------------------------------------
# 6) Net 资源与模型
# ---------------------------------------------------------------------------
log "6/9 处理 hubert/rmvpe 权重与 RVC 模型"
if [ -n "$ASSETS_DIR" ]; then
    [ -d "$ASSETS_DIR" ] || die "--assets 目录不存在: $ASSETS_DIR"
    for asset in hubert_base.pt rmvpe.pt; do
        if [ -f "$ASSETS_DIR/$asset" ]; then
            run cp "$ASSETS_DIR/$asset" "$BUNDLE_DIR/$asset"
        else
            warn "在 $ASSETS_DIR 中找不到 $asset，将在首次推理时自动下载（约 350MB）"
        fi
    done
else
    warn "未指定 --assets：hubert_base.pt / rmvpe.pt 会在第一次推理时自动下载（约 350MB）。
      如有现成文件，请用 --assets /path/to/dir 内置，避免目标机联网下载。"
fi

if [ -n "$MODEL_PATH" ]; then
    [ -f "$MODEL_PATH" ] || die "模型文件不存在: $MODEL_PATH"
    run cp "$MODEL_PATH" "$BUNDLE_DIR/models/$(basename "$MODEL_PATH")"
fi
if [ -n "$INDEX_PATH" ]; then
    [ -f "$INDEX_PATH" ] || die "索引文件不存在: $INDEX_PATH"
    run cp "$INDEX_PATH" "$BUNDLE_DIR/models/$(basename "$INDEX_PATH")"
fi

# ---------------------------------------------------------------------------
# 7) 生成启动脚本 / 说明 / 版本信息，并写回 config.yaml
# ---------------------------------------------------------------------------
log "7/9 生成 run.sh / README / VERSION（并同步 config.yaml 的模型路径）"

run tee "$BUNDLE_DIR/run.sh" >/dev/null <<'RUNSH'
#!/usr/bin/env bash
# 免安装启动脚本：直接运行本文件即可（无需 root、无需安装任何依赖）
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

# 运行时可调参数（不设也有默认值）
# 推理线程数：实测 8 线程基本吃饱（再往上收益很小），小机器自动降级
if [ -z "${OMP_NUM_THREADS:-}" ]; then
    CORES="$(nproc 2>/dev/null || echo 4)"
    [ "$CORES" -gt 8 ] && CORES=8
    export OMP_NUM_THREADS="$CORES"
fi
export PYTHONNOUSERSITE=1
export PATH="$HERE/bin:$PATH"                          # 使用内置 ffmpeg
export TTS_SERVER_CONFIG="${TTS_SERVER_CONFIG:-$HERE/config.yaml}"
export PYTHONUNBUFFERED=1

if [ ! -x "$HERE/python/bin/python3" ]; then
    echo "找不到内置 Python 运行时：$HERE/python/bin/python3" >&2
    exit 1
fi

LOGDIR="${TTS_SERVER_LOG_DIR:-$HERE/logs}"
mkdir -p "$LOGDIR" "$HERE/output"

exec "$HERE/python/bin/python3" -s "$HERE/app/main.py" --config "$HERE/config.yaml" "$@"
RUNSH

if [ "$DRY_RUN" != "1" ]; then
    chmod +x "$BUNDLE_DIR/run.sh"
fi

# 附带一个免依赖的测试客户端，方便在服务器上直接验证
if [ -f "$PROJECT_ROOT/tools/tts_client.py" ]; then
    run mkdir -p "$BUNDLE_DIR/tools"
    run cp "$PROJECT_ROOT/tools/tts_client.py" "$BUNDLE_DIR/tools/tts_client.py"
    run cp "$PROJECT_ROOT/tools/voice_tts.py" "$BUNDLE_DIR/tools/voice_tts.py"
    run cp "$PROJECT_ROOT/tools/sample_texts.txt" "$BUNDLE_DIR/tools/sample_texts.txt"
    run tee "$BUNDLE_DIR/tools/check.sh" >/dev/null <<'CHECKSH'
#!/usr/bin/env bash
# 一键自检：健康检查 + 合成一句 + 并发压测
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE/.."
PY="$PWD/python/bin/python3"
"$PY" tools/tts_client.py --url "http://127.0.0.1:${PORT:-8080}" health
"$PY" tools/tts_client.py --url "http://127.0.0.1:${PORT:-8080}" say "你好，这是语音测试。"
"$PY" tools/tts_client.py --url "http://127.0.0.1:${PORT:-8080}" bench --count 4 -c 1 --unique
CHECKSH
    run chmod +x "$BUNDLE_DIR/tools/check.sh"
fi

run tee "$BUNDLE_DIR/README-使用说明.md" >/dev/null <<'BUNDLEREADME'
# TTS-with-RVC 语音处理端（Linux amd64 免安装版）

## 直接运行

```bash
tar -xzf tts-server-linux-amd64-*.tar.gz
cd tts-server-linux-amd64-*
./run.sh                # 前台启动，默认监听 0.0.0.0:8080
```

后台运行：

```bash
nohup ./run.sh > logs/server.log 2>&1 &
```

## 需要改的配置

编辑同目录的 `config.yaml`：

- `rvc.model`：RVC 模型文件名（放在同目录 `models/` 下，可写绝对路径）
- `security.api_key`：留空表示不鉴权；填了之后 AstrBot 插件也要填同一个值
- `storage.expire_minutes`：音频保留时间，默认 10 分钟
- `queue.max_concurrent`：并发上限（RVC 推理本身是串行的，靠它排队）

改完重启即可（`./run.sh --check-config` 可只校验配置不启动）。

## 环境要求

- Linux x86_64，**glibc >= 2.28**（CentOS/RHEL 8+、Debian 10+、Ubuntu 18.10+）
- 无需 root 权限
- 无需安装 Python、pip、ffmpeg（已内置）
- CPU 版：无其他要求；CUDA 版：需要目标机装好 NVIDIA 驱动（>= 对应 CUDA 版本）

## 目录说明

```
run.sh              启动脚本（会用内置 Python + 内置 ffmpeg）
config.yaml         唯一配置文件
app/                服务端代码
python/             内置 Python 运行时与全部依赖
bin/ffmpeg          内置 ffmpeg（无需系统安装）
models/             RVC 模型（.pth / .index）
hubert_base.pt      推理权重（首次运行下载的，若已内置则直接用）
rmvpe.pt            音高提取权重
output/             生成的音频，按 config.yaml 的 expire_minutes 自动清理
logs/               日志目录
tools/tts_client.py 免依赖测试客户端（同样用内置 Python 运行）
tools/check.sh      一键自检：健康检查 + 合成一句 + 小压测
```

## 验证

```bash
# 只检查配置（不启动服务）
./run.sh --check-config

# 启动服务（前台）
./run.sh

# 另开一个终端做自检（不需要装 Python/curl）
./tools/check.sh
```

也可以手动用内置 Python 跑测试客户端：

```bash
./python/bin/python3 tools/tts_client.py health
./python/bin/python3 tools/tts_client.py say "你好，这是语音测试。"
./python/bin/python3 tools/tts_client.py bench --count 8 -c 2 --unique
```

## systemd 常驻（可选）

```ini
[Unit]
Description=TTS-with-RVC voice service
After=network-online.target

[Service]
WorkingDirectory=/opt/tts-server
ExecStart=/opt/tts-server/run.sh
Restart=always
RestartSec=5
# 让服务只使用 4 个 CPU 核心（避免抢占其它服务）
Environment=OMP_NUM_THREADS=4
# GPU 版可指定显卡
# Environment=CUDA_VISIBLE_DEVICES=0

[Install]
WantedBy=multi-user.target
```
BUNDLEREADME

# 同步 config.yaml：输出目录、模型路径、鉴权示例
if [ "$DRY_RUN" != "1" ]; then
    "$BUNDLED_PY" - "$BUNDLE_DIR/config.yaml" "$MODEL_PATH" "$INDEX_PATH" <<'PYPATCH'
import sys
from pathlib import Path

import yaml

config_path = Path(sys.argv[1])
model = sys.argv[2] if len(sys.argv) > 2 else ""
index = sys.argv[3] if len(sys.argv) > 3 else ""

data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
data.setdefault("storage", {})["output_dir"] = "./output"
if model:
    data.setdefault("rvc", {})["model"] = Path(model).name
if index:
    data.setdefault("rvc", {})["index"] = Path(index).name
data.setdefault("rvc", {}).setdefault("model_dir", "./models")
config_path.write_text(
    yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False),
    encoding="utf-8",
)
print(f"[build]    已写回配置: {config_path}")
if model:
    print(f"[build]    rvc.model = {Path(model).name}")
PYPATCH
fi

if [ "$DRY_RUN" != "1" ]; then
    PYTORCH_VER="$("$BUNDLED_PY" -c 'import torch; print(torch.__version__)' 2>/dev/null || echo unknown)"
    {
        echo "bundle_name: $BUNDLE_NAME"
        echo "variant: $VARIANT${CUDA_VERSION:+ (cuda $CUDA_VERSION)}"
        echo "build_time: $(date '+%Y-%m-%d %H:%M:%S %Z')"
        echo "build_host: $(uname -srm)"
        echo "python: $PYTHON_VERSION"
        echo "torch: $PYTORCH_VER"
        echo "ffmpeg_bundled: $WITH_FFMPEG"
        echo "model: ${MODEL_PATH:-未内置}"
    } > "$BUNDLE_DIR/VERSION.txt"
fi

# ---------------------------------------------------------------------------
# 8) 自检
# ---------------------------------------------------------------------------
log "8/9 自检（导入依赖 + 校验配置）"
if [ "$DRY_RUN" != "1" ]; then
    "$BUNDLED_PY" - <<'PYCHECK'
import importlib
import sys

missing = []
for name in ("torch", "fastapi", "uvicorn", "yaml", "tts_with_rvc"):
    try:
        module = importlib.import_module(name)
        version = getattr(module, "__version__", "")
        print(f"[build]    OK  {name} {version}")
    except Exception as exc:  # noqa: BLE001
        missing.append(f"{name}: {exc}")
if missing:
    print("[build]    依赖导入失败：", file=sys.stderr)
    for item in missing:
        print("  -", item, file=sys.stderr)
    raise SystemExit(1)
PYCHECK

    if [ -f "$BUNDLE_DIR/bin/ffmpeg" ]; then
        "$BUNDLE_DIR/bin/ffmpeg" -version | head -n 1
    fi

    if (cd "$BUNDLE_DIR" && TTS_SERVER_CONFIG="$BUNDLE_DIR/config.yaml" \
        "$BUNDLED_PY" -s "$BUNDLE_DIR/app/main.py" --check-config >/tmp/bundle-check.log 2>&1); then
        log "    配置校验通过"
    else
        warn "配置校验未通过（通常是还没放 RVC 模型，属正常）："
        sed 's/^/    /' /tmp/bundle-check.log | tail -n 5
    fi
fi

# ---------------------------------------------------------------------------
# 9) 打包
# ---------------------------------------------------------------------------
log "9/9 打包为 tar.gz"
run mkdir -p "$OUT_DIR"
if [ "$DRY_RUN" != "1" ]; then
    tar -czf "$BUNDLE_DIR.tar.gz" -C "$OUT_DIR" "$BUNDLE_NAME"

    echo
    log "构建完成 ✅"
    log "目录包  : $BUNDLE_DIR"
    log "压缩包  : $BUNDLE_DIR.tar.gz"
    log "大小    : $(du -sh "$BUNDLE_DIR" | cut -f1)（解压后） / $(du -sh "$BUNDLE_DIR.tar.gz" | cut -f1)（压缩包）"
    echo
    log "分发与运行："
    echo "    scp $BUNDLE_DIR.tar.gz user@server:/opt/"
    echo "    ssh user@server 'cd /opt && tar -xzf $(basename "$BUNDLE_DIR.tar.gz") && cd $BUNDLE_NAME && ./run.sh'"
else
    log "dry-run 结束，以上为将要执行的全部步骤"
fi
