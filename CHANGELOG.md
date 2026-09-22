# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## v1.0.1 — 2026-09-22

修复「服务端一直生成失败（控制台全是 504）」与「`Unit tts-server.service could not be found`」。

### 修复

- **Edge TTS 卡死拖垮整个服务（504 的根因）**：`edge-tts` 库自身**没有任何超时**。
  微软接口一旦"接了连接却不回音频"，`await communicate.save()` 就永久挂住 ——
  不抛异常、不返回、也不释放推理锁，于是后面每一个请求都在等锁，全部排到 180s 超时。
  现在会先给 `edge-tts` 套上 `tts.edge_timeout`（默认 60s）超时，卡住即抛错，
  走「重试 → 离线语音兜底」的正常链路。
- **推理卡死的自愈**：新增 `queue.hard_timeout`（默认 100s，强制小于 `queue.timeout`）。
  库调用超过硬上限、或推理锁被占用超过硬上限，即判定进程已卡死并**主动退出**，
  由 systemd（`Restart=always`）在几秒内拉起。卡在 C 层网络等待上的线程无法从 Python
  层面中断，重启是唯一干净的恢复方式。

### 新增

- `deploy/linux/install-systemd.sh`：给"手工跑起来、但没注册 systemd 服务"的机器补装服务。
  自动探测安装目录 / Python 解释器 / 端口，停掉手工起的旧进程，写入单元文件并 `enable --now`，
  修掉 `systemctl status tts-server` → `Unit tts-server.service could not be found.`
- 卡死保护回归测试（`tests/test_tts_server_api.py`）：超时补丁幂等、硬超时必须小于请求超时、
  库调用卡死与锁被卡死均触发重启。

### 配置新增（老配置可以不管，会用默认值）

```yaml
tts:
  edge_timeout: 60      # 单次 Edge TTS 的超时（秒）
queue:
  hard_timeout: 100     # 单次库调用的硬上限（秒），超过判定卡死并重启服务
```

### 运维提醒

自愈（主动退出 + 自动拉起）依赖 systemd 托管。用 `nohup` / `.bat` 手工起的进程，
卡死后不会自动重启，请改用 systemd（Linux）或 `后台启动.bat` + 任务计划（Windows）。

## v1.0.0 — 2026-09-17

首个正式版本。

### 服务端

- Edge TTS（`zh-CN-YunxiNeural`）+ RVC 音色转换的 HTTP 语音合成服务
- **长文本自动分段**：显存峰值只与单段长度有关，2GB 显存的小卡也能处理长文本
- **离线语音兜底**：在线语音失败时自动改用本地语音（Windows SAPI / Linux espeak-ng）
- 在线语音瞬时故障自动重试（空音频、断连、超时）
- 显卡架构智能识别：兼容 Pascal（sm_61，P106-100 / P4 / P40 / GTX 10 系）等老卡
- 显存不足自动降级 CPU 重试，不会让请求失败
- 音频按文本缓存 + 过期自动清理（默认 10 分钟）
- **网页控制台**：显卡状态 / 主机状态 / 在线试听 / 最近请求 / 一键清理
- 网页控制台支持密码登录（HMAC 签名会话 Cookie）
- 完全离线的资源查找：预置 `hubert_base.pt` / `rmvpe.pt`，不需要访问 HuggingFace

### 客户端

- AstrBot 插件：拦截 AI 文本 → 过滤括号内容 → 合成语音 → 发送语音消息，失败自动降级文字
- 命令行客户端 `tts_client.py`：health / say / batch / bench / filter
- 单文件客户端库 `voice_tts.py`：零依赖，拖进任何 Python 项目即可调用
- 安卓配置 App：在手机上配置并启动安卓版服务端

### 部署

- Windows 一键整合包（内置 Python 运行时、CUDA 版 PyTorch、ffmpeg）
- Linux 一键安装脚本（自动装 Python 3.12、PyTorch、systemd 服务）
- Docker / docker-compose
- Google Colab Notebook（白嫖免费 GPU）
- 安卓 Termux + chroot 部署

### 已知限制

- 线上语音（Edge TTS）依赖微软公开接口，偶发抖动；已加重试与离线兜底
- 服务端尚未内置鉴权以外的访问控制，暴露公网请自行套反向代理与 HTTPS
