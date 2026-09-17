"""把 d8 生成的 classes.dex 打包进 aapt2 产物（手工构建 APK 用）。"""

from __future__ import annotations

import sys
import zipfile


def main() -> int:
    if len(sys.argv) < 3:
        print("用法: pack_dex.py <base.apk> <classes.dex>", file=sys.stderr)
        return 2
    apk, dex = sys.argv[1], sys.argv[2]
    with zipfile.ZipFile(apk, "a", zipfile.ZIP_DEFLATED) as archive:
        archive.write(dex, "classes.dex")
    print("  已写入 classes.dex")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
