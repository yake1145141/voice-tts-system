"""把整合包目录打成 zip（没有 7-Zip 时使用；用多线程 + 低压缩级别以保证速度）。"""

from __future__ import annotations

import os
import sys
import zipfile
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 3:
        print("用法: zip_bundle.py <源目录> <输出.zip> [压缩级别0-9]", file=sys.stderr)
        return 2
    src = Path(sys.argv[1]).resolve()
    dst = Path(sys.argv[2]).resolve()
    level = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    if not src.is_dir():
        print(f"目录不存在: {src}", file=sys.stderr)
        return 1

    files = [p for p in src.rglob("*") if p.is_file()]
    total = sum(p.stat().st_size for p in files)
    print(f"  共 {len(files)} 个文件，{total / 1024 ** 3:.2f} GB，压缩级别 {level}")

    os.chdir(src.parent)
    base = src.name
    done = 0
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED, compresslevel=level) as archive:
        for path in files:
            archive.write(path, f"{base}/{path.relative_to(src).as_posix()}")
            done += 1
            if done % 2000 == 0:
                print(f"  已打包 {done}/{len(files)} …", flush=True)
    print(f"  完成: {dst}（{dst.stat().st_size / 1024 ** 3:.2f} GB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
