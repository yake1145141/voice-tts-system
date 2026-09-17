# 部署文档

> 📖 **在线版（带左侧目录导航、搜索）：<https://rvc-tts.top/>**
>
> 本文和文档站的内容一致，挑你顺手的看。

本文覆盖**服务端**和**客户端**在全部支持平台上的完整部署步骤。
只想快点跑起来的话，看 [README 的快速开始](README.md#三快速开始)；
硬件选型看 [docs/HARDWARE.md](docs/HARDWARE.md)。

目录：

1. [部署前准备](#一部署前准备)
2. [服务端 · Linux 一键安装](#二服务端--linux一键安装推荐)
3. [服务端 · Windows 整合包](#三服务端--windows-整合包)
4. [服务端 · Docker](#四服务端--docker)
5. [服务端 · Google Colab](#五服务端--google-colab)
6. [服务端 · 安卓](#六服务端--安卓)
7. [服务端配置详解](#七服务端配置详解)
8. [验证服务端](#八验证服务端)
9. [客户端 · AstrBot 插件](#九客户端--astrbot-插件)
10. [客户端 · 命令行 / 单文件库](#十客户端--命令行与单文件库)
11. [客户端 · 安卓配置 App](#十一客户端--安卓配置-app)
12. [排障速查](#十二排障速查)

---

## 一、部署前准备

### 1.1 选机器

服务端需要一台**一直开着**的机器，优先级：

1. **有 NVIDIA 显卡的 Linux 机器**（最推荐，性能好、部署最省心）
2. **有 NVIDIA 显卡的 Windows 机器**（用整合包，双击即可）
3. **纯 CPU 机器**（能用，但慢 3~5 倍）
4. **Google Colab**（没有机器时白嫖免费 GPU，需要保持页面在线）

显卡只要是 NVIDIA、计算能力 ≥ 6.0 就能用，**P106-100 这种矿卡完全没问题**。
显存最低 2GB。详见 [docs/HARDWARE.md](docs/HARDWARE.md)。

### 1.2 装显卡驱动

```bash
# Ubuntu：先用官方仓库装驱动（新卡建议用 NVIDIA 官方 .run 或 CUDA 仓库）
sudo apt update
sudo ubuntu-drivers autoinstall
sudo reboot

# 重启后确认
nvidia-smi
```

只要 `nvidia-smi` 能打印出显卡信息即可，**不需要单独装 CUDA Toolkit** ——
PyTorch 自带的 CUDA 运行库就够了。

### 1.3 准备 RVC 模型

你需要一个 RVC 的 `.pth` 模型（可选再带一个 `.index`）。来源：

* 自己训练的 RVC 模型
* 社区分享的模型（**使用他人声音请确保已获得授权**）

把文件准备好，安装脚本会用 `--model` / `--index` 参数接收。

服务端额外需要两个预置权重：`hubert_base.pt`、`rmvpe.pt`。
安装脚本会自动下载（国内会走镜像）；如果你本地已有，用 `--assets` 指定目录可以省掉下载。

---

## 二、服务端 · Linux 一键安装（推荐）

支持 Ubuntu 20.04 / 22.04 / 24.04、Debian 11+（x86_64）。

### 2.1 一条命令

```bash
sudo bash deploy/linux/install.sh \
     --model /root/models/MyVoice.pth \
     --index /root/models/MyVoice.index \
     --assets /root/models \
     --api-key 'your-secret-key' \
     --port 8080
```

脚本会依次完成：

1. 安装系统依赖（ffmpeg、gcc、espeak-ng 等）
2. 安装独立 Python 3.12 运行时（不动系统 Python）
3. 创建虚拟环境并安装 **PyTorch 2.5.1+cu121**（Pascal 老卡的唯一正确版本）
4. 安装 `tts-with-rvc` 及全部依赖
5. 复制服务端代码到 `/opt/tts-server`
6. 写入 `config.yaml`（含你给的模型、密钥、端口）
7. 注册并启动 systemd 服务 `tts-server`
8. 做一次端到端自检并打印结果

### 2.2 常用参数

| 参数 | 说明 |
| --- | --- |
| `--model PATH` | RVC 模型 `.pth`（必需） |
| `--index PATH` | RVC 索引 `.index`（可选但建议） |
| `--assets DIR` | 内含 `hubert_base.pt` / `rmvpe.pt` 的目录（可选，省下载） |
| `--api-key KEY` | 服务密钥（不填会随机生成并打印出来） |
| `--port N` | 监听端口（默认 8080） |
| `--device auto\|cuda:0\|cpu` | 推理设备（默认 auto） |
| `--max-chars N` | 单段最大字数（默认 80；2GB 显存填 60） |
| `--webui-password P` | 网页控制台密码（不填则打开控制台不需要登录） |
| `--install-dir DIR` | 安装目录（默认 `/opt/tts-server`） |
| `--cpu` | 强制安装 CPU 版 PyTorch（没有 N 卡时用） |
| `--dry-run` | 只打印步骤，不实际执行 |

### 2.3 装完之后

```bash
systemctl status tts-server      # 服务状态
journalctl -u tts-server -f      # 实时日志
systemctl restart tts-server     # 改完 config.yaml 重启

bash deploy/linux/uninstall.sh   # 需要卸载时（保留模型目录）
```

网页控制台：`http://<服务器IP>:8080/`

---

## 三、服务端 · Windows 整合包

### 3.1 用现成的整合包

整合包是**解压即用**的绿色包（内置 Python 运行时 + CUDA 版 PyTorch + ffmpeg），
里面自带启动脚本：

```text
tts-server-win64-cuda\
├── 启动语音服务.bat     ← 双击启动（关掉窗口就停止）
├── 首次运行检查.bat     ← 第一次先跑这个，确认 torch / 显卡正常
├── 诊断信息.bat         ← 出问题时跑它，把 diagnostics.txt 发给别人求助
├── 编辑配置.bat         ← 打开 config.yaml
├── 查看日志.bat / 测试合成.bat / 停止服务.bat / 后台启动.bat
├── 应用补丁.bat         ← 覆盖安装补丁
├── 升级到老显卡版torch.bat  ← 老卡（P4/P40/P106 等 sm_61）专用
├── models\              ← 放你的 .pth / .index
├── config.yaml
└── app\  python\  bin\  output\  logs\
```

步骤：

1. 解压到**不含中文和空格的路径**，例如 `D:\tts-server-win64-cuda`
2. 把 `.pth` / `.index` 放进 `models\`
3. 双击 `编辑配置.bat`，改 `rvc.model`、`rvc.index`、`security.api_key`
4. 双击 `首次运行检查.bat`，确认输出里 `cuda_available True`
   * 如果报 `no kernel image` 或 `operation not supported` → 双击 `升级到老显卡版torch.bat`
5. 双击 `启动语音服务.bat`

> 老显卡（P106-100 / P4 / P40）**必须**先跑一次 `升级到老显卡版torch.bat`，
> 它会把 PyTorch 换成 `2.5.1+cu121`，这是唯一包含 sm_61 兼容内核的版本。

### 3.2 自己构建整合包

```powershell
powershell -ExecutionPolicy Bypass -File deploy\windows\build-bundle.ps1 `
  -Model D:\models\MyVoice.pth -Index D:\models\MyVoice.index
```

产物在 `dist\tts-server-win64-cuda\`，压缩后约 3.5GB。

---

## 四、服务端 · Docker

适合已经用 Docker 管理服务的场景。**需要 nvidia-container-toolkit**。

```bash
# 1. 装 NVIDIA Container Toolkit（一次性）
#    https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html

# 2. 准备目录
mkdir -p /srv/tts/{models,output,logs}
cp MyVoice.pth MyVoice.index /srv/tts/models/

# 3. 启动
cd deploy/docker
export TTS_MODEL_DIR=/srv/tts/models TTS_OUTPUT_DIR=/srv/tts/output
docker compose up -d

# 4. 看日志
docker compose logs -f
```

`docker-compose.yml` 里已经写好 `deploy.resources.reservations.devices` 的 GPU 映射，
以及 `NVIDIA_VISIBLE_DEVICES=all`。

---

## 五、服务端 · Google Colab

没有显卡时可以用 Colab 的免费 GPU。打开
`deploy/colab/tts-server-colab.ipynb`，按顺序执行单元格即可；
最后一个单元格会给出一个临时公网地址（cloudflared 隧道）。

注意：Colab 会不定期断开，只适合临时体验，不适合长期挂机。

---

## 六、服务端 · 安卓

用 Termux + proot/chroot 在安卓手机上跑（需要 root 的手机体验更好）。
步骤见 `deploy/android/README.md`
（也可以直接用发布包里 `client-package/android/` 的配置 App 管理）。

---

## 七、服务端配置详解

配置文件是 `config.yaml`，**所有相对路径都相对于配置文件所在目录**。
改完记得重启服务。

### server

```yaml
server:
  host: "0.0.0.0"          # 0.0.0.0 = 局域网可访问；只本机用可改 127.0.0.1
  port: 8080
  log_level: "INFO"        # DEBUG / INFO / WARNING
  public_base_url: ""      # 生成 audio_url 用的对外地址，留空自动推断
```

### security

```yaml
security:
  api_key: "your-secret-key"   # 留空 = 不校验（不推荐）
  protect_audio: false         # true 时 /audio/xxx 也要带 key
```

### tts — 语音源

```yaml
tts:
  source: "auto"           # edgetts / sapi / espeak / auto
  speaker: "zh-CN-YunxiNeural"
  pitch: 5                 # RVC 变调半音数（正数更接近女声）
  rate: 0                  # 语速百分比
  volume: 0
  retries: 3               # 在线语音失败重试次数
  sapi_voice: ""           # Windows 本地语音名，留空自动挑中文语音
  espeak_voice: "cmn"      # Linux 本地语音（普通话）
```

| source | 行为 |
| --- | --- |
| `edgetts` | 只用微软在线语音（音色最自然，需要联网） |
| `sapi` | 只用 Windows 自带离线语音 |
| `espeak` | 只用 espeak-ng 离线语音（Linux/macOS） |
| `auto` | **先用在线，失败自动转本地**（推荐） |

### chunk — 长文本分段（低显存关键配置）

```yaml
chunk:
  enabled: true
  max_chars: 80     # 单段最大字数：2GB 显存填 60，4GB 填 120，6GB+ 可填 200
  min_chars: 10     # 相邻过短片段合并阈值
```

显存峰值只与这个值有关，与文本总长度无关。

### rvc

```yaml
rvc:
  enabled: true
  model: "MyVoice.pth"     # 相对 model_dir，也可写绝对路径
  model_dir: "./models"
  index: "MyVoice.index"
  f0_method: "rmvpe"       # rmvpe 音质最好；pm / dio 更快
  device: "auto"           # auto / cuda:0 / cpu
  min_vram_mb: 1800        # 低于该值直接用 CPU（2GB 卡填 1800）
  allow_cpu_fallback: true # 运行中显存不够时自动降级 CPU 重试
  is_half: true            # 半精度（CPU 上自动关闭）
  preload: true            # 启动即加载模型
```

### webui — 网页控制台

```yaml
webui:
  enabled: true
  username: "admin"
  password: ""             # 留空 = 打开控制台不需要登录
  session_hours: 12
```

### storage / queue / audio

```yaml
storage:
  output_dir: "./output"
  expire_minutes: 10       # 生成的音频多少分钟后自动删除
  cache_by_text: true      # 相同文本复用音频
  max_text_length: 2000    # 单次请求最大文本长度

queue:
  max_concurrent: 1        # RVC 推理由底层库串行化，保持 1 最稳
  max_queue_size: 16
  timeout: 180             # 单任务超时（长文本会自动放宽）

audio:
  output_format: "wav"     # wav 或 mp3
```

### 环境变量覆盖

不想改配置文件时可以用环境变量（docker / systemd 部署方便）：

```text
TTS_SERVER_HOST / TTS_SERVER_PORT / TTS_SERVER_API_KEY
TTS_SERVER_OUTPUT_DIR / TTS_SERVER_LOG_LEVEL / TTS_SERVER_CONFIG
RVC_MODEL / RVC_MODEL_DIR / RVC_DEVICE / RVC_ENABLED
```

---

## 八、验证服务端

### 8.1 健康检查

```bash
curl http://127.0.0.1:8080/api/health
```

重点看这几项：

```json
{
  "engine": {
    "ready": true,
    "device": "cuda:0",              ← 用的是 GPU 还是 CPU
    "gpu_detail": "sm_61（复用 sm_60 内核）",
    "device_fallback": false,        ← 是否已经降级过
    "rvc_enabled": true
  }
}
```

### 8.2 合成测试

```bash
curl -X POST http://127.0.0.1:8080/api/tts/file \
  -H "X-API-Key: your-secret-key" \
  -H "Content-Type: application/json" \
  -d '{"text":"你好，这是一条语音测试。"}' \
  -o test.wav

ffprobe test.wav      # 查看时长，正常应该有内容
```

### 8.3 用自带客户端验证

```bash
python tools/tts_client.py \
  --url http://127.0.0.1:8080 --api-key your-secret-key health

python tools/tts_client.py \
  --url http://127.0.0.1:8080 --api-key your-secret-key say "你好" --play

# 压测（看 RTF 和成功率）
python tools/tts_client.py \
  --url http://127.0.0.1:8080 --api-key your-secret-key bench --count 10
```

> 发布包里的 `client-package/tts_client.py` 是同一份文件，开箱即用、不需要克隆仓库。

### 8.4 网页控制台

浏览器打开 `http://<服务器IP>:8080/`，能看到：

* 服务状态、推理设备（`cuda:0 · fp16`）、显卡架构
* **显卡状态**：利用率 / 显存 / 温度 / 功耗 / 风扇 / 频率，以及占用显卡的进程
* 主机状态：内存、swap、磁盘、负载
* **合成测试**：输入文字直接试听
* 最近请求列表
* 一键清理过期音频

---

## 九、客户端 · AstrBot 插件

> 插件现在也有**独立仓库**，只想装插件的话直接克隆那个就行：
> **https://github.com/yake1145141/astrbot_plugin_voice_reply**
>
> 本仓库里的 `astrbot_plugin_voice_reply/` 是同一份源码（打包整合包时要用到），两边内容保持一致。

### 9.1 安装

把插件目录 `astrbot_plugin_voice_reply/` 整个拷贝到 AstrBot 的插件目录
（发布包里对应的是 `client-package/astrbot-plugin-voice-reply-v1.0.0.zip`，解压即得同一份内容）：

```bash
cd AstrBot/data/plugins
git clone https://github.com/yake1145141/astrbot_plugin_voice_reply.git
```

```text
AstrBot/
└── data/
    └── plugins/
        └── astrbot_plugin_voice_reply/
            ├── main.py
            ├── metadata.yaml
            ├── _conf_schema.json
            └── README.md
```

然后在 AstrBot 的插件管理页面**重载插件**。

### 9.2 配置

必填项只有两个：

| 配置项 | 说明 |
| --- | --- |
| `tts_server_url` | 服务端地址，例如 `http://192.168.1.10:8080` |
| `api_key` | 与服务端 `security.api_key` 保持一致 |

常用可选项：

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | true | 总开关 |
| `max_text_length` | 200 | 超过这个长度就发文字（避免长语音） |
| `only_private` | false | 只在私聊里用语音 |
| `strip_brackets` | true | 过滤 `（动作）` `【情绪】` 等括号内容 |
| `timeout` | 180 | 请求服务端的超时（秒） |
| `keep_text_on_failure` | true | 语音失败时保留原文字回复 |

### 9.3 验证

在任意支持的平台给 AI 发一句话，正常的话会收到一条语音消息。
如果收到的是文字，先看服务端日志，再看 AstrBot 日志里的插件报错。

---

## 十、客户端 · 命令行与单文件库

### 10.1 命令行客户端 `tts_client.py`

只用标准库，不需要安装任何依赖：

```bash
python tts_client.py health                      # 健康检查
python tts_client.py say "你好，这是语音测试。"    # 合成一条
python tts_client.py say "你好" --play            # 合成并播放
python tts_client.py batch texts.txt             # 逐行批量合成
python tts_client.py bench --count 10 -c 2       # 并发压测
python tts_client.py filter "你好呀！（开心地笑）"  # 预览插件实际会发的文本
```

地址与密钥的查找顺序：
`--url/--api-key` > 环境变量 `TTS_SERVER_URL/TTS_SERVER_API_KEY` >
`config.yaml` > 默认 `http://127.0.0.1:8080`。

### 10.2 单文件客户端库 `voice_tts.py`

把它复制进你自己的项目，就能三行调用：

```python
from voice_tts import VoiceTTS

tts = VoiceTTS("http://192.168.1.10:8080", api_key="your-secret-key")
path = tts.say("你好，这是一条语音消息。")   # 合成并保存，返回文件路径
```

其它常用方法：

```python
tts.speak("直接播放这句话")          # 合成 + 播放
data = tts.synthesize_bytes("只要字节流")   # 不落盘，拿 wav bytes
url  = tts.audio_url("给我一个下载链接")     # 交给别的平台拉取
tts.health()                        # 服务状态
if tts.available(): ...             # 服务可用性（失败不抛异常）
```

---

## 十一、客户端 · 安卓配置 App

`client-package/android/tts-server-config.apk` 是给**安卓版服务端**用的配置面板：
在手机上填写端口、API Key、模型路径等，一键启动/停止 chroot 里的服务端，
并查看运行状态。

```bash
adb install -r tts-server-config.apk
```

---

## 十二、排障速查

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `no kernel image is available` / `CUDA error: operation not supported` | PyTorch 没编译这张卡的架构（Pascal 老卡最常见） | 装 `torch 2.5.1+cu121`；Windows 双击 `升级到老显卡版torch.bat` |
| 启动后卡住几分钟没反应 | 在联网找 `hubert_base.pt` | 把权重放到服务端根目录，脚本会自动设 `HF_HUB_OFFLINE=1` |
| `'tuple' object has no attribute 'dtype'` | 找不到 `ffmpeg` | `apt install ffmpeg`，或把 ffmpeg 放进 `<服务目录>/bin/` |
| `No audio was received` | 微软在线语音抽风 / 限流 | 会自动重试；`tts.source: auto` 时还会自动转本地离线语音 |
| `CUDA out of memory` | 显存不够 | 调小 `chunk.max_chars`，或调高 `rvc.min_vram_mb` 让它走 CPU |
| 网页控制台打不开 | 端口 / 防火墙 | `ss -lntp \| grep 8080`；检查防火墙与 `server.host` |
| 控制台提示未授权 | 设了 `webui.password` | 用配置里的用户名密码登录；或把密码清空 |
| 插件一直发文字不发语音 | 服务端不可达或密钥不对 | 在 AstrBot 机器上 `curl http://服务端:端口/api/health` |
| 日志里 `pkg_resources` 报错 | setuptools ≥ 81 删掉了它 | `pip install "setuptools<81"`（安装脚本已自动处理） |
| 声音断续 / 分段处有停顿 | 长文本分段拼接 | 调大 `chunk.max_chars`（需要更多显存），或接受轻微停顿 |

还找不到原因时，跑 `python tools/tts_client.py --url ... health`（发布包里是
`client-package/tts_client.py`），把输出和服务端日志一起贴出来。
