import os, shutil, subprocess, sys, urllib.request

os.makedirs(f"{WORKDIR}/models", exist_ok=True)
model_path = ""
index_path = ""

if MODEL_SOURCE == "drive":
    from google.colab import drive

    drive.mount("/content/drive")
    src_model = os.path.join(DRIVE_DIR, MODEL_FILE)
    if os.path.exists(src_model):
        shutil.copy(src_model, f"{WORKDIR}/models/{MODEL_FILE}")
        model_path = MODEL_FILE
        print("已从 Drive 复制模型:", MODEL_FILE)
        if INDEX_FILE and os.path.exists(os.path.join(DRIVE_DIR, INDEX_FILE)):
            shutil.copy(os.path.join(DRIVE_DIR, INDEX_FILE), f"{WORKDIR}/models/{INDEX_FILE}")
            index_path = INDEX_FILE
            print("已从 Drive 复制索引:", INDEX_FILE)
    else:
        print(f"[!] Drive 里没找到 {src_model}，本次只用 Edge TTS（不跑 RVC）")
elif MODEL_SOURCE == "upload":
    from google.colab import files

    print("请选择 .pth（可以同时选中 .index）：")
    uploaded = files.upload()
    for name, data in uploaded.items():
        with open(f"{WORKDIR}/models/{name}", "wb") as fh:
            fh.write(data)
        if name.lower().endswith(".pth"):
            model_path = name
        elif name.lower().endswith(".index"):
            index_path = name
    print("上传完成:", model_path, index_path)
elif MODEL_SOURCE == "url":
    if not MODEL_URL:
        raise ValueError('MODEL_SOURCE="url" 时必须填 MODEL_URL')
    urllib.request.urlretrieve(MODEL_URL, f"{WORKDIR}/models/{MODEL_FILE}")
    model_path = MODEL_FILE
    if INDEX_URL:
        urllib.request.urlretrieve(INDEX_URL, f"{WORKDIR}/models/{INDEX_FILE}")
        index_path = INDEX_FILE
    print("已从直链下载模型")
else:
    print("MODEL_SOURCE=none：只跑 Edge TTS（不加载 RVC 模型）")

# hubert / rmvpe 权重：tts-with-rvc 会在"当前工作目录"里找这两个文件
for asset in ("hubert_base.pt", "rmvpe.pt"):
    dst = os.path.join(WORKDIR, asset)
    if not os.path.exists(dst):
        print(f"下载 {asset} …")
        url = f"https://huggingface.co/lj1995/VoiceConversionWebUI/resolve/main/{asset}"
        subprocess.run(["wget", "-q", "-O", dst, url], check=True)
    print(f"  {asset}: {os.path.getsize(dst) / 1024 / 1024:.1f} MB")

os.chdir(WORKDIR)
print("\n模型:", model_path or "(未配置)", "| 索引:", index_path or "(无)")
