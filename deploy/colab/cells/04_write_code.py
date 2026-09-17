import base64, gzip, io, os, tarfile

# 服务端代码已内嵌在本单元格里（main.py / config.py / engine.py / storage.py）
PAYLOAD_B64 = "__PAYLOAD__"

os.makedirs(f"{WORKDIR}/app", exist_ok=True)
raw = gzip.decompress(base64.b64decode(PAYLOAD_B64))
with tarfile.open(fileobj=io.BytesIO(raw), mode="r") as tar:
    tar.extractall(f"{WORKDIR}/app")

print("已写入:", ", ".join(sorted(os.listdir(f"{WORKDIR}/app"))))
