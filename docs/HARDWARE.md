# 硬件与显存要求

## 一、一句话结论

**显卡显存最低 2 GB，NVIDIA 计算能力 ≥ 6.0 即可。**
P106-100、P106-090、P4、P40、GTX 10 系、RTX 全系都能跑。

---

## 二、为什么"2 GB 就够"

RVC 推理的显存占用符合这个模型：

```text
峰值显存 ≈ 550 MB（模型常驻 + CUDA 上下文）
         + 约 15 MB × 单次处理的中文字符数
```

关键在于**单次**。服务端会把长文本按 `chunk.max_chars` 自动切成多段，
逐段合成后用 ffmpeg 无缝拼接，所以：

```text
峰值显存 = f(chunk.max_chars)     与文本总长度无关
```

也就是说，一段 1000 字的文本和一段 60 字的文本，在 `max_chars=60` 时显存峰值是一样的。

### 实测数据（P106-100 6GB / Ubuntu 22.04 / rmvpe / fp16）

| 文本长度 | 未分段峰值 | `max_chars=80` 分段后 |
| --- | --- | --- |
| 10 字 | 815 MB | 815 MB |
| 20 字 | 1037 MB | 1037 MB |
| 40 字 | 1311 MB | 1311 MB |
| 60 字 | 1699 MB | 1699 MB |
| 80 字 | 1941 MB | 1941 MB |
| 100 字 | 2289 MB | **1941 MB** |
| 192 字 | **3493 MB** | **1941 MB** |

### 按显存选 `chunk.max_chars`

| 显存 | `chunk.max_chars` | `rvc.min_vram_mb` | 说明 |
| --- | --- | --- | --- |
| 2 GB | **60** | 1800 | 峰值约 1.7 GB |
| 3 GB | 80 | 2800 | |
| 4 GB | 120 | 3500 | |
| 6 GB | 80 ~ 200 | 5000 | P106-100 / GTX 1660 等 |
| 8 GB+ | 200 | 7000 | 基本不触发分段 |

显存不够也不会失败：不满足 `min_vram_mb` 会直接走 CPU；
运行时 OOM 会自动降级 CPU 重试（`allow_cpu_fallback: true`）。

---

## 三、显卡对照表

**最重要的一条：Pascal 及更老的卡必须用 cu121 版 PyTorch。**

| 架构 | 计算能力 | 代表显卡 | cu121 (torch 2.5.1) | cu124 / cu128 |
| --- | --- | --- | --- | --- |
| Pascal | sm_60 | Tesla P100 | ✅ | ❌ |
| Pascal | **sm_61** | **P106-100 / P4 / P40 / GTX 1050~1080 / Titan Xp** | ✅（复用 sm_60 内核） | ❌ |
| Pascal | sm_62 | Tesla P40（部分批次） | ✅ | ❌ |
| Volta | sm_70 | Tesla V100 / Titan V | ✅ | ❌ |
| Turing | sm_75 | GTX 1650~1660 / RTX 2060 / T4 | ✅ | ✅ |
| Ampere | sm_80 / sm_86 | RTX 30 系 / A100 | ✅ | ✅ |
| Ada | sm_89 | RTX 40 系 | ✅ | ✅ |
| Blackwell | sm_120 | RTX 50 系 | ⚠️ 需要更新的 torch | ✅ |

> **为什么 sm_61 能在只编译了 sm_60 的 PyTorch 上跑？**
> CUDA 的二进制兼容规则：为计算能力 X.y 编译的 cubin 可以在 X.z（z ≥ y）上执行。
> sm_61 显卡可以复用 sm_60 内核，但**不能**复用 sm_75 的内核（跨大版本了）。
> 所以 cu124/cu128 编译列表里只有 sm_75+ 时，Pascal 卡无内核可用，会报
> `no kernel image is available`。

服务端会自动判断这一点，在 `/api/health` 与网页控制台里显示：

```text
设备        cuda:0 · fp16
架构        sm_61（复用 sm_60 内核）
已编译架构   sm_50 sm_60 sm_70 sm_75 sm_80 sm_86 sm_90
```

---

## 四、驱动要求

| 项目 | 要求 |
| --- | --- |
| NVIDIA 驱动 | **≥ 525**（CUDA 12.1 需要），建议 535+ |
| CUDA Toolkit | **不需要单独安装**，PyTorch wheel 自带运行库 |
| 验证方式 | `nvidia-smi` 能正常输出 |

```bash
# Ubuntu 一键装驱动
sudo ubuntu-drivers autoinstall && sudo reboot
nvidia-smi
```

---

## 五、没有显卡 / 显存特别小

### 用 CPU

```yaml
rvc:
  device: "cpu"        # 或者让 min_vram_mb 高于你的显存，让它自动选 CPU
```

CPU 推理大约是实时的 **3~5 倍耗时**（一句 10 秒的语音要 30~50 秒）。
适合偶尔用、不追求速度的场景。

### 用在线 GPU

* **Google Colab**：免费 GPU，`deploy/colab/tts-server-colab.ipynb` 一键部署（会断线）
* **云服务器按量计费**：选带 T4 / P4 的实例，注意显存 ≥ 4GB 体验更好

### 只想要语音、不换音色

把 `rvc.enabled` 设成 `false`，直接输出 Edge TTS 原始音频，
显存占用几乎为零，任何机器都能跑。

---

## 六、性能参考

| 显卡 | 显存 | 短句延迟 | RTF | 备注 |
| --- | --- | --- | --- | --- |
| P106-100 | 6 GB | 2 ~ 3 s | **0.41x** | 实测，Ubuntu 22.04 |
| Tesla P4 | 8 GB | 4 ~ 6 s | 0.70 ~ 0.89x | 实测，Windows |
| CPU（4 核） | — | 15 ~ 40 s | 3 ~ 5x | 降级场景 |

> RTF（实时倍率）= 处理耗时 ÷ 音频时长，**越小越快**，小于 1 表示比实时快。

---

## 七、老显卡常见坑速查

| 报错 | 原因 | 解决 |
| --- | --- | --- |
| `no kernel image is available for execution on the device` | PyTorch 没编译这张卡的架构 | 装 `torch 2.5.1+cu121` |
| `CUDA error: operation not supported` | 同上（通常出现在第一次推理时） | 同上 |
| `CUDA out of memory` | 显存不够 | 调小 `chunk.max_chars`；或调高 `min_vram_mb` 走 CPU |
| `Found GPU ... force to fp32` | 库认为该卡不支持 fp16 | 正常提示，不影响使用（更快的是 fp32 反而更稳） |
| vGPU 切片只有 1 GB | 显存物理不够 | 调整 vGPU 档位，或用 CPU |
