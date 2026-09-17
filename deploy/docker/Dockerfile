# =============================================================================
#  voice-tts-system 服务端镜像
#
#  构建（在发布包根目录执行）：
#     docker build -f deploy/docker/Dockerfile -t voice-tts-system:1.0.0 .
#
#  说明：
#   * 基础镜像用 python:3.12-slim —— 必须是 3.12，fairseq-fixed 只有 cp312 的 wheel
#   * PyTorch 固定 cu121 —— Pascal（sm_61）等老卡在 cu124+ 上没有内核
#   * torch 的 pip wheel 自带 CUDA 运行库，宿主机只需要装好 NVIDIA 驱动 +
#     nvidia-container-toolkit，不需要装 CUDA Toolkit
# =============================================================================
FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HUB_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1

# ffmpeg 用于音频读写与分段拼接；espeak-ng 是离线语音兜底
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ffmpeg espeak-ng build-essential \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# setuptools 81+ 删掉了 pkg_resources，而 librosa 还在用它
RUN python -m pip install --no-cache-dir --upgrade pip wheel \
 && python -m pip install --no-cache-dir "setuptools<81"

# 老显卡必须用 cu121
ARG TORCH_VERSION=2.5.1
ARG CUDA_TAG=cu121
RUN python -m pip install --no-cache-dir \
        "torch==${TORCH_VERSION}+${CUDA_TAG}" "torchaudio==${TORCH_VERSION}+${CUDA_TAG}" \
        --index-url "https://download.pytorch.org/whl/${CUDA_TAG}"

COPY server/requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/requirements.txt

COPY server/ /app/

# 模型与权重目录挂出来，镜像里不放任何 .pth
VOLUME ["/app/models", "/app/output"]

EXPOSE 8080

# 健康检查对外开放的探活接口
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4).status==200 else 1)"

CMD ["python", "/app/main.py", "--config", "/app/config.yaml"]
