#!/usr/bin/env python3
"""从本仓库生成「开源发布包」。

    python deploy/make_release.py                 # 生成 release/voice-tts-system-<版本>/
    python deploy/make_release.py --zip           # 顺便打个 zip
    python deploy/make_release.py --version 1.1.0 # 指定版本号

发布包内容 = 仓库源码 + 文档 + 部署脚本，再额外生成一个开箱即用的 client-package/。

这样做的原因：仓库里是开发布局（tests/ 依赖 tts-server/ 这个目录名），
而发布包要给终端用户一个干净的目录。两者用同一个脚本同步，避免手工拷贝出错。

Windows 的构建脚本在发布包里会改名成 build-bundle.ps1（仓库里是
build_windows_bundle.ps1），脚本内部同时认 server/ 和 tts-server/ 两种源码目录。
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 从仓库根目录原样拷过去的文件 / 目录
ROOT_FILES = ["README.md", "DEPLOY.md", "CHANGELOG.md", "LICENSE", "PUBLISH-CHECKLIST.md"]
ROOT_DIRS = ["docs", "tts-server", "astrbot_plugin_voice_reply", "tools", "android-app", "deploy"]

# 仓库路径 -> 发布包路径（改名的文件）
RENAMES = {
    "deploy/windows/build_windows_bundle.ps1": "deploy/windows/build-bundle.ps1",
}

# 这些文件/目录不进发布包
EXCLUDE_DIRS = {"__pycache__", "build", "dist", ".gradle", ".venv", "venv", "output", "logs"}
EXCLUDE_FILES = {"debug.keystore", ".gitignore", ".dockerignore"}

SKIP_DEPLOY = {"make_release.py", "__pycache__"}


def copy_tree(src: Path, dst: Path) -> int:
    """递归拷贝，跳过构建产物与本地临时文件。返回拷贝的文件数。"""
    count = 0
    for item in sorted(src.rglob("*")):
        rel = item.relative_to(src)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        if item.name in EXCLUDE_FILES:
            continue
        if item.suffix in {".pyc", ".log"}:
            continue
        target = dst / rel
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
            count += 1
    return count


def build_client_package(root: Path, out: Path) -> int:
    """生成开箱即用的客户端部署包：插件 zip + 命令行客户端 + 单文件库 + APK。"""
    pkg = out / "client-package"
    pkg.mkdir(parents=True, exist_ok=True)
    count = 0

    # 1) 命令行客户端与单文件库
    for name in ("tts_client.py", "voice_tts.py", "sample_texts.txt"):
        src = root / "tools" / name
        if src.exists():
            shutil.copy2(src, pkg / name)
            count += 1

    # 2) AstrBot 插件打成 zip（解压出来就是 astrbot_plugin_voice_reply/）
    plugin_src = root / "astrbot_plugin_voice_reply"
    if plugin_src.is_dir():
        version = (out.name.rsplit("-v", 1) + ["0"])[1]
        zip_path = pkg / f"astrbot-plugin-voice-reply-v{version}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for item in sorted(plugin_src.rglob("*")):
                if item.is_file() and "__pycache__" not in item.parts:
                    z.write(item, f"astrbot_plugin_voice_reply/{item.relative_to(plugin_src).as_posix()}")
        count += 1

    # 3) 安卓配置 App（已构建好的 APK；源码在 android-app/）
    apk = root / "android-app" / "dist" / "tts-server-config.apk"
    if apk.exists():
        (pkg / "android").mkdir(parents=True, exist_ok=True)
        shutil.copy2(apk, pkg / "android" / apk.name)
        count += 1
    else:
        print("  [提示] 没找到 android-app/dist/tts-server-config.apk，跳过安卓 App")

    return count


def main() -> int:
    parser = argparse.ArgumentParser(description="生成开源发布包")
    parser.add_argument("--version", default="1.0.1", help="版本号，默认 1.0.1")
    parser.add_argument("--out-dir", default=str(ROOT / "release"), help="输出目录")
    parser.add_argument("--zip", action="store_true", help="顺便打成 zip")
    args = parser.parse_args()

    name = f"voice-tts-system-v{args.version}"
    out_root = Path(args.out_dir)
    out = out_root / name

    if not (ROOT / "tts-server").is_dir():
        print(f"错误：找不到服务端源码 {ROOT / 'tts-server'}", file=sys.stderr)
        return 1

    # 干净重建（只删自己生成的那一层，避免误伤）
    if out.exists():
        print(f"清理旧目录 {out}")
        shutil.rmtree(out)
    out.mkdir(parents=True)

    print(f"生成发布包 {name}")

    total = 0
    for name_ in ROOT_FILES:
        src = ROOT / name_
        if src.exists():
            shutil.copy2(src, out / name_)
            total += 1
            print(f"  + {name_}")

    for name_ in ROOT_DIRS:
        src = ROOT / name_
        if not src.is_dir():
            print(f"  [跳过] 缺少目录 {name_}")
            continue
        if name_ == "deploy":
            # deploy 目录里要排除本脚本自身
            dst = out / "deploy"
            dst.mkdir(parents=True, exist_ok=True)
            n = 0
            for item in sorted(src.rglob("*")):
                rel = item.relative_to(src)
                if any(part in EXCLUDE_DIRS for part in rel.parts):
                    continue
                if item.name in SKIP_DEPLOY or item.suffix == ".pyc":
                    continue
                target = dst / rel
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, target)
                    n += 1
            # 应用改名规则
            for old, new in RENAMES.items():
                src_file = out / old
                if src_file.exists():
                    dst_file = out / new
                    dst_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(src_file), str(dst_file))
                    print(f"  ~ {old} → {new}")
            total += n
            print(f"  + deploy/ ({n} 个文件)")
        else:
            n = copy_tree(src, out / name_)
            total += n
            print(f"  + {name_}/ ({n} 个文件)")

    n = build_client_package(ROOT, out)
    total += n
    print(f"  + client-package/ ({n} 个文件)")

    print(f"\n完成：{out}   共 {total} 个文件")

    if args.zip:
        zip_path = out_root / f"{name}.zip"
        if zip_path.exists():
            zip_path.unlink()
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            for item in sorted(out.rglob("*")):
                if item.is_file():
                    z.write(item, f"{name}/{item.relative_to(out).as_posix()}")
        digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
        print(f"zip ：{zip_path}  {zip_path.stat().st_size / 1024:.1f} KB")
        print(f"SHA256：{digest}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
