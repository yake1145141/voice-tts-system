# astrbot_plugin_voice_reply — AI 语音回复（tts-with-rvc）

[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

把 AstrBot 的**文本回复**自动转换成**语音回复**：插件在「发送消息前」拦截 AI 的回复，
过滤掉括号/中括号里的动作、情绪描写，再把剩下的**可朗读文本**发给你自己的
[tts-with-rvc](https://github.com/Atm4x/tts-with-rvc) 语音服务，最后把文本消息段
替换成语音消息段。

> 核心原则：**能语音就语音，不能语音就文字**。任何一步失败都不会影响 AI 原本的文字回复。

## 🇨🇳 国内拉取加速

GitHub 直连慢或者连不上？在地址前面加上 `https://gh-proxy.cn/` 就能加速：

```bash
# 直连（国外网络）
git clone https://github.com/yake1145141/astrbot_plugin_voice_reply.git

# 国内加速（推荐国内用户用这条）
git clone https://gh-proxy.cn/https://github.com/yake1145141/astrbot_plugin_voice_reply.git
```

> 加速服务由 **[gh-proxy.cn](https://gh-proxy.cn)** 提供，使用帮助见 **[www.gh-proxy.cn](https://www.gh-proxy.cn/)**。
> 纯公益加速站，国内拉 GitHub 代码、Release、raw 文件都很快，推荐收藏。

这个插件只是**客户端**。它需要配合一个独立部署的语音服务端才能工作，
服务端的搭建（含显卡要求、模型配置、网页控制台、Windows/Linux/Docker/Colab 各种部署方式）
全部写在下面这个仓库里：

> ### 👉 服务端仓库：[yake1145141/voice-tts-system](https://github.com/yake1145141/voice-tts-system)
>
> 里面包含：
> * 服务端源码（Edge TTS + RVC 的 HTTP 合成服务）
> * **完整部署文档**：Linux 一键安装脚本 / Docker / Windows 整合包 / Google Colab / 安卓
> * 硬件要求说明：**显存最低 2 GB**，P106-100 这类 Pascal 老卡也能用
> * 网页控制台（显卡状态、主机状态、在线试听）
> * 命令行客户端、单文件调用库、安卓配置 App

### 📖 图文教程（推荐先看这个）

**<https://rvc-tts.top/>**

在线文档站，左侧是完整目录树，从硬件选型一路讲到排障，
比翻 README 舒服得多。服务端搭建的每一步都有截图和可复制的命令。

---

## 服务端搭建（必读）

**这个插件不能单独使用**，必须先在工作电脑或服务器上跑起语音服务端，拿到它的地址和密钥。

完整步骤请看 **[voice-tts-system 的部署文档](https://github.com/yake1145141/voice-tts-system/blob/main/DEPLOY.md)**，
最省事的两种方式：

```bash
# Linux 一键安装（自动装 Python 3.12 + PyTorch cu121 + 注册 systemd 服务）
git clone https://github.com/yake1145141/voice-tts-system.git
# 国内网络慢的话，改用加速地址：
# git clone https://gh-proxy.cn/https://github.com/yake1145141/voice-tts-system.git
cd voice-tts-system
sudo bash deploy/linux/install.sh \
     --model /path/你的模型.pth --index /path/你的模型.index \
     --api-key your-secret-key
```

```powershell
# Windows：解压整合包后双击「启动语音服务.bat」
# 没有整合包就用仓库里的脚本自己构建：
git clone https://github.com/yake1145141/voice-tts-system.git
# 国内网络慢的话，改用加速地址：
# git clone https://gh-proxy.cn/https://github.com/yake1145141/voice-tts-system.git
cd voice-tts-system
powershell -ExecutionPolicy Bypass -File deploy\windows\build-bundle.ps1 `
  -Model D:\models\MyVoice.pth -Index D:\models\MyVoice.index
```

装好后打开网页控制台 `http://<服务器IP>:8080/` 确认「服务状态 正常」，
然后回到本页继续安装插件。

---

## 功能

| 功能 | 说明 |
| --- | --- |
| 拦截 AI 文本回复 | 使用 AstrBot 官方 `on_decorating_result`（发送消息前）事件钩子，不使用任何废弃 API |
| 括号内容过滤 | 删除 `（）` `()` `[]` `【】` 及其内部内容，支持**嵌套**（`你好！（开心地说：[笑]）` → `你好！`） |
| 可朗读文本判断 | 过滤后为空（如整个回复是 `[笑]`）时不调用 TTS，正常发送原文 |
| 最大长度限制 | 超过 `voice.max_text_length` 直接发文字，不浪费 GPU |
| TTS 失败自动降级 | 连接失败 / 超时 / RVC 失败 / API Key 错误 / 音频为空 → 恢复原文发送 |
| 并发与排队 | 异步 HTTP + 信号量限流，多个会话同时回复不会拖垮语音服务 |
| 防重复处理 | 事件标记 + 消息段替换双重保证，插件产生的消息不会被再次送去 TTS |
| 语音消息发送 | 使用官方 `Record` 消息段；不支持语音的平台自动回退文字 |
| 管理指令 | `/voice on` `/voice off` `/voice status`（仅管理员） |
| WebUI 配置 | 提供 `_conf_schema.json`，可在 AstrBot 管理面板直接修改 |
| 缓存清理 | 插件下载的音频会按 `cache_expire_minutes` 自动清理 |

---

## 安装

1. 先部署好**语音服务端**（步骤见上一节，仓库：
   [yake1145141/voice-tts-system](https://github.com/yake1145141/voice-tts-system)），
   确认 `curl http://127.0.0.1:8080/api/health` 正常返回。
2. 把本仓库克隆进 AstrBot 的插件目录：

   ```bash
   cd AstrBot/data/plugins
   git clone https://github.com/yake1145141/astrbot_plugin_voice_reply.git
   # 国内网络慢的话，改用加速地址：
   # git clone https://gh-proxy.cn/https://github.com/yake1145141/astrbot_plugin_voice_reply.git
   ```

   也可以用 AstrBot 插件市场 / 手动下载 zip 解压到同一位置。目录名保持
   `astrbot_plugin_voice_reply`，里面应该直接就是 `main.py`、`metadata.yaml`。

3. 打开 AstrBot WebUI → 插件管理 → 找到 `astrbot_plugin_voice_reply` → 点击 **启用 / 重载插件**。
4. 在插件配置页填写 `TTS 服务地址`（默认 `http://127.0.0.1:8080`），
   如果语音服务端配置了 `security.api_key`，在 `TTS 服务 API Key` 里填入同样的值。

> 建议把 AstrBot 自带的「语音合成（TTS）」功能关掉，避免两套 TTS 同时生效。

---

## 配置项（WebUI 可视化配置）

```yaml
tts_server:
  url: "http://127.0.0.1:8080"   # 语音处理端地址
  api_key: ""                    # 与服务端 security.api_key 保持一致
  delivery: "auto"               # auto / file / url

voice:
  enabled: true                  # 总开关（也可用 /voice on|off）
  max_text_length: 300           # 超过该长度直接发文字
  timeout: 60                    # 单次 TTS 请求超时（秒）
  max_concurrent: 2              # 最大并发请求数
  keep_text: false               # true = 语音 + 原文一起发
  only_llm_result: false         # true = 只处理大模型回复
  cleanup_markdown: true         # 去掉 ** ` # 等 Markdown 符号
  cache_expire_minutes: 60       # 本地音频缓存过期时间
  skip_platforms:                # 这些平台不支持语音消息，直接发文字
    - qq_official
    - qq_official_webhook
    - dingtalk
    - lark
```

### delivery 三种模式

| 值 | 行为 | 适用场景 |
| --- | --- | --- |
| `file` | 插件通过 `POST /api/tts/file` 下载音频到本地，再以本地文件发送 | AstrBot 与协议端在同一台机器（最稳） |
| `url` | 插件拿到 `audio_url` 后直接交给消息平台拉取 | 语音服务地址可被 QQ/Telegram 等平台访问 |
| `auto`（默认） | 先试 `file`，失败再退回 `url` | 大多数场景 |

---

## 指令

| 指令 | 权限 | 说明 |
| --- | --- | --- |
| `/voice on` | 管理员 | 开启语音回复（会持久化，重启后保持） |
| `/voice off` | 管理员 | 关闭语音回复，AI 回复全部以文字发送 |
| `/voice status` | 管理员 | 查看开关、服务地址、健康状态、本次运行统计 |

普通用户无需任何指令即可正常使用语音回复，管理指令使用 AstrBot 官方
`PermissionType.ADMIN` 权限校验（管理员 ID 在 AstrBot 主配置的 `admins_id` 中设置）。

---

## 处理流程

```text
AI 文本回复
   │
   ▼
[VoiceReply] 收到文本回复
   │
   ▼  过滤 （） () [] 【】 及其内部内容（支持嵌套）
[VoiceReply] 过滤括号内容后 TTS 文本长度：42
   │
   ├─ 过滤后为空 ────────────────► 发送原始文字
   ├─ 超过 max_text_length ─────► 发送原始文字
   │
   ▼
[VoiceReply] 正在请求 TTS 服务
   │
   ├─ 失败/超时/鉴权错误 ───────► [VoiceReply] 已降级为文字回复
   │
   ▼
[VoiceReply] TTS 生成成功，正在发送语音
   │
   ▼
把 Plain（文本）消息段替换为 Record（语音）消息段 → AstrBot 发送
```

### 文本处理示例

| 输入 | TTS 文本 |
| --- | --- |
| `你好呀！` | `你好呀！` |
| `你好呀！（开心地笑）` | `你好呀！` |
| `Hello! (smile)` | `Hello!` |
| `你好！[挥手]` + 换行 + `很高兴认识你！` | `你好！` + 换行 + `很高兴认识你！` |
| `【系统提示】你好，很高兴见到你。` | `你好，很高兴见到你。` |
| `你好！（开心地说：[笑]）` | `你好！` |
| `（开心）[笑]【挥手】` | 空 → 正常发送原始文字 |

---

## 平台支持

| 平台 | 语音消息 | 说明 |
| --- | --- | --- |
| QQ 个人号（aiocqhttp / NapCat / Lagrange） | ✅ | 推荐 |
| Telegram | ✅ | |
| 企业微信（wecom） | ✅ | |
| QQ 官方接口、钉钉、飞书 | ❌ | 适配器不支持 `Record`，插件自动发文字（见 `skip_platforms`） |

---

## 常见问题

**Q：AI 回复直接变成了文字，没有语音？**
查看 AstrBot 日志中 `[VoiceReply]` 开头的行：

- `TTS 服务连接失败` → 检查 `tts_server.url`、防火墙、服务是否启动。
- `TTS 服务返回错误 [unauthorized]` → 两端 API Key 不一致。
- `文本长度 xx 超过上限` → 调大 `voice.max_text_length`。
- `过滤后没有可朗读文本` → 该回复只有括号内容，属于预期行为。
- `平台 qq_official 不支持语音消息` → 换了平台或使用了 QQ 官方接口。
- `检测到 AstrBot 流式输出` → 请在 AstrBot 配置中关闭「流式输出（streaming_response）」。
  流式模式下文本会逐段发给用户、结束时不再发送完整消息，因此无法替换为语音。

**Q：第一次语音回复特别慢？**
RVC 首次推理需要加载模型（10~60 秒），语音服务端已默认开启 `rvc.preload` 预热。

**Q：语音太长？**
调小 `voice.max_text_length`（例如 150），超长回复会直接以文字发送。

**Q：多个群同时说话会卡吗？**
插件使用信号量限制并发（`max_concurrent`），语音服务端也有自己的队列与并发限制，
超出部分会排队或直接降级为文字，不会阻塞 AstrBot 主事件循环。

---

## 国内加速与致谢

### 拉代码 / 下 Release 慢怎么办

用 **[gh-proxy.cn](https://gh-proxy.cn)**（使用帮助：**[www.gh-proxy.cn](https://www.gh-proxy.cn/)**）——
在任意 GitHub 地址前面加个前缀就行：

```bash
# 直连
git clone https://github.com/yake1145141/astrbot_plugin_voice_reply.git

# 国内加速
git clone https://gh-proxy.cn/https://github.com/yake1145141/astrbot_plugin_voice_reply.git
```

同样适用于服务端仓库和它的 Release 附件。纯公益加速站，国内实测很稳，推荐收藏。

### 致谢

* AstrBot 插件框架：[AstrBot](https://github.com/AstrBotDevs/AstrBot)
* 语音服务端：[voice-tts-system](https://github.com/yake1145141/voice-tts-system)
* 国内 GitHub 加速：[gh-proxy.cn](https://gh-proxy.cn) / [www.gh-proxy.cn](https://www.gh-proxy.cn/)
* 本项目以 MIT 协议开源，详见 [LICENSE](LICENSE)
