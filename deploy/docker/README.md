# Docker 部署

## 前置条件

1. Docker 与 docker compose v2
2. NVIDIA 驱动（`nvidia-smi` 能跑）
3. [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)

```bash
# 验证容器能看到 GPU
docker run --rm --gpus all nvidia/cuda:12.1.1-base-ubuntu22.04 nvidia-smi
```

## 步骤

```bash
cd deploy/docker

# 1. 准备模型与配置
mkdir -p models output
cp /path/MyVoice.pth /path/MyVoice.index models/
cp ../../server/config.yaml ./config.yaml
vi config.yaml      # 改 api_key、rvc.model、rvc.index

# 2. 权重（可选，但强烈建议：不挂载时容器会联网下载，国内很慢）
#    把 hubert_base.pt / rmvpe.pt 放到 ./models，然后在 compose 里
#    加一行挂载，或直接放进容器 /app：
#      cp hubert_base.pt rmvpe.pt .
#    然后修改 docker-compose.yml 的 volumes，追加：
#      - "${TTS_ASSETS_DIR:-.}:/app/assets:ro"
#    并把这两个文件软链到 /app：
#      docker compose exec tts-server ln -sf /app/assets/hubert_base.pt /app/
#      docker compose exec tts-server ln -sf /app/assets/rmvpe.pt /app/

# 3. 启动
docker compose up -d
docker compose logs -f

# 4. 验证
curl http://127.0.0.1:8080/api/health

# 5. 停止 / 更新
docker compose down
docker compose build --no-cache && docker compose up -d
```

## 常用环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `TTS_PORT` | 8080 | 宿主机映射端口 |
| `TTS_CONFIG` | `./config.yaml` | 配置文件路径 |
| `TTS_MODEL_DIR` | `./models` | 模型目录 |
| `TTS_OUTPUT_DIR` | `./output` | 音频输出目录 |

## 注意事项

* 镜像基于 `python:3.12-slim`，PyTorch 装的是 **cu121** —— 这是 Pascal 老卡
  （P106-100 / P4 / P40 / GTX 10 系）唯一可用的版本，别随便升级。
* 音频默认 10 分钟后自动删除，`output` 目录不会无限增长。
* 容器里跑的是 root，模型目录注意宿主机的权限。
