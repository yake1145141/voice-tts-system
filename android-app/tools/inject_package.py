"""给 AndroidManifest.xml 注入 package 属性（aapt2 手工构建需要，Gradle 构建不需要）。"""

from __future__ import annotations

import re
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 4:
        print("用法: inject_package.py <源清单> <输出清单> <包名>", file=sys.stderr)
        return 2
    src, dst, package = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    text = src.read_text(encoding="utf-8")
    if re.search(r"<manifest[^>]*\bpackage=", text):
        out = text
    else:
        out = re.sub(r"<manifest\b", f'<manifest package="{package}"', text, count=1)
    dst.write_text(out, encoding="utf-8")
    print(f"  已生成 {dst.name}（package={package}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
