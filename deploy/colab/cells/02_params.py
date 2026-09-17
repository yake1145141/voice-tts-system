# ============================ 参数设置（改这里）============================
API_KEY = "colab-tts-key"     # AstrBot 插件里要填同一个值（建议改成随机字符串）
PORT = 8080

# RVC 模型来源，四选一：
#   "drive"  → 已把 .pth（和 .index）放到 Google Drive 的 ColabVoice 目录（推荐）
#   "upload" → 运行时手动上传（55MB 左右，稍慢）
#   "url"    → 从直链下载（填 MODEL_URL / INDEX_URL）
#   "none"   → 先不用 RVC，只跑 Edge TTS（验证链路用）
MODEL_SOURCE = "drive"

DRIVE_DIR = "/content/drive/MyDrive/ColabVoice"
MODEL_FILE = "Shaonian.pth"
INDEX_FILE = "Shaonian.index"      # 没有索引文件就留空字符串 ""
MODEL_URL = ""                     # MODEL_SOURCE="url" 时填 .pth 直链
INDEX_URL = ""                     # 可选

F0_METHOD = "rmvpe"                # rmvpe 质量最好；想更快可改 pm / dio
PITCH = 5                          # RVC 变调半音数
MAX_TEXT_LENGTH = 300              # 与插件侧 voice.max_text_length 保持一致
EXPIRE_MINUTES = 10                # 服务端音频保留时间（分钟）

WORKDIR = "/content/tts-server"    # 服务端运行目录（每次会话重新生成）
print(f"参数就绪：端口={PORT} 模型来源={MODEL_SOURCE} f0={F0_METHOD} pitch={PITCH}")
