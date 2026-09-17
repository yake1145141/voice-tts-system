# Windows 整合包（打开即用）

把语音处理端做成一个**自带 Python 运行时和全部依赖**的文件夹：复制到任何
Windows x64 机器上，双击 `启动语音服务.bat` 就能用，目标机不需要装 Python / ffmpeg / 任何库。

## 构建

```powershell
# 默认 CUDA 版（体积大，有 NVIDIA 显卡时快 4~5 倍；没显卡也能跑，会自动退回 CPU）
powershell -ExecutionPolicy Bypass -File deploy\windows\build_windows_bundle.ps1

# 纯 CPU 版（体积小很多）
powershell -ExecutionPolicy Bypass -File deploy\windows\build_windows_bundle.ps1 -Variant cpu

# 同时打 zip（没有 7-Zip 时用内置 Python 压缩，比较慢）
powershell -ExecutionPolicy Bypass -File deploy\windows\build_windows_bundle.ps1 -Zip

# 只重新生成 .bat 启动脚本（调脚本模板时用，不会重装依赖）
powershell -ExecutionPolicy Bypass -File deploy\windows\build_windows_bundle.ps1 -LaunchersOnly
```

常用参数：

| 参数 | 说明 |
| --- | --- |
| `-Variant cuda\|cpu` | torch 版本（默认 cuda，即 `2.11.0+cu128`） |
| `-Model` / `-Index` | 内置的 RVC 模型与索引（默认取本机 `Desktop\tts\models\Shaonian.*`） |
| `-Assets` | 内含 `hubert_base.pt` / `rmvpe.pt` 的目录（内置后目标机无需联网下载） |
| `-ApiKey` / `-Port` | 写进 config.yaml 的鉴权 Key 与端口 |
| `-SkipAssets` / `-SkipFfmpeg` | 不内置模型权重 / 不内置 ffmpeg（减小体积） |
| `-Zip` | 额外产出 `<bundle>.zip` |
| `-LaunchersOnly` | 只重写 `.bat` 脚本 |

产物：`dist\windows\tts-server-win64-<变体>\`（当前约 5.5GB，其中 torch+CUDA 约 4.9GB）。

## 目标机使用

```
双击 启动语音服务.bat     → 前台运行，关窗口即停止
双击 后台启动.bat         → 后台常驻（日志 logs\server.log）
双击 停止服务.bat         → 停止后台服务
双击 测试合成.bat [文本]  → 合成并自动播放
双击 首次运行检查.bat     → 校验配置 + 显示 CUDA 是否可用
双击 编辑配置.bat         → 用记事本改 config.yaml
```

浏览器访问 `http://127.0.0.1:8080/api/health` 看到 `"status":"ok"` 即成功。
AstrBot 插件配置见包内 `使用说明.md`（把 `astrbot_plugin_voice_reply` 拷进 AstrBot 插件目录即可）。

## 已实测（本机 RTX 5070）

| 项目 | 结果 |
| --- | --- |
| 引擎 | `device=cuda:0, is_half=true`，CUDA 可用 |
| 合成 6.88s 音频 | 2.58s（首次请求，含预热） |
| 合成 4.76s 音频 | 1.72s |
| 一键脚本 | 启动 / 停止 / 测试合成 / 首次运行检查 均验证通过 |
| 换目录搬迁 | 复制到 `D:\` 后照常运行，模型路径自动跟随 |

包内还带了一个**单文件调用库** `app\voice_tts.py`（零依赖，可直接复制到别的项目）：

```python
import sys; sys.path.insert(0, r"<整合包>\app")
from voice_tts import VoiceTTS

tts = VoiceTTS()                       # 自动读取同目录 config.yaml 的地址与 Key
tts.say("你好，这是一条语音消息。")       # 合成并保存
tts.speak("合成并播放")
tts.synthesize_bytes("只要字节流")        # 不落盘
tts.audio_url("只要链接")
```

## 显存需求（重要）

本机实测（RTX 5070、fp16、rmvpe）：

| 状态 | 显存占用 |
| --- | --- |
| 模型加载完成、空闲 | **2377 MiB** |
| 合成 8 秒音频（峰值） | 约 2.6 GB |
| 合成 21 秒音频（峰值） | **3042 MiB** |

结论：

- **≥ 4GB 显存**：可以放心用 GPU；
- **2.5 ~ 4GB**：能用，但长文本可能吃紧（可在 config.yaml 里调小 `max_text_length` 或换 `pm`/`dio`）；
- **≤ 2GB（含 1GB 卡）**：**GPU 基本跑不动**，请用 CPU。服务已经做了自动处理：
  - `rvc.device: auto` 时，如果检测到显存低于 `rvc.min_vram_mb`（默认 2500），**启动就直接用 CPU**，并在日志里说明原因；
  - 运行中真的 OOM 了，也会**自动降级到 CPU 并重试当前这条**（`rvc.allow_cpu_fallback: true`），不会让语音直接失败；
  - 想强制用显卡：把 `rvc.device` 写成 `cuda:0`。

CPU 模式的参考速度（同一台机器）：5 秒音频约 5.6 秒生成（约 1.1x 实时）；
老一些的 4 核机器大约 2~4 倍实时（10~20 秒一条）。慢但一定能用。

## 打包时踩到的坑（已修）

1. PowerShell here-string 拼 `.bat` 时**少了一个换行**，导致每条脚本的第一行命令被拼到
   `set` 行后面——`停止服务.bat` 因此完全不生效。现在显式补 `"`n"` 并统一 CRLF。
2. `.bat` 里用 `timeout /t N >nul` 在无控制台（stdin 被重定向）时会报
   `Input redirection is not supported`，已改为 `ping -n N 127.0.0.1 >nul`。
3. `.bat` 必须存成 **UTF-8 无 BOM** 并在首行 `chcp 65001`，否则中文提示在 cmd 里是乱码。
