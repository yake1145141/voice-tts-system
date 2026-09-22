#!/usr/bin/env bash
# =============================================================================
#  给已经手动部署好的语音服务端补一个 systemd 服务
#
#  适用场景：
#    * 用 Linux 免安装便携包 / 手动解压的方式装的 —— 这种装法不包含 systemd 单元，
#      所以 `systemctl status tts-server` 会报 Unit not found
#    * 服务现在是靠 nohup / screen / 手敲 python main.py 跑着的
#
#  用法：
#    sudo bash install-systemd.sh                      # 自动探测安装目录
#    sudo bash install-systemd.sh /opt/tts-server      # 手动指定
#    sudo bash install-systemd.sh /opt/tts-server 8080 # 再指定端口
#
#  脚本会：找到 python 解释器与 config.yaml → 停掉手工起的进程 →
#          生成并启用 tts-server.service → 启动并做一次探活
# =============================================================================
set -Eeuo pipefail

APP_DIR="${1:-}"
PORT="${2:-}"
SERVICE_NAME="tts-server"

log()  { printf '\033[1;32m[systemd]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[systemd]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[systemd]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "请用 root 运行（sudo bash $0）"

# ------------------------------------------------------------------ 定位安装目录
find_app_dir() {
    # 1) 已经在跑的进程，直接看它的命令行
    local pid cmd
    pid="$(pgrep -f 'app/main\.py' | head -1 || true)"
    if [ -n "$pid" ]; then
        cmd="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
        local p
        for p in $cmd; do
            case "$p" in
                */app/main.py)
                    echo "${p%/app/main.py}"
                    return 0
                    ;;
            esac
        done
    fi
    # 2) 常见位置
    local d
    for d in /opt/tts-server /root/tts-server /opt/voice-tts-system/tts-server; do
        [ -f "$d/app/main.py" ] && { echo "$d"; return 0; }
    done
    # 3) 全盘找一下（只找两层，避免太慢）
    d="$(find /opt /root /srv /home -maxdepth 4 -type f -path '*/app/main.py' 2>/dev/null | head -1 || true)"
    [ -n "$d" ] && { echo "${d%/app/main.py}"; return 0; }
    return 1
}

if [ -z "$APP_DIR" ]; then
    APP_DIR="$(find_app_dir || true)"
fi
[ -n "$APP_DIR" ] || die "找不到服务端安装目录，请手动指定：sudo bash $0 /opt/tts-server"
APP_DIR="$(cd "$APP_DIR" && pwd)"

log "安装目录: $APP_DIR"
[ -f "$APP_DIR/app/main.py" ] || die "$APP_DIR 下面没有 app/main.py，不像服务端目录"

CONFIG="$APP_DIR/config.yaml"
[ -f "$CONFIG" ] || die "找不到配置文件 $CONFIG"

# ------------------------------------------------------------------ 找 python 解释器
# 便携包是 python/bin/python3，venv 装法是 venv/bin/python，两种都得认
PY=""
for cand in \
    "$APP_DIR/venv/bin/python" \
    "$APP_DIR/.venv/bin/python" \
    "$APP_DIR/python/bin/python3" \
    "$APP_DIR/python/bin/python3.12" \
    "$APP_DIR/runtime/bin/python3"
do
    [ -x "$cand" ] && { PY="$cand"; break; }
done
if [ -z "$PY" ]; then
    for cand in "$APP_DIR"/python/bin/python3.*; do
        [ -x "$cand" ] && { PY="$cand"; break; }
    done
fi
if [ -z "$PY" ]; then
    PY="$(command -v python3 || true)"
    [ -n "$PY" ] && warn "没找到虚拟环境，将使用系统 python3：$PY"
fi
[ -n "$PY" ] || die "找不到可用的 python 解释器"
log "python  : $PY  ($("$PY" -V 2>&1))"

# ------------------------------------------------------------------ 端口
if [ -z "$PORT" ]; then
    PORT="$(grep -E '^\s+port:' "$CONFIG" 2>/dev/null | head -1 | sed -E 's/[^0-9]*([0-9]+).*/\1/' || true)"
fi
PORT="${PORT:-8080}"
log "端口    : $PORT"

# ------------------------------------------------------------------ 停掉手工起的进程
PIDS="$(pgrep -f 'app/main\.py' | tr '\n' ' ' || true)"
if [ -n "${PIDS// /}" ]; then
    log "停掉手工启动的进程: $PIDS"
    # shellcheck disable=SC2086
    kill $PIDS 2>/dev/null || true
    sleep 3
    # shellcheck disable=SC2086
    kill -9 $PIDS 2>/dev/null || true
fi

# ------------------------------------------------------------------ 写单元文件
log "写入 /etc/systemd/system/${SERVICE_NAME}.service"
cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=TTS-with-RVC voice service (Edge TTS + RVC) for AstrBot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
Group=root
WorkingDirectory=${APP_DIR}

Environment=PYTHONUNBUFFERED=1
Environment=PYTHONIOENCODING=utf-8
Environment=HF_HUB_OFFLINE=1
Environment=HF_HUB_DISABLE_TELEMETRY=1

ExecStart=${PY} ${APP_DIR}/app/main.py --config ${CONFIG}

# 卡死或崩溃都自动拉起。配合引擎里的硬超时：
# 库卡在网络等待上时会主动退出（exit 70），由这里把服务重新拉起来。
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE_NAME}

LimitNOFILE=65535
# 内存小的机器上别被 OOM killer 优先干掉
OOMScoreAdjust=-200

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true
systemctl restart "$SERVICE_NAME"

# ------------------------------------------------------------------ 探活
log "等待引擎就绪…"
ready=0
for _ in $(seq 1 60); do
    if curl -sf -m 3 "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 2
done

if [ "$ready" = "1" ]; then
    log "服务已就绪 ✅"
    curl -s -m 8 "http://127.0.0.1:${PORT}/api/health" | head -c 260; echo
    cat <<EOF

常用命令：
  systemctl status  ${SERVICE_NAME}
  systemctl restart ${SERVICE_NAME}
  journalctl -u ${SERVICE_NAME} -f
EOF
else
    warn "服务没起来，看日志：journalctl -u ${SERVICE_NAME} -n 50 --no-pager"
    exit 1
fi
