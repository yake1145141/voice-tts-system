# Google Colab 版语音处理端

在 Colab 上跑本项目自带的服务端（Edge TTS + RVC），再用 Cloudflare 临时隧道暴露到公网，
AstrBot 插件把 `tts_server.url` 指过去即可用语音回复。

**适合场景**：本机/服务器显卡跑不动（例如显存只有 1GB、或老架构不被新版 PyTorch 支持）时，
借用 Colab 的免费 GPU（通常 T4 16GB，RVC 完全够用）。

## 文件

```text
deploy/colab/
├── tts-server-colab.ipynb      ← 生成出来的 notebook（直接上传到 Colab 用）
├── build_colab_notebook.py     ← 生成器：把服务端代码内嵌进 notebook
└── cells/                      ← 各单元格源码（改这里，然后重新生成 notebook）
    ├── 01_intro.md             说明
    ├── 02_params.py            参数（API Key / 模型来源 / f0 / 音调…）
    ├── 03_install.py           安装依赖（含 praat-parselmouth 失败的兜底）
    ├── 04_write_code.py        写入内嵌的服务端代码
    ├── 05_models.py            准备 RVC 模型 + hubert/rmvpe 权重
    ├── 06_config.py            生成 config.yaml
    ├── 07_start.py             启动服务并等待就绪
    ├── 08_tunnel.py            建隧道并打印 AstrBot 配置片段
    ├── 09_keepalive.py         保活 + 实时状态
    ├── 10_logs.py              看日志
    ├── 11_test.py              试听
    └── 12_stop.py              停止
```

服务端代码改动后重新生成 notebook：

```bash
python deploy/colab/build_colab_notebook.py
```

## 在 Colab 上怎么用

1. 打开 <https://colab.research.google.com/> → `上传` → 选择 `tts-server-colab.ipynb`
2. 菜单：**代码执行程序 → 更改运行时类型 → 硬件加速器选 GPU（T4 即可）**
3. 从上往下运行单元格：
   - 第 2 个单元格填 `API_KEY`
   - 选模型来源：推荐把 `.pth`/`.index` 放到 Google Drive 的 `MyDrive/ColabVoice/`
   - 跑完第 8 个单元格会打印 **公网地址 + 插件配置片段**，复制到 AstrBot 插件里
   - 第 9 个单元格保持运行（既保活又能看实时状态）

## 模型怎么给（三选一）

| 方式 | 做法 | 适合 |
| --- | --- | --- |
| `MODEL_SOURCE="drive"`（推荐） | 把 `.pth`（和 `.index`）上传到 Drive 的 `MyDrive/ColabVoice/` | 长期使用，换会话不用重传 |
| `MODEL_SOURCE="upload"` | 运行时用文件选择框上传 | 临时试一下 |
| `MODEL_SOURCE="url"` | 填 `.pth` 直链 | 模型已在某个可直链下载的地方 |
| `MODEL_SOURCE="none"` | 不加载 RVC，只跑 Edge TTS | 先验证链路是否通 |

`hubert_base.pt` / `rmvpe.pt` 会从 HuggingFace 自动下载（约 354MB，Colab 网速很快）。

## 必须知道的限制

1. **每次运行地址都会变**：`https://xxxx.trycloudflare.com` 是临时隧道，重启后要更新 AstrBot 插件里的 `url`。
2. **会话有时限**：Colab 免费版最长约 12 小时，且长时间无交互会被回收；关掉浏览器也会断。
3. **公网暴露**：务必保留 `API_KEY`（notebook 默认已设置），不要只依赖“地址没人知道”。
4. **首次请求较慢**：要下载权重 + 加载模型（约 30~60 秒）；之后每次合成在 T4 上通常 1~3 秒。
5. **音频保留 10 分钟**：服务端会自动清理（`EXPIRE_MINUTES`）。

## 已验证内容

- notebook 由生成器产出，**12 个单元格语法检查全部通过**
- 内嵌的服务端代码解包后与仓库里的 `tts-server/*.py` **逐文件大小一致**
- 用 notebook 生成的 `config.yaml` 跑服务端 `--check-config` **校验通过**
- 用解出的代码真起服务：`/api/health` → `ready=True`，`/api/tts/file` → **HTTP 200（237KB wav，1.16s）**
- 依赖下载源可达：cloudflared `HTTP 200`、HuggingFace 权重 `HTTP 200`
