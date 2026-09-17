#!/system/bin/sh
# =============================================================================
#  在安卓设备上创建 Ubuntu arm64 chroot（需要 root）
#
#  用法（电脑端）：
#     adb push deploy/android/1_setup_rootfs.sh /data/local/tmp/
#     adb shell su -c "sh /data/local/tmp/1_setup_rootfs.sh"
#
#  作用：
#    1. 下载 Ubuntu 24.04 base arm64 rootfs（约 29MB）到 /data/local/tmp
#    2. 解压到 /data/tts-rootfs
#    3. 配好 DNS、挂载 /proc /sys /dev /dev/pts
#    4. 用一个 chroot 命令验证能否正常运行 glibc 程序
# =============================================================================
set -eu

ROOT="${TTS_ROOTFS:-/data/tts-rootfs}"
CACHE=/data/local/tmp/tts-cache
ROOTFS_URL="${ROOTFS_URL:-https://cdimage.ubuntu.com/ubuntu-base/releases/24.04/release/ubuntu-base-24.04.5-base-arm64.tar.gz}"
ROOTFS_FILE="$CACHE/ubuntu-base-arm64.tar.gz"
BB=/data/adb/ksu/bin/busybox
[ -x "$BB" ] || BB=busybox

echo "[1/5] 准备目录"
mkdir -p "$CACHE" "$ROOT"

echo "[2/5] 下载 rootfs（约 29MB）"
if [ ! -s "$ROOTFS_FILE" ]; then
    curl -fL --retry 3 -o "$ROOTFS_FILE" "$ROOTFS_URL" || {
        echo "下载失败，试试镜像："
        echo "  curl -fL -o $ROOTFS_FILE https://mirrors.tuna.tsinghua.edu.cn/ubuntu-cdimage/ubuntu-base/releases/24.04/release/ubuntu-base-24.04.5-base-arm64.tar.gz"
        exit 1
    }
fi
ls -lh "$ROOTFS_FILE"

echo "[3/5] 解压到 $ROOT"
if [ ! -x "$ROOT/bin/bash" ]; then
    $BB tar -xzf "$ROOTFS_FILE" -C "$ROOT"
fi
mkdir -p "$ROOT/proc" "$ROOT/sys" "$ROOT/dev/pts" "$ROOT/tmp" "$ROOT/root" "$ROOT/opt"

echo "[4/5] 配置 DNS 与挂载"
if [ ! -s "$ROOT/etc/resolv.conf" ]; then
    {
        echo "nameserver 223.5.5.5"
        echo "nameserver 119.29.29.29"
    } > "$ROOT/etc/resolv.conf"
fi
grep -q "tts-rootfs" "$ROOT/etc/hosts" 2>/dev/null || echo "127.0.0.1 localhost tts-rootfs" >> "$ROOT/etc/hosts"

mountpoint -q "$ROOT/proc"    || $BB mount -t proc proc "$ROOT/proc"
mountpoint -q "$ROOT/sys"     || $BB mount -o bind /sys "$ROOT/sys"
mountpoint -q "$ROOT/dev"     || $BB mount -o bind /dev "$ROOT/dev"
mountpoint -q "$ROOT/dev/pts" || $BB mount -o bind /dev/pts "$ROOT/dev/pts"

echo "[5/5] chroot 自检"
chroot "$ROOT" /bin/bash -c '
set -e
echo "  内核: $(uname -m)"
. /etc/os-release
echo "  系统: $PRETTY_NAME"
echo "  Python: $(python3 --version 2>&1)"
echo "  glibc: $(ldd --version | head -1)"
echo "  内存: $(free -h | awk "/Mem:/{print \$2\" 总 / \"\$7\" 可用\"}")"
echo "  磁盘: $(df -h / | tail -1 | awk "{print \$2\" 总 / \"\$4\" 可用\"}")"
echo "  DNS: $(getent hosts mirrors.tuna.tsinghua.edu.cn | head -1)"
'

echo
echo "✅ chroot 就绪：$ROOT"
echo "   进入方式：adb shell su -c \"$BB chroot $ROOT /bin/bash\""
