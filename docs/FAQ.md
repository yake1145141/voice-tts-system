# 常见问题

## 硬件相关

**Q：最低需要什么显卡？**
A：NVIDIA、计算能力 ≥ 6.0、显存 ≥ 2 GB。P106-100 这种矿卡完全可以，
实测 RTF 0.41x（比实时快一倍多）。详见 [HARDWARE.md](HARDWARE.md)。

**Q：我的卡是 Pascal（P106 / P4 / P40 / GTX 10 系），装完报
`no kernel image is available` 或 `CUDA error: operation not supported`？**
A：PyTorch 版本不对。cu124 之后的 PyTorch 不再编译 sm_61 内核，
必须用 **cu121**：

```bash
pip install torch==2.5.1+cu121 torchaudio==2.5.1+cu121 \
  --index-url https://download.pytorch.org/whl/cu121
```

Windows 整合包是双击 `升级到老显卡版torch.bat`。Linux 安装脚本默认就装 cu121。

**Q：显存只有 2GB，长文本会不会 OOM？**
A：不会。服务端会自动把长文本切成多段合成再拼接，显存峰值只与单段长度有关。
2GB 显存把 `chunk.max_chars` 设成 60 即可（峰值约 1.7GB）。

**Q：没有显卡能跑吗？**
A：能。`rvc.device: "cpu"`，或者让 `rvc.min_vram_mb` 高于你的显存。
速度大约是实时的 3~5 倍耗时。

**Q：显存不够时会直接失败吗？**
A：不会。检测到显存不足会自动改用 CPU；运行时 OOM 也会自动降级 CPU 重试
（`rvc.allow_cpu_fallback`）。只是慢一些，音频照样给你。

---

## 语音合成相关

**Q：`No audio was received. Please verify that your parameters are correct.`**
A：这是微软在线语音（Edge TTS）的瞬时故障。服务端会自动重试 3 次，
仍然失败且 `tts.source: "auto"` 时会自动改用本地离线语音，请求不会失败。

**Q：`Cannot connect to host speech.platform.bing.com` / `Connection timeout`**
A：同样是网络问题，已归类为可重试错误。如果这台机器长期连不上微软，
建议直接把 `tts.source` 设成 `sapi`（Windows）或 `espeak`（Linux）走全离线。

**Q：怎么完全离线使用？**
A：

* Windows：`tts.source: "sapi"`（用系统自带语音，无需额外安装）
* Linux：`sudo apt install -y espeak-ng`，然后 `tts.source: "espeak"`

离线语音音色比较机械，但经过 RVC 转换之后可用。

**Q：长文本听起来在分段处有停顿？**
A：这是分段拼接不可避免的接缝。想减少分段就调大 `chunk.max_chars`
（需要更多显存），或者不要一次合成太长的文本（聊天回复本身不会很长）。

**Q：想换音色怎么办？**
A：把 `.pth`（和可选的 `.index`）放进 `models/`，改 `config.yaml`：

```yaml
rvc:
  model: "MyVoice.pth"
  index: "MyVoice.index"
```

**Q：`pitch` 要怎么调？**
A：它是 RVC 的变调半音数。0 = 不变调；正数声音更接近女声/更年轻，
负数更低沉。男声模型转女声一般用 +5 ~ +12，反之 -5 ~ -12。

**Q：`f0_method` 怎么选？**
A：`rmvpe` 音质最好（也是默认值）；机器慢或者显存很小时可以换 `pm` 或 `dio`，
速度快但音质下降。`harvest` 慢、`crepe` 更慢。

---

## 部署相关

**Q：启动后卡好几分钟没反应？**
A：在联网下载 `hubert_base.pt`。把这两个权重放到服务端根目录即可：

```text
hubert_base.pt    约 180 MB
rmvpe.pt          约 173 MB
```

服务端发现本地已有就会设 `HF_HUB_OFFLINE=1`，跳过联网检查。

**Q：报 `ModuleNotFoundError: No module named 'pkg_resources'`**
A：setuptools 81+ 删除了 `pkg_resources`，而 librosa 还在用它。

```bash
pip install "setuptools<81"
```

安装脚本已自动处理。

**Q：报 `'tuple' object has no attribute 'dtype'`**
A：找不到 `ffmpeg`。`apt install -y ffmpeg`，或者把 ffmpeg 可执行文件放到
`<服务目录>/bin/`（服务启动时会自动把它加进 PATH）。

**Q：`No module named 'fairseq'` / fairseq 在编译时炸了**
A：Linux 上 `tts-with-rvc` 依赖 `fairseq-fixed`，而 PyPI 上只有 **cp312** 的
manylinux wheel。所以必须用 **Python 3.12**，用 3.10/3.11 会退化成源码编译
fairseq，几乎必然失败。安装脚本会用独立运行时装 3.12。

**Q：服务开机自启怎么配？**
A：Linux 安装脚本已自动注册 systemd 服务：

```bash
systemctl enable --now tts-server
```

Windows 整合包用 `后台启动.bat`，或把它加进「任务计划程序」。

---

## 使用相关

**Q：网页控制台要密码吗？**
A：默认不要。填了 `webui.password` 就会启用登录页，
账号是 `webui.username`（默认 admin），登录状态存在浏览器 Cookie 里，
默认 12 小时有效（`session_hours`）。

**Q：控制台密码和 API Key 是一回事吗？**
A：不是。API Key 保护的是**程序调用**（`/api/tts`）；
控制台密码保护的是**人在浏览器里看的状态页面**。
两者互相独立，可以只开其中一个。

**Q：生成的音频文件会一直堆在磁盘上吗？**
A：不会。`storage.expire_minutes`（默认 10 分钟）到期自动删除，
后台清理间隔由 `cleanup_interval_minutes` 控制；控制台上也能手动清理。

**Q：相同文本会重复推理吗？**
A：不会。`storage.cache_by_text` 打开时（默认），
10 分钟内同样的文本直接复用已有音频，秒回。

**Q：能同时处理多个请求吗？**
A：可以排队，但 RVC 推理由底层库串行化，`max_concurrent` 建议保持 1。
并发设置太大反而容易出问题。

**Q：AstrBot 一直发文字不发语音？**
A：

1. 在 AstrBot 所在机器上执行 `curl http://服务端IP:端口/api/health`，确认能通
2. 检查插件的 `api_key` 与服务端 `security.api_key` 是否一致
3. 看 AstrBot 日志里插件的报错
4. 文本可能超过了插件的 `max_text_length`，被有意降级成文字

**Q：能把服务暴露到公网吗？**
A：可以，但要注意：

* API Key 与网页密码都是明文比较、HTTP 明文传输，**公网必须套 HTTPS**
* 建议用 Nginx / Caddy 反向代理，加上限流和更严格的认证
* Ollama 之类的其它服务不要跟着一起暴露

---

## 版权与合规

**Q：可以用别人的声音模型吗？**
A：请务必确认授权。克隆他人声音用于冒充、诈骗等用途在多数国家和地区都是违法的。
本项目只提供技术能力，使用后果由使用者自负。

**Q：Edge TTS 是免费的吗？**
A：本项目调用的是微软 Edge 浏览器的公开朗读接口（`edge-tts` 库），
不需要 API Key。但它不是官方承诺的公开 API，可能随时变化或限流，
所以本项目才做了重试与离线兜底。
