# 在 chroot 内执行：为 Python 3.12 补一个 numpy.distutils.cpuinfo 最小实现
#
# 背景：faiss 1.10 的 loader 会用 numpy.distutils.cpuinfo 检测 ARM SVE 指令集，
# 而 numpy 1.26 在 Python 3.12 上不再提供 numpy.distutils（3.12 移除了 distutils），
# 导致 `import faiss` 直接失败。上游 faiss 1.15 已用 try/except 兜住。
# 这里补一个"报告无 SVE"的最小实现，faiss 即可正常导入（手机上 SVE 不是瓶颈）。
set -Eeuo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export TMPDIR=/tmp

SP=/opt/tts-server/python/lib/python3.12/site-packages
mkdir -p "$SP/numpy/distutils"

cat > "$SP/numpy/distutils/__init__.py" <<'PY'
"""numpy.distutils 的最小替身（Python 3.12 上 numpy 官方不再提供）。

仅用于满足 faiss 等库对 cpuinfo 的探测需求。真正的构建工具链不需要它。
"""

__all__ = ["cpuinfo"]
PY

cat > "$SP/numpy/distutils/cpuinfo.py" <<'PY'
"""最小 cpuinfo 实现：只提供 faiss 需要的 cpu.info[0]['Features']。"""


class _Cpu:
    def __init__(self) -> None:
        # 声明"没有 SVE"：手机上 faiss 走通用 NEON/ASIMD 路径，性能足够
        self.info = [{"Features": ""}]


cpu = _Cpu()


def get_cpuinfo() -> list:
    return cpu.info
PY

echo "已写入 numpy.distutils 替身"
/opt/tts-server/python/bin/python3 -c "
import numpy.distutils.cpuinfo as c
print('cpuinfo.cpu.info =', c.cpu.info)
import faiss
print('faiss 导入成功，版本信息:', getattr(faiss, '__version__', 'n/a'))
"
