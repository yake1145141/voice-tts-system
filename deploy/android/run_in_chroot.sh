#!/system/bin/sh
# 在 chroot 内执行一个脚本（避免命令行嵌套引号问题）
#
# 用法：
#   adb push <你的脚本>.sh /data/local/tmp/cmd.sh
#   adb push deploy/android/run_in_chroot.sh /data/local/tmp/
#   adb shell su -c "sh /data/local/tmp/run_in_chroot.sh /data/local/tmp/cmd.sh"
#
# 不传参数时直接进入交互式 shell。
set -eu

ROOT="${TTS_ROOTFS:-/data/tts-rootfs}"
BB=/data/adb/ksu/bin/busybox
[ -x "$BB" ] || BB=busybox

for m in proc sys dev dev/pts; do
    mkdir -p "$ROOT/$m"
done
mountpoint -q "$ROOT/proc"    || $BB mount -t proc proc "$ROOT/proc"
mountpoint -q "$ROOT/sys"     || $BB mount -o bind /sys "$ROOT/sys"
mountpoint -q "$ROOT/dev"     || $BB mount -o bind /dev "$ROOT/dev"
mountpoint -q "$ROOT/dev/pts" || $BB mount -o bind /dev/pts "$ROOT/dev/pts"
# torch/joblib 需要 /dev/shm
mkdir -p "$ROOT/dev/shm"
mountpoint -q "$ROOT/dev/shm" || \
    $BB mount -t tmpfs -o mode=1777,size=512m tmpfs "$ROOT/dev/shm" 2>/dev/null || true

export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
# 关键：安卓环境里 TMPDIR 指向 /data/local/tmp，chroot 内不存在该目录，
# 会导致 mktemp/dpkg/pip 等随机报错，这里统一改到 chroot 内的 /tmp
export TMPDIR=/tmp HOME=/root LANG=C.UTF-8 LC_ALL=C.UTF-8
mkdir -p "$ROOT/tmp"

if [ $# -ge 1 ]; then
    cp -f "$1" "$ROOT/root/exec.sh"
    chroot "$ROOT" /bin/bash -c 'cd /root && bash /root/exec.sh'
else
    exec chroot "$ROOT" /bin/bash -l
fi
