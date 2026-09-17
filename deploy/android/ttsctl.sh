#!/system/bin/sh
# =============================================================================
#  安卓语音服务控制脚本（设备端，需要 root）
#
#  用法（电脑端）：
#     adb push deploy/android/ttsctl.sh /data/local/tmp/
#     adb shell su -c "sh /data/local/tmp/ttsctl.sh start|stop|restart|status|health|test|logs|config"
#
#  说明：
#    * 服务实际运行在 Ubuntu chroot（/data/tts-rootfs）内的 /opt/tts-server
#    * 配置改 /data/tts-rootfs/opt/tts-server/config.yaml 后重启生效
# =============================================================================

ROOT="${TTS_ROOTFS:-/data/tts-rootfs}"
TTS="$ROOT/opt/tts-server"
TTS_IN="/opt/tts-server"          # chroot 内的同一目录
BB=/data/adb/ksu/bin/busybox
[ -x "$BB" ] || BB=busybox
PIDFILE="$TTS/logs/server.pid"
LOG="$TTS/logs/server.log"
PORT_DEFAULT=8080

ensure_mounts() {
    for m in proc sys dev dev/pts; do mkdir -p "$ROOT/$m"; done
    mkdir -p "$ROOT/tmp"
    mountpoint -q "$ROOT/proc"    || $BB mount -t proc proc "$ROOT/proc"
    mountpoint -q "$ROOT/sys"     || $BB mount -o bind /sys "$ROOT/sys"
    mountpoint -q "$ROOT/dev"     || $BB mount -o bind /dev "$ROOT/dev"
    mountpoint -q "$ROOT/dev/pts" || $BB mount -o bind /dev/pts "$ROOT/dev/pts"
    # /dev/shm：torch / joblib / python multiprocessing 需要共享内存
    mkdir -p "$ROOT/dev/shm"
    mountpoint -q "$ROOT/dev/shm" || \
        $BB mount -t tmpfs -o mode=1777,size=512m tmpfs "$ROOT/dev/shm" 2>/dev/null || true
}

in_chroot() {
    TMPDIR=/tmp HOME=/root LANG=C.UTF-8 chroot "$ROOT" /bin/bash -c \
        "export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin TMPDIR=/tmp HOME=/root; cd /; $1"
}

port() {
    sed -n 's/^ *port: *\([0-9]*\).*/\1/p' "$TTS/config.yaml" 2>/dev/null | head -1
}

api_key() {
    sed -n 's/^ *api_key: *"\{0,1\}\([^"]*\)"\{0,1\}.*/\1/p' "$TTS/config.yaml" 2>/dev/null | head -1
}

is_running() {
    if [ -f "$PIDFILE" ]; then
        PID=$(cat "$PIDFILE" 2>/dev/null)
        [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null && return 0
    fi
    # 兜底：pidfile 丢失时按进程名查找（并回写 pidfile）
    PID=$($BB pgrep -f "app/main.py" 2>/dev/null | head -1)
    if [ -n "$PID" ]; then
        echo "$PID" > "$PIDFILE" 2>/dev/null || true
        return 0
    fi
    return 1
}

wait_health() {
    P=$(port); [ -n "$P" ] || P=$PORT_DEFAULT
    i=0
    while [ $i -lt 90 ]; do
        if $BB wget -q -O - "http://127.0.0.1:$P/api/health" 2>/dev/null | grep -q '"success": *true'; then
            echo "服务就绪（http://127.0.0.1:$P）"
            return 0
        fi
        sleep 2
        i=$((i+1))
    done
    echo "等待服务就绪超时，日志尾部："
    tail -15 "$LOG" 2>/dev/null
    return 1
}

cmd_start() {
    ensure_mounts
    if is_running; then echo "服务已在运行（pid $(cat $PIDFILE)）"; return 0; fi
    [ -f "$TTS/config.yaml" ] || { echo "缺少 $TTS/config.yaml"; return 1; }
    THREADS="${OMP_NUM_THREADS:-8}"
    in_chroot "mkdir -p $TTS_IN/logs $TTS_IN/output && cd $TTS_IN && \
        OMP_NUM_THREADS=$THREADS nohup ./python/bin/python3 -s app/main.py --config config.yaml \
        > $TTS_IN/logs/server.log 2>&1 & echo \$! > $TTS_IN/logs/server.pid"
    sleep 2
    if is_running; then
        echo "已启动（pid $(cat $PIDFILE)，OMP_NUM_THREADS=$THREADS），等待就绪…"
        wait_health
    else
        echo "启动失败，日志："; tail -20 "$LOG" 2>/dev/null
        return 1
    fi
}

cmd_stop() {
    if is_running; then
        PID=$(cat "$PIDFILE")
        kill "$PID" 2>/dev/null
        sleep 2
        kill -9 "$PID" 2>/dev/null
        echo "已停止（pid $PID）"
    else
        echo "服务未在运行"
    fi
    rm -f "$PIDFILE"
    # 兜底：杀掉遗留进程
    $BB pkill -f "app/main.py" 2>/dev/null
}

cmd_health() {
    P=$(port); [ -n "$P" ] || P=$PORT_DEFAULT
    K=$(api_key)
    if [ -n "$K" ]; then
        $BB wget -q -O - --header="Authorization: Bearer $K" "http://127.0.0.1:$P/api/health" 2>/dev/null || \
        $BB wget -q -O - "http://127.0.0.1:$P/api/health"
    else
        $BB wget -q -O - "http://127.0.0.1:$P/api/health"
    fi
}

cmd_test() {
    TEXT="${2:-你好，这是安卓手机本地合成的语音测试。}"
    P=$(port); [ -n "$P" ] || P=$PORT_DEFAULT
    K=$(api_key)
    in_chroot "cd $TTS_IN && ./python/bin/python3 -s app/tts_client.py --url http://127.0.0.1:$P --api-key '$K' say '$TEXT'"
}

case "${1:-}" in
    start)   cmd_start ;;
    stop)    cmd_stop ;;
    restart) cmd_stop; cmd_start ;;
    status)
        if is_running; then echo "运行中：pid $(cat $PIDFILE)"; else echo "未运行"; fi
        echo "--- 配置 ---"
        grep -E "^  (host|port|api_key|model|f0_method|expire_minutes|max_concurrent)" "$TTS/config.yaml" 2>/dev/null
        echo "--- 健康检查 ---"
        cmd_health; echo
        ;;
    health)  cmd_health; echo ;;
    test)    cmd_test "$@" ;;
    logs)    tail -n "${2:-40}" "$LOG" 2>/dev/null ;;
    config)  cat "$TTS/config.yaml" 2>/dev/null ;;
    *)
        echo "用法: ttsctl.sh {start|stop|restart|status|health|test [文本]|logs [行数]|config}"
        exit 1
        ;;
esac
