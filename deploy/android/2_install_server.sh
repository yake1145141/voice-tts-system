#!/system/bin/sh
# =============================================================================
#  把语音服务安装进安卓上的 Ubuntu chroot（在电脑端执行本文件）
#
#  前置：已跑过 1_setup_rootfs.sh，且已用 adb push 准备好素材：
#     /data/local/tmp/tts-src/app/        服务端 py 代码 + config.yaml
#     /data/local/tmp/tts-src/models/     RVC 模型（.pth/.index）
#     /data/local/tmp/tts-src/assets/     hubert_base.pt / rmvpe.pt
#     /data/local/tmp/tts-src/install_inchroot.sh
#
#  用法：
#     adb push ... /data/local/tmp/tts-src/
#     adb push deploy/android/2_install_server.sh /data/local/tmp/
#     adb shell su -c "sh /data/local/tmp/2_install_server.sh"
# =============================================================================
set -eu

ROOT="${TTS_ROOTFS:-/data/tts-rootfs}"
SRC=/data/local/tmp/tts-src
BB=/data/adb/ksu/bin/busybox
[ -x "$BB" ] || BB=busybox

echo "[1/4] 检查素材"
# install_inchroot.sh 允许放在素材目录或 /data/local/tmp 下
INSTALL_SH="$SRC/install_inchroot.sh"
[ -f "$INSTALL_SH" ] || INSTALL_SH=/data/local/tmp/install_inchroot.sh
for item in "$SRC/app" "$INSTALL_SH"; do
    [ -e "$item" ] || { echo "缺少 $item"; exit 1; }
done
ls -lh "$SRC" "$SRC/app" 2>/dev/null | sed 's/^/  /'

echo "[2/4] 确保 chroot 挂载"
for m in proc sys dev dev/pts; do
    mkdir -p "$ROOT/$m"
done
mountpoint -q "$ROOT/proc"    || $BB mount -t proc proc "$ROOT/proc"
mountpoint -q "$ROOT/sys"     || $BB mount -o bind /sys "$ROOT/sys"
mountpoint -q "$ROOT/dev"     || $BB mount -o bind /dev "$ROOT/dev"
mountpoint -q "$ROOT/dev/pts" || $BB mount -o bind /dev/pts "$ROOT/dev/pts"

echo "[3/4] 拷贝服务端文件到 chroot 内 /opt/tts-server"
DEST="$ROOT/opt/tts-server"
mkdir -p "$DEST/app" "$DEST/models" "$DEST/output" "$DEST/logs" "$DEST/bin" "$DEST/venv"
cp -f "$SRC"/app/* "$DEST/app/"
# 配置文件要放在服务目录根（main.py --config config.yaml）
if [ -f "$SRC/app/config.yaml" ]; then
    cp -f "$SRC/app/config.yaml" "$DEST/config.yaml"
fi
[ -d "$SRC/models" ] && cp -f "$SRC"/models/* "$DEST/models/" 2>/dev/null || true
[ -d "$SRC/assets" ] && cp -f "$SRC"/assets/* "$DEST/" 2>/dev/null || true
cp -f "$INSTALL_SH" "$ROOT/root/install_inchroot.sh"
chmod +x "$ROOT/root/install_inchroot.sh"
# 自带的 arm64 Python 运行时（在电脑端下载后 push 到素材目录）
if [ -f "$SRC/python-arm64.tar.gz" ]; then
    cp -f "$SRC/python-arm64.tar.gz" "$ROOT/root/python-arm64.tar.gz"
fi
ls -lh "$DEST" "$DEST/app" | sed 's/^/  /'

echo "[4/4] 进入 chroot 安装（日志：$ROOT/root/install.log）"
mkdir -p "$ROOT/tmp"
TMPDIR=/tmp HOME=/root LANG=C.UTF-8 chroot "$ROOT" /bin/bash -c \
    'export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin TMPDIR=/tmp HOME=/root; \
     bash /root/install_inchroot.sh > /root/install.log 2>&1; \
     rc=$?; tail -25 /root/install.log; exit $rc'

echo
echo "✅ 安装流程结束，安装日志尾部："
tail -20 "$ROOT/root/install.log" 2>/dev/null | sed 's/^/  /'
