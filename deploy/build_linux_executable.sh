#!/usr/bin/env bash
# =============================================================================
#  用 PyInstaller 把语音处理端打成 Linux amd64「可执行文件」（单目录模式）
#
#  用法（必须在 Linux x86_64 + Python 3.12 上执行）：
#     ./deploy/build_linux_executable.sh --cpu
#     ./deploy/build_linux_executable.sh --cuda 12.8
#     ./deploy/build_linux_executable.sh --cpu --dry-run
#
#  产物：dist/tts-server/tts-server     （可执行文件）
#        dist/tts-server/               （整个目录一起分发）
#        dist/tts-server-linux-amd64-<variant>-exe.tar.gz
#
#  重要说明：
#  1. PyInstaller **不能跨平台编译**。Windows/macOS 上无法产出 Linux 可执行文件，
#     请在 Linux 机器上构建，或用 Docker（见 deploy/Dockerfile.build）。
#  2. 这套依赖（torch + fairseq + numba…）体积很大，产物约 2~4GB，
#     且首次启动会比便携包慢一点。
#  3. 如果打包后启动报缺模块，绝大多数情况是某个动态导入没被收集：
#     把报错里的模块名加到 deploy/tts-server.spec 的 hiddenimports 即可。
#  4. 只要「能跑起来」而不追求单个可执行文件，更推荐免安装便携包方案：
#     ./deploy/build_linux_bundle.sh  （更稳、启动更快）
# =============================================================================

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVER_DIR="$PROJECT_ROOT/tts-server"

VARIANT="cpu"
CUDA_VERSION=""
PYTHON_BIN="${PYTHON_BIN:-python3.12}"
MODEL_PATH=""
INDEX_PATH=""
DRY_RUN=0
OUT_DIR="$PROJECT_ROOT/dist"

log()  { printf '\033[1;32m[exe]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[exe]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[exe]\033[0m %s\n' "$*" >&2; exit 1; }
run()  {
    if [ "$DRY_RUN" = "1" ]; then
        printf '\033[1;36m[dry-run]\033[0m %s\n' "$*"
    else
        "$@"
    fi
}

while [ $# -gt 0 ]; do
    case "$1" in
        --cpu)      VARIANT="cpu"; shift ;;
        --cuda)     VARIANT="cuda"; CUDA_VERSION="${2:-12.8}"; shift 2 ;;
        --cuda=*)   VARIANT="cuda"; CUDA_VERSION="${1#*=}"; shift ;;
        --model)    MODEL_PATH="${2:-}"; shift 2 ;;
        --index)    INDEX_PATH="${2:-}"; shift 2 ;;
        --python)   PYTHON_BIN="${2:-}"; shift 2 ;;
        --out-dir)  OUT_DIR="${2:-}"; shift 2 ;;
        --dry-run)  DRY_RUN=1; shift ;;
        -h|--help)  sed -n '2,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)          die "未知参数: $1" ;;
    esac
done

[ -f "$SERVER_DIR/main.py" ] || die "找不到 $SERVER_DIR/main.py"

if [ "$DRY_RUN" != "1" ]; then
    [ "$(uname -s)" = "Linux" ] || die "必须在 Linux 上运行（当前: $(uname -s)）。
  Windows/macOS 用户请用 Docker 构建：见 deploy/README-LINUX.md"
    [ "$(uname -m)" = "x86_64" ] || die "只支持 x86_64"
    command -v "$PYTHON_BIN" >/dev/null 2>&1 || die "找不到 $PYTHON_BIN（需要 Python 3.12，可用 --python 指定）
  原因：Linux 上 tts-with-rvc 依赖的 fairseq-fixed 只有 cp312 的 wheel"

    "$PYTHON_BIN" - <<'PYVER'
import sys

if sys.version_info[:2] != (3, 12):
    raise SystemExit(
        f"需要 Python 3.12（当前 {sys.version.split()[0]}）。\n"
        "Linux 上 tts-with-rvc 依赖 fairseq-fixed，它只提供 cp312 的 manylinux wheel。",
    )
print(f"[exe] Python {sys.version.split()[0]} OK")
PYVER
fi

BUILD_ENV="$PROJECT_ROOT/.build-exe-venv"

log "1/6 创建构建用虚拟环境（$BUILD_ENV）"
run "$PYTHON_BIN" -m venv "$BUILD_ENV"
PIP="$BUILD_ENV/bin/pip"
run "$PIP" install --upgrade pip

log "2/6 安装依赖 + PyInstaller（最慢的一步）"
# 部分依赖（pyworld）需要现场编译；独立/精简 Python 默认可能指向 clang
if ! command -v clang++ >/dev/null 2>&1 && command -v g++ >/dev/null 2>&1; then
    export CC="${CC:-$(command -v gcc || command -v cc)}"
    export CXX="${CXX:-$(command -v g++)}"
    log "    使用编译器: CC=$CC CXX=$CXX（构建机无 clang）"
fi
if [ "$VARIANT" = "cpu" ]; then
    run "$PIP" install torch torchaudio --index-url https://download.pytorch.org/whl/cpu
else
    run "$PIP" install torch torchaudio --index-url "https://download.pytorch.org/whl/cu${CUDA_VERSION//./}"
fi
run "$PIP" install -r "$SERVER_DIR/requirements.txt"
run "$PIP" install "pyinstaller>=6.6" "pyinstaller-hooks-contrib>=2024.6"

log "3/6 清理旧产物"
run rm -rf "$PROJECT_ROOT/build" "$OUT_DIR/tts-server"

log "4/6 运行 PyInstaller（分析 torch/fairseq 需要几分钟）"
run env PYTHONPATH="$SERVER_DIR" \
    "$BUILD_ENV/bin/pyinstaller" "$SCRIPT_DIR/tts-server.spec" \
    --noconfirm --distpath "$OUT_DIR" --workpath "$PROJECT_ROOT/build"

log "5/6 补齐运行所需文件（config.yaml / models / ffmpeg）"
TARGET="$OUT_DIR/tts-server"
run cp "$SERVER_DIR/config.yaml" "$TARGET/config.yaml"
run mkdir -p "$TARGET/models" "$TARGET/output" "$TARGET/bin"
[ -n "$MODEL_PATH" ] && run cp "$MODEL_PATH" "$TARGET/models/"
[ -n "$INDEX_PATH" ] && run cp "$INDEX_PATH" "$TARGET/models/"

if [ "${WITH_FFMPEG:-1}" = "1" ] && [ "$DRY_RUN" != "1" ]; then
    if command -v ffmpeg >/dev/null 2>&1; then
        cp "$(command -v ffmpeg)" "$TARGET/bin/ffmpeg"
        log "    已复制系统 ffmpeg 到 bin/"
    else
        warn "构建机没有 ffmpeg，未内置。可用 --with-ffmpeg 静态包，或让目标机自行安装"
    fi
fi

run tee "$TARGET/run.sh" >/dev/null <<'RUNSH'
#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-4}"
export PATH="$HERE/bin:$PATH"
exec "$HERE/tts-server" --config "$HERE/config.yaml" "$@"
RUNSH
[ "$DRY_RUN" != "1" ] && chmod +x "$TARGET/run.sh"

log "6/6 自检并打包"
if [ "$DRY_RUN" != "1" ]; then
    if (cd "$TARGET" && ./tts-server --check-config >/tmp/exe-check.log 2>&1); then
        log "    可执行文件启动正常，配置校验通过"
    else
        warn "    自检未通过（没有放 RVC 模型时属正常）："
        sed 's/^/    /' /tmp/exe-check.log | tail -n 5
    fi
    tar -czf "$OUT_DIR/tts-server-linux-amd64-$VARIANT-exe.tar.gz" -C "$OUT_DIR" tts-server
    log "完成 ✅"
    log "可执行文件 : $TARGET/tts-server"
    log "整个目录   : $TARGET（$(du -sh "$TARGET" | cut -f1)）"
    log "压缩包     : $OUT_DIR/tts-server-linux-amd64-$VARIANT-exe.tar.gz（$(du -sh "$OUT_DIR/tts-server-linux-amd64-$VARIANT-exe.tar.gz" | cut -f1)）"
    echo
    log "运行方式：cd $TARGET && ./run.sh"
else
    log "dry-run 结束"
fi
