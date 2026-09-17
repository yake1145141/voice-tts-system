import os, subprocess, sys, time


def run(cmd, check=True, quiet=False):
    """执行命令并回显输出（quiet=True 时只在失败时打印）。"""
    p = subprocess.Popen(cmd, shell=isinstance(cmd, str),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines = []
    for line in p.stdout:
        lines.append(line)
        if not quiet:
            print(line, end="")
    rc = p.wait()
    if check and rc != 0:
        print("".join(lines[-40:]))
        raise RuntimeError(f"命令失败({rc}): {cmd}")
    return rc


print("=== 1/3 检查 GPU ===")
run([sys.executable, "-c",
     "import torch;print('torch', torch.__version__, '| CUDA:', torch.cuda.is_available(),"
     "'|', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU')"])

print("\n=== 2/3 检查 ffmpeg（Colab 通常已自带）===")
if os.system("ffmpeg -version >/dev/null 2>&1") != 0:
    run("apt-get -qq update && apt-get -qq install -y ffmpeg", quiet=True)
print(subprocess.run("ffmpeg -version | head -1", shell=True,
                     capture_output=True, text=True).stdout.strip())

print("\n=== 3/3 安装服务端依赖（约 2~4 分钟）===")
run([sys.executable, "-m", "pip", "install", "-q",
     "fastapi", "uvicorn[standard]", "pydantic", "PyYAML", "httpx"])
rc = run([sys.executable, "-m", "pip", "install", "-q", "tts-with-rvc>=0.1.9"], check=False)
if rc != 0:
    # 少数环境里 praat-parselmouth 需要现场编译（本项目默认用 rmvpe，用不到它）
    print("\n[!] tts-with-rvc 安装失败，改为跳过 praat-parselmouth 再装一次…")
    run([sys.executable, "-m", "pip", "install", "-q", "--no-deps", "tts-with-rvc>=0.1.9"])
    run([sys.executable, "-m", "pip", "install", "-q",
         "huggingface_hub", "av", "nest_asyncio", "edge-tts", "numpy==1.26.0",
         "librosa==0.9.1", "faiss-cpu==1.10.0", "soundfile>=0.12.1", "ffmpeg-python>=0.2.0",
         "resampy>=0.4.2", "scikit-learn", "tqdm>=4.63.1", "audioread", "pyworld==0.3.5",
         "torchcrepe==0.0.20", "numba==0.60.0", "cffi<2.0.0", "einops", "local_attention",
         "fairseq-fixed"])

print("\n=== 自检 ===")
run([sys.executable, "-c",
     "import torch, fastapi, tts_with_rvc;"
     "print('torch', torch.__version__);print('CUDA 可用:', torch.cuda.is_available())"])
