#!/usr/bin/env bash
# =============================================================================
# 部署自检：把「能不能跑」的每一层都打印出来，出问题时一眼定位
#
#   bash verify.sh
# =============================================================================
set -uo pipefail

APP_DIR="${APP_DIR:-/opt/tts-server}"
# 端口以 config.yaml 里的 server.port 为准，不写死
PORT="${PORT:-$(grep -E '^\s+port:' "$APP_DIR/config.yaml" 2>/dev/null | head -1 | sed -E 's/[^0-9]*([0-9]+).*/\1/')}"
PORT="${PORT:-50051}"
KEY="${KEY:-$(grep -E '^\s*api_key:' "$APP_DIR/config.yaml" 2>/dev/null | head -1 | sed -E 's/.*"(.*)".*/\1/')}"

ok()   { printf '  \033[1;32m[OK]\033[0m   %s\n' "$*"; }
bad()  { printf '  \033[1;31m[FAIL]\033[0m %s\n' "$*"; }
warn() { printf '  \033[1;33m[WARN]\033[0m %s\n' "$*"; }

echo "=== 1. 系统 ==="
echo "  内核: $(uname -r)   发行版: $(. /etc/os-release; echo "$PRETTY_NAME")"
echo "  内存: $(free -m | awk '/^Mem:/{print $2" MB (可用 "$7" MB)"}')"
echo "  磁盘: $(df -h /opt | awk 'NR==2{print $4" 可用"}')"

echo "=== 2. 显卡 ==="
if command -v nvidia-smi >/dev/null 2>&1; then
    nvidia-smi --query-gpu=index,name,driver_version,memory.total,memory.used,temperature.gpu \
        --format=csv,noheader | sed 's/^/  /'
else
    bad "没有 nvidia-smi"
fi

echo "=== 3. ffmpeg ==="
if command -v ffmpeg >/dev/null 2>&1; then
    ok "$(ffmpeg -version 2>/dev/null | head -1)"
else
    bad "未安装 ffmpeg（RVC 读写音频需要）"
fi

echo "=== 4. Python 环境 ==="
PY="$APP_DIR/venv/bin/python"
if [ -x "$PY" ]; then
    "$PY" -V | sed 's/^/  /'
    APP_DIR="$APP_DIR" "$PY" - <<'PY' 2>&1 | sed 's/^/  /'
import os, sys
sys.path.insert(0, os.path.join(os.environ["APP_DIR"], "app"))   # 让 import webui 生效
import torch, torchaudio
print("torch", torch.__version__, "| torchaudio", torchaudio.__version__)
print("cuda_available:", torch.cuda.is_available())
if torch.cuda.is_available():
    major, minor = torch.cuda.get_device_capability(0)
    arches = [a for a in torch.cuda.get_arch_list() if a.startswith("sm_")]
    print("device:", torch.cuda.get_device_name(0), "| capability: sm_%d%d" % (major, minor))
    print("arch_list:", arches)
    # CUDA 二进制兼容：为 X.y 编译的 cubin 可以在 X.z (z>=y) 上跑
    exact = "sm_%d%d" % (major, minor)
    matched = None
    for a in arches:
        d = a[3:]
        if d.isdigit() and len(d) >= 2 and int(d[:-1]) == major and int(d[-1]) <= minor:
            if matched is None or int(d[-1]) > int(matched[3:-1]):
                matched = a
    if exact in arches:
        print("当前 torch 支持这张卡: 是（精确内核 %s）" % exact)
    elif matched:
        print("当前 torch 支持这张卡: 是（复用 %s 内核）" % matched)
    else:
        print("当前 torch 支持这张卡: 否 —— Pascal 老卡请用 cu121：bash install_deps.sh")
for m in ("fastapi", "uvicorn", "yaml", "edge_tts", "fairseq", "tts_with_rvc", "webui"):
    try:
        __import__(m); print(f"import {m:<12} ok")
    except Exception as e:
        print(f"import {m:<12} FAIL {type(e).__name__}: {e}")
PY
else
    bad "虚拟环境不存在：$PY"
fi

echo "=== 5. 配置 ==="
if [ -f "$APP_DIR/config.yaml" ]; then
    "$PY" "$APP_DIR/app/main.py" --config "$APP_DIR/config.yaml" --check-config 2>&1 | tail -3 | sed 's/^/  /'
else
    bad "缺少 $APP_DIR/config.yaml"
fi

echo "=== 6. 服务 ==="
systemctl is-active tts-server >/dev/null 2>&1 && ok "systemd: active" || warn "systemd: 未运行"
if curl -s -m 5 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
    ok "HTTP /api/health 可访问"
    curl -s -m 8 "http://127.0.0.1:$PORT/api/gpu" | head -c 400; echo
else
    bad "HTTP 无法访问 http://127.0.0.1:$PORT"
fi

echo "=== 7. 端到端合成 ==="
if [ -n "$KEY" ]; then
    code=$(curl -s -o /tmp/_tts_test.wav -w '%{http_code}' -m 120 \
        -X POST "http://127.0.0.1:$PORT/api/tts/file" \
        -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
        -d '{"text":"语音服务自检，一切正常。"}')
    size=$(stat -c%s /tmp/_tts_test.wav 2>/dev/null || echo 0)
    if [ "$code" = "200" ] && [ "$size" -gt 1000 ]; then
        ok "合成成功（HTTP $code，$((size/1024)) KB）→ /tmp/_tts_test.wav"
        command -v ffprobe >/dev/null 2>&1 && ffprobe -v error -show_entries format=duration \
            -of default=nw=1:nk=1 /tmp/_tts_test.wav | sed 's/^/  音频时长: /;s/$/ 秒/'
    else
        bad "合成失败（HTTP $code，$size 字节）"
    fi
else
    warn "读不到 api_key，跳过合成测试"
fi
