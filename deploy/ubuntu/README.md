# Ubuntu 22.04 语音服务器部署（Edge TTS + RVC + 网页控制台）

本文是一份真实部署记录，照着做可以在任意一台 Ubuntu 22.04 + NVIDIA 显卡上复现。

```
目标机 ssh   root@<TARGET_IP>:<SSH_PORT>     （Ubuntu 22.04，4 核，4G 内存，NVIDIA P106-100 6G）
跳板机 ssh   root@<JUMP_IP>:22        （可选；Proxmox/普通跳板机，转发 50051/50052）
语音服务     http://<TARGET_IP>:50051   （网页控制台就是根路径 /）
Ollama       http://<TARGET_IP>:50052   （systemd ollama，OLLAMA_HOST=0.0.0.0:50052）
```

---

## 一、这个部署的关键决策（踩过的坑）

| 决策 | 原因 |
| --- | --- |
| **Python 必须是 3.12** | `tts-with-rvc` 在 Linux 上依赖 `fairseq-fixed`，PyPI 上只有 **cp312** 的 manylinux wheel。3.10/3.11 会退化成源码编译 fairseq，基本必失败。 |
| **用 python-build-standalone 而不是 deadsnakes PPA** | 国内机器取 launchpad 签名密钥经常超时。独立运行时是单个 tar.gz，解压即用，也不动系统 Python。 |
| **PyTorch 必须用 cu121** | P106-100 是 Pascal 架构 **sm_61**；cu124 之后的 torch 不再编译 sm_61 内核，会报 `no kernel image is available` / `CUDA error: operation not supported`。torch 2.5.1+cu121 的 `arch_list` 覆盖 sm_50~sm_90。 |
| **预置 `hubert_base.pt` / `rmvpe.pt`** | `tts-with-rvc` 是用 `os.getcwd()` 找这两个文件的，找不到就会去 HuggingFace 下载。国内经常连不上，表现成"启动卡好几分钟"。 |
| **设置 `HF_HUB_OFFLINE=1`** | 本地已有权重时禁止 `huggingface_hub` 再发联网检查请求。 |
| **加 swap** | 机器只有 4G 内存，RVC 首次加载模型时容易顶到上限（本机已有 3.8G swap，够用）。 |

---

## 二、部署步骤

```bash
# 0) 跳板机上配置一次免密（可选，方便 scp）
#    本机 ~/.ssh/config 里加：
#      Host rvctts-jump
#        HostName <JUMP_IP>
#        User root
#        IdentityFile ~/.ssh/id_ed25519_rvctts
#      Host rvctts-target
#        HostName <TARGET_IP>
#        Port 50050
#        User root
#        ProxyJump rvctts-jump
#        IdentityFile ~/.ssh/id_ed25519_rvctts

# 1) 系统依赖 + 独立 Python 3.12 + 虚拟环境
scp deploy/ubuntu/bootstrap.sh rvctts-target:/root/
ssh rvctts-target "bash /root/bootstrap.sh"

# 2) PyTorch 2.5.1+cu121 + 全部 Python 依赖（约 2.4GB，最慢的一步）
scp deploy/ubuntu/install_deps.sh rvctts-target:/root/
ssh rvctts-target "bash /root/install_deps.sh"

# 3) 上传服务端代码
ssh rvctts-target "mkdir -p /opt/tts-server/app/static"
scp tts-server/*.py rvctts-target:/opt/tts-server/app/
scp tts-server/static/index.html rvctts-target:/opt/tts-server/app/static/
scp tts-server/requirements.txt rvctts-target:/opt/tts-server/

# 4) 上传 RVC 模型与推理权重（约 440MB）
scp models/Shaonian.pth models/Shaonian.index rvctts-target:/opt/tts-server/models/
scp hubert_base.pt rmvpe.pt rvctts-target:/opt/tts-server/

# 5) 配置 + systemd
scp deploy/ubuntu/config.yaml rvctts-target:/opt/tts-server/config.yaml
scp deploy/ubuntu/tts-server.service rvctts-target:/etc/systemd/system/
ssh rvctts-target "systemctl daemon-reload && systemctl enable --now tts-server"

# 6) 自检
scp deploy/ubuntu/verify.sh rvctts-target:/root/
ssh rvctts-target "bash /root/verify.sh"
```

---

## 三、日常运维

```bash
systemctl status tts-server          # 状态
systemctl restart tts-server         # 改完 config.yaml 重启
journalctl -u tts-server -f          # 实时日志
journalctl -u tts-server -n 100      # 最近 100 行
```

改了 `app/*.py` 之后重新部署代码：

```bash
scp tts-server/*.py tts-server/static/index.html rvctts-target:/opt/tts-server/app/
ssh rvctts-target "systemctl restart tts-server"
```

---

## 四、网页控制台

浏览器打开 **`http://<TARGET_IP>:50051/`**（跨网段可用 `http://<JUMP_IP>:50051/`），页面上有：

* **服务状态**：就绪与否、推理设备（cuda:0 / CPU）、是否降级、f0 算法、语音源
* **显卡状态**：型号、驱动、**利用率 / 显存占用 / 温度 / 功耗 / 风扇 / 频率**（实时刷新，2 秒缓存）
* **显卡占用进程**：谁在用 GPU、用了多少显存
* **主机状态**：内存、swap、磁盘、负载、运行时长
* **合成测试**：输入文字 → 合成并直接在页面播放 / 下载
* **最近请求**：接口、状态码、耗时、来源 IP
* **一键清理**过期音频

页面右上角填 API Key（存在浏览器 localStorage），需要鉴权的接口才会生效。

### 控制台密码

控制台自带一层登录（和 API Key 相互独立）：

```yaml
webui:
  enabled: true
  username: "admin"
  password: "Tts@50051"    # 留空 = 关闭登录，直接打开控制台
  session_hours: 12
```

* 未登录访问 `/` 会被 302 到 `/login`；管理类接口（`/api/gpu`、`/api/system`、
  `/api/stats`、`/api/config`、`/api/cleanup`）返回 401。
* 登录成功后在浏览器里种一个 **HMAC-SHA256 签名的会话 Cookie**（HttpOnly），
  密钥持久化在 `config.yaml` 同目录的 `.webui_secret`，重启服务不会把人踢下线。
* **`/api/health` 与 `/api/tts*` 不受控制台密码影响**：前者给探活/监控用，
  后者由 API Key 保护（AstrBot 没有浏览器 Cookie，不能让它依赖登录态）。
* 注意控制台密码是**明文比较**、Cookie 是 HTTP 明文传输 —— 这样简单的口令验证
  只适合在可信局域网里当"别让人随手点开"的闸门。真要暴露到公网，请在外面套
  Nginx + HTTPS + 更完整的认证。

### 配套 JSON 接口

| 接口 | 鉴权 | 说明 |
| --- | --- | --- |
| `GET /` | 否 | 网页控制台 |
| `GET /login` | 否 | 登录页（未设密码时直接跳回 `/`） |
| `POST /api/login` | 否 | 提交用户名密码，成功下发会话 Cookie |
| `POST /api/logout` | 否 | 注销当前会话 |
| `GET /api/health` | 否 | 服务 + 引擎 + 存储状态 |
| `GET /api/gpu` | 否 | 显卡状态（含 nvidia-smi 原始指标与 torch 架构列表） |
| `GET /api/system` | 否 | CPU / 内存 / 磁盘 / 负载 |
| `GET /api/stats` | 否 | 引擎统计 + 最近请求列表 |
| `GET /api/config` | 是 | 配置概览（不含密钥） |
| `POST /api/cleanup` | 是 | 立即清理过期音频 |
| `POST /api/tts` | 是 | 合成，返回 JSON（含 `audio_url`） |
| `POST /api/tts/file` | 是 | 合成，直接返回音频流（AstrBot 插件用这个） |
| `GET /audio/{name}` | 视配置 | 下载/播放音频；`protect_audio: true` 时需要 `?key=xxx` |

鉴权方式：请求头 `X-API-Key: <key>` 或 `Authorization: Bearer <key>`；
浏览器打不开自定义请求头时可以用查询参数 `?key=<key>`。

---

## 五、排障速查

## 五、同一台机器上的 Ollama（:50052）

这台机器同时跑了 Ollama，默认只监听 `127.0.0.1:11434`，已改成对外监听：

```bash
# 端口/监听地址通过 systemd drop-in 覆盖（不动官方 unit 文件）
cat /etc/systemd/system/ollama.service.d/override.conf
#   [Service]
#   Environment="OLLAMA_HOST=0.0.0.0:50052"

systemctl restart ollama
curl http://<TARGET_IP>:50052/api/tags      # 列出模型
curl http://<TARGET_IP>:50052/api/version
```

已装模型：`qwen2.5:7b`、`qwen2.5-6g:latest`（各约 4.7GB）。

> ⚠️ **显存是共享的**。P106-100 只有 6GB：
> * 语音服务常驻约 0.6GB，推理峰值约 1GB；
> * `qwen2.5:7b` 用 Q4 量化加载约 4.7GB。
>
> 两个同时满载会顶到 6GB 上限。Ollama 会自动少卸载几层到内存（变慢），
> 极端情况会报 OOM。建议：要么给 Ollama 换更小的模型，
> 要么在跑语音时先 `ollama stop qwen2.5:7b`，或设置 `OLLAMA_KEEP_ALIVE=0`
> 让模型用完就释放（代价是每次冷启动变慢）。
>
> 另外 Ollama 本身**没有鉴权**，`0.0.0.0:50052` 等于整个局域网都能调用它，
> 这也是它能被别的机器当 API 用的前提。只想本机用就把 drop-in 里的
> `OLLAMA_HOST` 改回 `127.0.0.1:50052`。

---

## 六、排障速查

| 现象 | 原因 / 处理 |
| --- | --- |
| 日志里 `no kernel image is available` 或 `CUDA error: operation not supported` | torch 没编译这张卡的架构。Pascal 请用 cu121：`bash install_deps.sh` 会装 `2.5.1+cu121` |
| 启动卡几分钟没反应 | 在联网找 `hubert_base.pt`。确认 `/opt/tts-server/hubert_base.pt` 存在，并且 `HF_HUB_OFFLINE=1` |
| `'tuple' object has no attribute 'dtype'` | 找不到 `ffmpeg`。`apt install ffmpeg`，或把 ffmpeg 放到 `/opt/tts-server/bin/` |
| `No audio was received` | 微软在线语音抽风/限流，会自动重试 3 次（`tts.retries`）。Linux 上没有本地 SAPI 兜底 |
| 显存不够 / OOM | 调 `rvc.min_vram_mb`，或把 `rvc.device` 改成 `"cpu"`（慢但能跑） |
| 页面显示"显卡不可用" | 看页面上的原因文字；`nvidia-smi` 能不能跑是第一步 |
