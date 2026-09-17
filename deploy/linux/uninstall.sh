#!/usr/bin/env bash
# =============================================================================
#  卸载服务端（默认保留安装目录，加 --purge 才连目录一起删）
#
#    sudo bash deploy/linux/uninstall.sh            # 只停服务 + 删 systemd 单元
#    sudo bash deploy/linux/uninstall.sh --purge    # 连 /opt/tts-server 一起删
# =============================================================================
set -Eeuo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/tts-server}"
SERVICE_NAME="tts-server"
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

[ "$(id -u)" = "0" ] || { echo "请用 root 运行" >&2; exit 1; }

echo "[1/3] 停止并禁用服务"
systemctl disable --now "$SERVICE_NAME" >/dev/null 2>&1 || true
rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
systemctl daemon-reload

if [ "$PURGE" = "1" ]; then
    if [ -d "$INSTALL_DIR" ]; then
        echo "[2/3] 备份模型目录到 ${INSTALL_DIR}.models.bak"
        mkdir -p "${INSTALL_DIR}.models.bak"
        cp -r "$INSTALL_DIR/models/." "${INSTALL_DIR}.models.bak/" 2>/dev/null || true
        echo "[3/3] 删除 $INSTALL_DIR"
        rm -rf "$INSTALL_DIR"
        echo "模型已保留在 ${INSTALL_DIR}.models.bak"
    fi
else
    echo "[2/3] 保留安装目录 $INSTALL_DIR（加 --purge 可一并删除）"
    echo "[3/3] 完成"
fi
