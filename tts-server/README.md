# tts-server — 语音处理端（Edge TTS + RVC）

基于 [tts-with-rvc](https://github.com/Atm4x/tts-with-rvc) 的 HTTP 语音合成服务：
固定使用 **Edge TTS**（`zh-CN-YunxiNeural`，音调 `5`）生成基础语音，再用**指定的 RVC 模型**
做音色转换，最后把音频文件通过 HTTP 提供给 AstrBot 插件。

```
文本 ──► Edge TTS (zh-CN-YunxiNeural) ──► RVC (固定模型) ──► output/xxxx.wav ──► AstrBot 语音消息
```

---

## 目录结构

```text
tts-server/
├── main.py          # FastAPI 应用与启动入口
├── config.py        # 配置加载与校验
├── engine.py        # tts-with-rvc 调用封装（线程池 / 并发 / 超时 / 错误码）
├── storage.py       # 音频落盘、查询与过期清理
├── config.yaml      # 配置文件（所有可变参数都在这里）
├── requirements.txt
└── output/          # 生成的音频（自动清理）
```

---

## 一、安装

### 0. 环境要求

- Python **3.10 ~ 3.12**
- **ffmpeg**（tts-with-rvc 与音频格式转换都需要，务必加入 `PATH`）
- NVIDIA GPU + CUDA（推荐；CPU 也能跑，但很慢）

```bash
# Ubuntu / Debian
sudo apt update && sudo apt install -y ffmpeg python3-venv

# 先装好对应 CUDA 版本的 PyTorch：
pip3 install torch torchaudio --index-url https://download.pytorch.org/whl/cu128
```

### 1. 安装 tts-with-rvc 与服务依赖

```bash
cd tts-server
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -U pip
pip install -r requirements.txt
```

`requirements.txt` 会安装 `tts-with-rvc`、`fastapi`、`uvicorn`、`PyYAML` 等。

### 2. 准备 RVC 模型

1. 把你的模型文件放到 `tts-server/models/` 目录，例如 `models/MyVoice.pth`；
2. 如果有索引文件（`.index`），一起放进去，例如 `models/MyVoice.index`；
3. 在 `config.yaml` 里填写模型名（**模型名集中配置，代码中没有任何硬编码**）：

   ```yaml
   rvc:
     model: "MyVoice.pth"      # 也可以写绝对路径 /home/user/models/MyVoice.pth
     model_dir: "./models"
     index: "MyVoice.index"    # 没有索引文件就留空
   ```

---

## 二、配置说明（config.yaml）

```yaml
server:
  host: "0.0.0.0"      # 监听地址
  port: 8080           # 监听端口
  public_base_url: ""  # 生成 audio_url 的对外地址，留空则按请求 Host 推断
  log_level: "INFO"

security:
  api_key: ""          # 非空时，调用方必须带 Authorization / X-API-Key
  protect_audio: false # 是否给音频下载接口也加鉴权（url 交付模式必须为 false）

tts:
  source: "edgetts"            # 固定 edgetts
  speaker: "zh-CN-YunxiNeural" # 固定发音人
  pitch: 5                     # 音调：RVC 变调半音数（传给 tts-with-rvc 的 pitch 参数）
  edge_pitch_hz: 0             # Edge TTS 原生音高（Hz），非必要不要改
  rate: 0                      # 语速百分比（可负）
  volume: 0                    # 音量百分比（可负）

rvc:
  enabled: true                # 调试时可设为 false（跳过 RVC，只用 Edge TTS）
  model: "MyVoice.pth"         # 指定模型（唯一）
  model_dir: "./models"
  index: ""
  index_rate: 0.75
  f0_method: "rmvpe"
  device: "auto"               # auto / cuda:0 / cpu
  min_vram_mb: 2500            # auto 模式下显存低于该值就自动用 CPU（1~2GB 显存必备）
  allow_cpu_fallback: true     # 运行中显存不足时自动降级到 CPU 并重试
  is_half: true
  preload: true                # 启动时预热，提前加载模型

storage:
  output_dir: "./output"
  expire_minutes: 10           # 音频只保留 10 分钟，到期自动删除
  cleanup_interval_minutes: 2  # 后台清理间隔（应明显小于保留时长）
  cache_by_text: true          # 10 分钟内的相同文本复用已生成音频
  max_text_length: 2000

queue:
  max_concurrent: 2            # 并发任务数（RVC 推理由底层库串行化）
  max_queue_size: 32           # 排队上限，超过返回 503
  timeout: 120                 # 单任务超时（秒）

audio:
  output_format: "wav"         # wav（AstrBot 推荐）/ mp3
  mp3_bitrate: "64k"
```

所有配置都可以用**环境变量**覆盖，便于 systemd / Docker 部署：

```bash
TTS_SERVER_CONFIG=/etc/tts-server/config.yaml
TTS_SERVER_HOST=0.0.0.0 TTS_SERVER_PORT=8080
TTS_SERVER_API_KEY=change-me
RVC_MODEL=MyVoice.pth RVC_MODEL_DIR=/data/models RVC_DEVICE=cuda:0
TTS_SERVER_OUTPUT_DIR=/var/lib/tts-server/output
TTS_SERVER_EXPIRE_MINUTES=10
```

---

## 三、启动与校验

```bash
# 只校验配置和模型路径（不启动服务）
python main.py --check-config

# 启动服务
python main.py
python main.py --config /path/to/config.yaml --port 8080

# 也可以直接用 uvicorn（注意必须使用 asyncio 事件循环）
uvicorn main:create_app --factory --loop asyncio --host 0.0.0.0 --port 8080
```

启动日志示例：

```text
2026-01-01 12:00:00 [INFO] tts_server: 语音处理端启动中… 配置: {...}
2026-01-01 12:00:03 [INFO] tts_server: 已加载 tts-with-rvc: model=.../MyVoice.pth index=(未配置) f0=rmvpe device=cuda:0
2026-01-01 12:00:05 [INFO] tts_server: TTS 引擎就绪: device=cuda:0 is_half=True speaker=zh-CN-YunxiNeural rvc=True
2026-01-01 12:00:05 [INFO] tts_server: 监听 http://0.0.0.0:8080
```

---

## 四、HTTP API

### 鉴权

当 `security.api_key` 非空时，以下接口需要携带：

```http
Authorization: Bearer <api_key>
```

或

```http
X-API-Key: <api_key>
```

### 1. 健康检查

```http
GET /api/health
```

```json
{
  "success": true,
  "status": "ok",
  "engine": {
    "ready": true,
    "device": "cuda:0",
    "model": "/opt/tts-server/models/MyVoice.pth",
    "speaker": "zh-CN-YunxiNeural",
    "pitch": 5,
    "f0_method": "rmvpe",
    "max_concurrent": 2,
    "inflight": 0,
    "queued": 0
  },
  "storage": {"files": 3, "total_mb": 1.234, "expire_minutes": 10, "expire_seconds": 600.0},
  "auth_enabled": false
}
```

### 2. 文本转语音（返回 JSON）

```http
POST /api/tts
Content-Type: application/json

{"text": "你好，这是语音测试。"}
```

```json
{
  "success": true,
  "audio_url": "http://127.0.0.1:8080/audio/1f2e3d4c5b6a7988071625344a5b6c7d.wav",
  "audio_path": "/audio/1f2e3d4c5b6a7988071625344a5b6c7d.wav",
  "filename": "1f2e3d4c5b6a7988071625344a5b6c7d.wav",
  "format": "wav",
  "size_bytes": 96320,
  "expire_minutes": 10,
  "cached": false
}
```

### 3. 直接返回音频文件

```http
POST /api/tts/file
Content-Type: application/json

{"text": "你好，这是语音测试。"}
```

响应为 `audio/wav` 二进制流，并带有 `X-Audio-Url`、`X-Audio-Filename` 响应头。
`POST /api/tts?format=file` 等价。

### 4. 获取音频

```http
GET /audio/{filename}
```

> 音频文件在生成 **10 分钟**后（`storage.expire_minutes`）由后台任务自动删除，
> 过期后再访问会返回 `410 audio_expired`。AstrBot 插件在生成后会立即下载并发送，不受影响。

### 错误响应

```json
{"success": false, "error": {"code": "timeout", "message": "TTS 处理超时（>120s），请稍后重试或减小文本长度。"}}
```

| HTTP | code | 含义 |
| --- | --- | --- |
| 400 | `empty_text` | 文本为空 |
| 401 | `unauthorized` | API Key 错误或缺失 |
| 404 | `audio_not_found` | 音频不存在 |
| 410 | `audio_expired` | 音频已过期 |
| 413 | `text_too_long` | 超过 `storage.max_text_length` |
| 422 | `invalid_request` | 请求体格式错误 |
| 500 | `rvc_failed` / `empty_audio` / `ffmpeg_*` | 生成失败 |
| 503 | `queue_full` / `engine_not_ready` | 队列已满 / 引擎未就绪 |
| 504 | `timeout` | 处理超时 |

---

## 五、curl 测试

```bash
# 1. 健康检查
curl -s http://127.0.0.1:8080/api/health | python3 -m json.tool

# 2. 生成语音，拿到 audio_url
curl -s -X POST http://127.0.0.1:8080/api/tts \
  -H "Content-Type: application/json" \
  -d '{"text":"你好，这是语音测试。"}' | python3 -m json.tool

# 3. 直接下载音频文件（推荐用这个验证音质）
curl -s -X POST http://127.0.0.1:8080/api/tts/file \
  -H "Content-Type: application/json" \
  -d '{"text":"你好，这是语音测试。"}' \
  -o test.wav

# 4. 带 API Key
curl -s -X POST http://127.0.0.1:8080/api/tts \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{"text":"带鉴权的语音测试"}' | python3 -m json.tool

# 5. 访问生成的音频
curl -s -o out.wav http://127.0.0.1:8080/audio/<filename>.wav
```

## 五之二、本地测试客户端（更省事）

项目自带一个纯标准库的测试客户端 `../tools/tts_client.py`，会自动读取本目录的
`config.yaml`（端口 + API Key），无需安装任何依赖：

```bash
python ../tools/tts_client.py health                     # 服务状态
python ../tools/tts_client.py say "你好，这是语音测试。"   # 合成一条并保存
python ../tools/tts_client.py say "你好" --play           # 合成并播放
python ../tools/tts_client.py batch ../tools/sample_texts.txt -c 2
python ../tools/tts_client.py bench --count 8 -c 2 --unique   # 并发压测
python ../tools/tts_client.py filter "你好呀！（开心地笑）"     # 预览插件会发的文本
```

`bench` 输出的**实时倍率**（推理耗时 ÷ 音频时长）是评估 CPU/GPU 是否够用的关键指标：
小于 1 表示比播放速度快，大于 1 表示生成比说话还慢。

> 并发说明：tts-with-rvc 内部使用全局 VC 实例，**RVC 推理本身不支持并发**。
> 本服务已用进程内锁把 RVC 调用串行化，并在调用失败后自动复位库的全局状态
> （否则该库在异常路径不会复位 `can_speak`，会导致后续请求永久卡死、必须重启服务）。
> 想提高并发吞吐请起多个实例（不同端口 + 各自 `OMP_NUM_THREADS`），而不是调大 `queue.max_concurrent`。

> 首次运行提示：tts-with-rvc 会在**当前工作目录**查找 `hubert_base.pt` 与 `rmvpe.pt`，
> 找不到就会从 HuggingFace 下载（合计约 360MB）。如果你已经有这两个文件，
> 直接把它们放进启动服务时的工作目录（默认 `tts-server/`）即可跳过下载。

---

## 六、Linux 部署（systemd）

### 6.0 免安装便携包（推荐给"不想装依赖"的场景）

```bash
# 在 Linux x86_64 构建机上
./deploy/build_linux_bundle.sh --cpu \
  --model /path/YourVoice.pth --index /path/YourVoice.index --assets /path/to/rvc-assets
# Windows/macOS 用 Docker：见 deploy/README-LINUX.md
```

产物解压即用（内置 Python 3.12 运行时 + ffmpeg + 可选权重），目标机不需要
Python/pip/ffmpeg，只需 glibc ≥ 2.28：

```bash
tar -xzf dist/tts-server-linux-amd64-cpu.tar.gz
cd tts-server-linux-amd64-cpu
vi config.yaml          # rvc.model / security.api_key / storage.expire_minutes
./run.sh                # 启动；./tools/check.sh 一键自检
```

想要真正的"可执行文件"而不是便携目录，可用 `deploy/build_linux_executable.sh`（PyInstaller），
产物为 `dist/tts-server/tts-server`，代价是体积更大、首次启动更慢。

```bash
# 1. 部署代码
sudo mkdir -p /opt/voice-tts-system
sudo cp -r . /opt/voice-tts-system/tts-server
cd /opt/voice-tts-system/tts-server

# 2. 创建虚拟环境并安装依赖
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt

# 3. 配置模型与 config.yaml（略）

# 4. 安装 systemd 服务（模板见 ../deploy/tts-server.service）
sudo cp ../deploy/tts-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tts-server
sudo journalctl -u tts-server -f
```

也可以直接用脚本前台/后台启动：

```bash
chmod +x ../deploy/start_tts_server.sh
nohup ../deploy/start_tts_server.sh > tts.log 2>&1 &
```

---

## 七、常见问题

| 现象 | 原因与处理 |
| --- | --- |
| 启动报 `找不到 RVC 模型文件` | `rvc.model` / `rvc.model_dir` 配置不对，用 `--check-config` 快速定位 |
| `RuntimeError: Failed to load audio` | 缺少 ffmpeg，或 ffmpeg 未加入 `PATH` |
| 推理非常慢 | 设备回退到了 CPU；检查 torch 是否装的 CUDA 版本、`nvidia-smi` 是否正常 |
| 显存只有 1~2GB | GPU 跑不动：RVC 实测需要约 2.4GB 常驻、长句峰值约 3GB。服务在 `device: auto` 下会自动改用 CPU（日志有说明），运行中 OOM 也会自动降级 CPU 重试；想强制 GPU 请把 `device` 写成 `cuda:0` |
| 首次请求很慢 | RVC 首次加载模型需要时间，保持 `rvc.preload: true` |
| 想先验证链路 | 把 `rvc.enabled` 设为 `false`，只走 Edge TTS |
| 音频文件会一直留服务器上吗 | 不会。默认只保留 **10 分钟**（`storage.expire_minutes: 10`），后台每 2 分钟扫描一次，并在每次合成请求时顺带清理一次，文件到点即删。旧配置里的 `storage.expire_hours` 仍然兼容（按小时换算） |
