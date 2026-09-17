# 打包成 Linux amd64「免安装」程序

目标：把语音处理端做成一个**拿到 Linux x86_64 机器上解压就能跑**的程序，
目标机不需要装 Python、pip、ffmpeg，也不需要 root。

---

## 一、先说结论（方案对比）

| 方案 | 产物 | 目标机要求 | 体积 | 推荐度 |
| --- | --- | --- | --- | --- |
| **A. 免安装便携包**（`build_linux_bundle.sh`） | 目录 + `tar.gz`，内含独立 Python 运行时 | glibc ≥ 2.28，无需装任何东西 | 解压后 ~1.5~2GB（CPU）/ ~5GB（CUDA） | ⭐⭐⭐⭐⭐ 最稳 |
| **B. PyInstaller 可执行文件**（`build_linux_executable.sh`） | `dist/tts-server/tts-server` 可执行文件 | 同上 | ~2~4GB | ⭐⭐ 按需 |

两者都**必须在 Linux x86_64 上构建**（PyInstaller 等工具无法从 Windows 交叉编译出 Linux 可执行文件），
Windows 用户请用 Docker（本目录已提供 `Dockerfile.build` 和一键脚本）。

> **为什么推荐 A？** 这套依赖树（torch + fairseq + numba + librosa + faiss…）大量使用运行时
> 动态导入，PyInstaller 需要把它们逐个显式收集，容易漏；便携包直接带着真实 site-packages，
> 行为和你在开发机上跑 `python main.py` 完全一致。真·单文件（`--onefile`）在这里也不可取：
> 每次启动都要把几 GB 解压到 `/tmp`，启动慢且占双份磁盘，所以方案 B 用的是单目录模式。

---

## 二、Windows / macOS 用户：用 Docker 构建（推荐流程）

前置：Docker Desktop 能运行 `linux/amd64` 容器，磁盘留 10GB 以上，首次构建要下载 2~3GB。

```powershell
# 1) 可选：先把模型和权重准备好，避免目标机联网下载 350MB
#    models/   放 RVC 模型：YourVoice.pth（+ .index）
#    assets/   放 hubert_base.pt 与 rmvpe.pt（可从你已有的 RVC 目录拷来）

# 2) 一行命令构建 CPU 版便携包
powershell -ExecutionPolicy Bypass -File deploy\build_with_docker.ps1 `
  -Model models\YourVoice.pth -Index models\YourVoice.index -Assets assets

# CUDA 12.8 版（目标机需有 NVIDIA 驱动）
powershell -ExecutionPolicy Bypass -File deploy\build_with_docker.ps1 -Cuda -CudaVersion 12.8

# 改用 PyInstaller 可执行文件方案
powershell -ExecutionPolicy Bypass -File deploy\build_with_docker.ps1 -PyInstaller
```

产物在 `dist\` 下：

```text
dist/tts-server-linux-amd64-cpu.tar.gz         ← 传到服务器解压即用
dist/tts-server-linux-amd64-cpu/               ← 未压缩目录
```

不想用脚本的等价手工命令：

```powershell
docker build --platform linux/amd64 -f deploy/Dockerfile.build -t tts-builder .
docker run --rm -v "$($PWD.Path)/dist:/dist" tts-builder
docker run --rm -v "$($PWD.Path)/dist:/dist" tts-builder `
  bash deploy/build_linux_bundle.sh --cuda 12.8 --out-dir /dist
```

---

## 三、Linux 用户：直接构建

```bash
# 如果提示 Permission denied，先加执行权限
chmod +x deploy/*.sh

# 依赖：curl / tar / gcc（pyworld 需要现场编译）
sudo apt install -y build-essential curl xz-utils

# 推荐：先 dry-run 看一眼会执行什么
./deploy/build_linux_bundle.sh --cpu --dry-run

# CPU 版便携包（默认）
./deploy/build_linux_bundle.sh --cpu \
  --model /path/YourVoice.pth --index /path/YourVoice.index \
  --assets /path/to/rvc-assets        # 内含 hubert_base.pt / rmvpe.pt

# CUDA 版本
./deploy/build_linux_bundle.sh --cuda 12.8 --model /path/YourVoice.pth

# PyInstaller 可执行文件版本
./deploy/build_linux_executable.sh --cpu --model /path/YourVoice.pth
```

### 可选参数

| 参数 | 说明 |
| --- | --- |
| `--cpu` / `--cuda 12.8` | 选择 torch 版本（默认 CPU；CUDA 需要目标机有对应驱动） |
| `--model` / `--index` | 内置 RVC 模型与索引（不填则运行时放到 `models/` 也可） |
| `--assets DIR` | 内置 `hubert_base.pt`、`rmvpe.pt`，避免首次运行联网下载 350MB |
| `--name` | 自定义产物名 |
| `--out-dir` | 输出目录（默认 `dist/`） |
| `--no-ffmpeg` | 不内置 ffmpeg（目标机自行安装时用） |
| `--python 3.12.14` | 指定内置 Python 版本（**必须是 3.12**，原因见下） |
| `--dry-run` | 只打印步骤，不真正执行 |

### 国内服务器加速（可选）

```bash
# 依赖包走清华镜像
export PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
# torch 也走镜像（默认用官方 download.pytorch.org）
export TORCH_INDEX_URL=https://mirrors.tuna.tsinghua.edu.cn/pytorch/whl/cpu
./deploy/build_linux_bundle.sh --cpu ...
```

---

## 四、目标机上的使用（两种方案都一样）

```bash
tar -xzf tts-server-linux-amd64-cpu.tar.gz
cd tts-server-linux-amd64-cpu

vi config.yaml           # 改 rvc.model / security.api_key / storage.expire_minutes
./run.sh --check-config  # 只校验配置，不启动
./run.sh                 # 启动，默认 0.0.0.0:8080

# 另开终端做自检（包内自带测试客户端，无需装 Python）
./tools/check.sh
```

后台常驻（推荐 systemd）：

```bash
sudo mv tts-server-linux-amd64-cpu /opt/tts-server
sudo tee /etc/systemd/system/tts-server.service >/dev/null <<'EOF'
[Unit]
Description=TTS-with-RVC voice service
After=network-online.target

[Service]
WorkingDirectory=/opt/tts-server
ExecStart=/opt/tts-server/run.sh
Restart=always
RestartSec=5
Environment=OMP_NUM_THREADS=4
# Environment=CUDA_VISIBLE_DEVICES=0

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload && sudo systemctl enable --now tts-server
sudo journalctl -u tts-server -f
```

配套的 AstrBot 插件仍然按原来的方式配置，把 `tts_server.url` 指向这台机器的 `http://IP:8080` 即可。

---

## 五、必须知道的限制

1. **glibc 版本**：产物要求目标机 **glibc ≥ 2.28**（CentOS/RHEL 8+、Debian 10+、Ubuntu 18.10+）。
   这是因为 `soundfile` 等依赖只提供 `manylinux_2_28` 的 wheel；更老的系统请在对应系统上重新构建。
2. **Python 必须是 3.12**：Linux 上 `tts-with-rvc` 依赖 `fairseq-fixed`，PyPI 上它**只有 cp312 的
   manylinux wheel**；用 3.11 会退化成编译 fairseq 源码（几乎必然失败）。
3. **CUDA 版仍需要目标机的 NVIDIA 驱动**：驱动属于内核态组件，无法打包进产物；CUDA/cuDNN 运行库已随 wheel 打进包里。
4. **体积**：CPU 版解压后约 1.5~2GB（其中 ffmpeg 静态二进制 ~78MB），CUDA 版约 5GB；
   压缩包分别约 600MB / 3GB，传输请留足时间。
5. **首次运行**：如果构建时没有 `--assets` 内置权重，第一次推理会自动下载
   `hubert_base.pt`(181MB) 与 `rmvpe.pt`(173MB) 到程序目录，需要目标机能联网。
6. **RVC 模型不随仓库分发**：构建时用 `--model` 内置，或运行时放进 `models/` 并在 `config.yaml` 里填名字。
7. **CPU 推理能力**：CPU 版能跑，但一条 4~10 秒的语音大约需要 5~8 秒生成（实时倍率 0.8~1.2x），
   长回复建议把插件里的 `voice.max_text_length` 调小；要更快请用 CUDA 版。

---

## 六、构建脚本都做了什么（便于排查）

`build_linux_bundle.sh` 的 9 个步骤：

1. 准备目录；
2. 下载 python-build-standalone（官方独立 Python 3.12，自带 pip 与标准库）；
3. 安装 torch（CPU 版走 `download.pytorch.org/whl/cpu`，CUDA 版走 `cuXXX` 源）+ `tts-server/requirements.txt`；
4. 拷贝服务端代码（`main.py / config.py / engine.py / storage.py`）；
5. 下载并内置静态 ffmpeg；
6. 处理 `hubert_base.pt` / `rmvpe.pt` 与 RVC 模型；
7. 生成 `run.sh`、中文使用说明、`VERSION.txt`，并把 `config.yaml` 的输出目录/模型路径写对；
8. 自检（导入 torch/fastapi/tts_with_rvc，再跑一次 `--check-config`）；
9. 打 `tar.gz`。

---

## 七、实测记录（Debian 11 / 28 核 CPU 服务器）

一次真实的构建 + 运行记录，可作为时间与体积的参考：

| 项目 | 结果 |
| --- | --- |
| 构建机 | Debian 11 (bullseye)、glibc 2.31、28 核、19G 内存、无 GPU |
| 构建耗时 | 约 6 分钟（含 354MB 权重下载、torch 2.14.0+cpu 安装、pyworld 现场编译） |
| 产物 | 解压 **2.5GB** / `tar.gz` **1.1GB**（含 ffmpeg 77MB、hubert+rmvpe 354MB、RVC 模型 84MB） |
| 服务启动 | 模型加载 + 预热约 **9 秒**，之后常驻内存约 2.5GB |
| 单条合成（2.8s 音频） | 4.5s（RTF 1.60x） |
| 单条合成（7.0s 音频） | 6.5s（RTF 0.63x，长文本摊薄了固定开销） |
| 外网调用（含网络传输） | 7.18s 音频 → 7.96s（RTF 1.11x） |

### CPU 线程数怎么设

同一台 28 核机器上，用同一段文本实测三种线程设置（`OMP_NUM_THREADS`）：

| 线程数 | 音频 7.0s 的单条耗时 | 实时倍率 | 吞吐 |
| --- | --- | --- | --- |
| 4 | 8.86s | 0.86x | 0.17 条/秒 |
| **8** | **6.55s** | **0.65x** | **0.22 条/秒** |
| 16 | 6.53s | 0.63x | 0.23 条/秒 |

结论：**8 线程基本吃饱，再往上几乎没收益**（RVC 推理不是线性可扩展的）。
`run.sh` 已经默认取 `min(CPU 核数, 8)`，即 28 核机器上自动用 8 线程，
留出 20 个核给其它服务。想压满可自行 `OMP_NUM_THREADS=16 ./run.sh`，但收益很小。

### 这次真实构建踩到的坑（脚本已修）

1. **pyworld 编译失败**：python-build-standalone 的 `sysconfig` 默认 CC/CXX 是 `clang`/`clang++`，
   而多数服务器只装了 gcc。脚本现在会自动回退到 `gcc`/`g++`。
2. **PyPI 在国内服务器极慢**（实测 0.06 MB/s）：用 `PIP_INDEX_URL=<清华/阿里镜像>` 提速；
   而 `download.pytorch.org` 实测有 14.8 MB/s，无需换源。
3. **便携包里的测试客户端找不到配置**：客户端原来只找 `tts-server/config.yaml`，
   包内布局是 `./config.yaml`，已支持两种布局自动识别。
