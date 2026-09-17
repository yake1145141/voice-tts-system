# 在 Colab 里直接试听一条（走完整 TTS + RVC 流程）
import json, urllib.request
from IPython.display import Audio, display

text = "你好，这是运行在 Google Colab 上的语音合成测试。"
req = urllib.request.Request(
    f"http://127.0.0.1:{PORT}/api/tts/file",
    data=json.dumps({"text": text}).encode(),
    headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"})
with urllib.request.urlopen(req, timeout=300) as resp:
    audio = resp.read()
with open("/content/test.wav", "wb") as fh:
    fh.write(audio)
print(f"已生成 /content/test.wav（{len(audio) / 1024:.1f} KB）")
display(Audio("/content/test.wav"))
