import os

config_text = f"""# Colab 语音服务配置（本文件由 notebook 生成；手改后重启服务生效）
server:
  host: "0.0.0.0"
  port: {PORT}
  log_level: "INFO"

security:
  api_key: "{API_KEY}"

tts:
  source: "edgetts"
  speaker: "zh-CN-YunxiNeural"
  pitch: {PITCH}

rvc:
  enabled: {str(bool(model_path)).lower()}
  model: "{model_path}"
  model_dir: "./models"
  index: "{index_path}"
  f0_method: "{F0_METHOD}"
  device: "auto"
  min_vram_mb: 2500
  allow_cpu_fallback: true
  is_half: true
  preload: true

storage:
  output_dir: "./output"
  expire_minutes: {EXPIRE_MINUTES}
  cleanup_interval_minutes: 2
  cache_by_text: true
  max_text_length: 2000

queue:
  max_concurrent: 1
  max_queue_size: 16
  timeout: 180

audio:
  output_format: "wav"
"""

os.makedirs(WORKDIR, exist_ok=True)
with open(f"{WORKDIR}/config.yaml", "w", encoding="utf-8") as fh:
    fh.write(config_text)
print(config_text)
