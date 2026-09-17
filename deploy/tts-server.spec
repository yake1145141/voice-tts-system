# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：把语音处理端打成 Linux amd64「单目录可执行文件」。

用法（在 Linux x86_64 + Python 3.12 下执行，推荐直接用 Docker）：
    pyinstaller deploy/tts-server.spec --noconfirm

产物：dist/tts-server/tts-server  （可执行文件）
      dist/tts-server/ 目录里还需要有 config.yaml、models/、bin/ffmpeg
      （由 deploy/build_linux_executable.sh 自动补齐）

说明：
* 这套依赖树（torch + fairseq + numba + librosa…）大量使用动态导入和运行时数据文件，
  所以这里用 collect_all 把它们整体收集，代价是产物体积大（2~4GB）。
* 不使用 --onefile：单文件模式每次启动都要把几 GB 解压到 /tmp，启动慢且占双份磁盘。
* config.yaml 在冻结环境下的查找逻辑见 tts-server/config.py 的 default_config_path()。
"""

from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_all,
    collect_data_files,
    collect_dynamic_libs,
    copy_metadata,
)

PROJECT_ROOT = Path(SPECPATH).parent          # deploy/ 的上级目录
SERVER_DIR = PROJECT_ROOT / "tts-server"

datas: list = []
binaries: list = []
hiddenimports: list = []
metadata_packages = [
    "torch",
    "torchaudio",
    "tts-with-rvc",
    "tts_with_rvc",
    "numpy",
    "scipy",
    "fairseq",
    "fairseq-fixed",
    "fairseq-built",
    "librosa",
    "numba",
    "fastapi",
    "uvicorn",
    "pydantic",
    "huggingface-hub",
]


def safe_collect_all(package: str) -> None:
    """collect_all 个别包不存在时不应中断打包。"""
    try:
        package_datas, package_binaries, package_hidden = collect_all(package)
    except Exception as exc:  # noqa: BLE001
        print(f"[spec] collect_all('{package}') 跳过: {exc}")
        return
    datas.extend(package_datas)
    binaries.extend(package_binaries)
    hiddenimports.extend(package_hidden)
    print(f"[spec] collect_all('{package}'): {len(package_datas)} 数据 / "
          f"{len(package_binaries)} 二进制 / {len(package_hidden)} 隐藏导入")


# 动态导入多、带数据文件的包：整体收集
for name in (
    "tts_with_rvc",
    "fairseq",
    "hydra",
    "omegaconf",
    "antlr4",
    "numba",
    "llvmlite",
    "librosa",
    "soundfile",
    "resampy",
    "pyworld",
    "praat_parselmouth",
    "faiss",
    "torchcrepe",
    "local_attention",
    "einops",
    "av",
    "audioread",
    "sklearn",
    "scipy",
    "numpy",
    "uvicorn",
    "fastapi",
    "starlette",
    "pydantic",
    "yaml",
    "huggingface_hub",
    "nest_asyncio",
    "tqdm",
):
    safe_collect_all(name)

# torch 体积巨大：数据文件 + 动态库单独收集，其余交给 pyinstaller-hooks-contrib
try:
    datas += collect_data_files("torch")
    binaries += collect_dynamic_libs("torch")
except Exception as exc:  # noqa: BLE001
    print(f"[spec] 收集 torch 数据失败: {exc}")

for package in metadata_packages:
    try:
        datas += copy_metadata(package)
    except Exception:  # noqa: BLE001
        pass

hiddenimports += [
    "torch",
    "torch.nn",
    "torch.nn.functional",
    "torch.utils.data",
    "torchaudio",
    "fairseq.checkpoint_utils",
    "fairseq.data.dictionary",
    "fairseq.tasks.text_to_speech",
    "fairseq.utils",
    "hydra",
    "hydra.core.config_store",
    "omegaconf",
    "omegaconf.dictconfig",
    "omegaconf.listconfig",
    "tts_with_rvc",
    "tts_with_rvc.inference",
    "tts_with_rvc.vc_infer",
    "tts_with_rvc.infer.vc.modules",
    "tts_with_rvc.infer.vc.pipeline",
    "engine",
    "config",
    "storage",
    "uvicorn.lifespan.on",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.websockets_impl",
]

analysis = Analysis(
    [str(SERVER_DIR / "main.py")],
    pathex=[str(SERVER_DIR)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # 明显用不到的大件，能省一点体积
        "tkinter",
        "matplotlib",
        "IPython",
        "notebook",
        "pytest",
    ],
    noarchive=False,
)
pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="tts-server",
    debug=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="tts-server",
)
