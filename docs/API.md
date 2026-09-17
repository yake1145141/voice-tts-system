# HTTP 接口文档

服务端是一个 FastAPI 应用，启动后自带交互式文档：`http://<服务器>:<端口>/docs`。

## 鉴权

`security.api_key` 非空时，下列接口必须带密钥，三种传法任选：

```http
X-API-Key: your-secret-key
Authorization: Bearer your-secret-key
```

```text
GET /audio/xxx.wav?key=your-secret-key     # 浏览器/播放器没法自定义请求头时用
```

`webui.password` 非空时，`/`、`/api/gpu`、`/api/system`、`/api/stats`、`/api/config`、
`/api/cleanup`、`/docs` 需要先登录控制台（会话 Cookie）。
`/api/health` 与 `/api/tts*` **不受控制台密码影响**。

---

## 接口列表

| 方法 | 路径 | 鉴权 | 说明 |
| --- | --- | --- | --- |
| GET | `/` | 视配置 | 网页控制台 |
| GET | `/login` | 否 | 登录页 |
| POST | `/api/login` | 否 | 提交用户名密码，下发会话 Cookie |
| POST | `/api/logout` | 否 | 注销 |
| GET | `/api/health` | 否 | 服务、引擎、存储状态 |
| GET | `/api/gpu` | 视配置 | 显卡 + 主机状态 |
| GET | `/api/system` | 视配置 | 主机状态 |
| GET | `/api/stats` | 视配置 | 引擎统计 + 最近请求 |
| GET | `/api/config` | 视配置 | 配置概览（不含密钥） |
| POST | `/api/cleanup` | 视配置 | 立即清理过期音频 |
| POST | `/api/tts` | API Key | 合成，返回 JSON |
| POST | `/api/tts/file` | API Key | 合成，返回音频流 |
| GET | `/audio/{filename}` | 视配置 | 下载 / 播放音频 |

---

## POST /api/tts

合成语音，返回 JSON（含可直接播放的 URL）。

**请求体**

```json
{ "text": "你好，这是一条语音测试。" }
```

查询参数 `format=file` 时直接返回音频流，等价于 `/api/tts/file`。

**响应**

```json
{
  "success": true,
  "audio_url": "http://192.168.1.10:8080/audio/8fc09ec2....wav",
  "audio_path": "/audio/8fc09ec2....wav",
  "filename": "8fc09ec2....wav",
  "format": "wav",
  "size_bytes": 225644,
  "expire_minutes": 10,
  "cached": false
}
```

```bash
curl -X POST http://127.0.0.1:8080/api/tts \
  -H "X-API-Key: your-secret-key" \
  -H "Content-Type: application/json" \
  -d '{"text":"你好"}'
```

---

## POST /api/tts/file

合成语音，直接返回音频文件流（AstrBot 插件默认用这个）。

**响应头**

| 头 | 说明 |
| --- | --- |
| `Content-Type` | `audio/wav` 或 `audio/mpeg` |
| `X-Audio-Filename` | 文件名 |
| `X-Audio-Url` | 对应的下载地址 |
| `X-Audio-Cached` | `1` 表示命中了文本缓存 |
| `X-Audio-Expire-Minutes` | 多少分钟后自动删除 |

```bash
curl -X POST http://127.0.0.1:8080/api/tts/file \
  -H "X-API-Key: your-secret-key" \
  -H "Content-Type: application/json" \
  -d '{"text":"你好，这是一条语音测试。"}' \
  -o out.wav
```

---

## GET /api/health

不需要鉴权，适合做探活和监控。

```json
{
  "success": true,
  "status": "ok",
  "engine": {
    "ready": true,
    "device": "cuda:0",
    "is_half": true,
    "device_fallback": false,
    "gpu_detail": "sm_61（复用 sm_60 内核）",
    "torch_arch_list": ["sm_50", "sm_60", "sm_70", "sm_75", "sm_80", "sm_86", "sm_90"],
    "rvc_enabled": true,
    "tts_source": "auto",
    "sapi_available": false,
    "chunk_enabled": true,
    "chunk_max_chars": 80,
    "model": "/opt/tts-server/models/MyVoice.pth",
    "f0_method": "rmvpe",
    "queued": 0,
    "inflight": 0,
    "last_error": null,
    "stats": {
      "total": 12, "success": 12, "failed": 0, "timeout": 0,
      "cache_hit": 3, "last_success_at": "2026-09-17 14:10:00",
      "last_duration": 2.31
    }
  },
  "storage": {
    "files": 7, "total_mb": 1.9,
    "expire_minutes": 10, "expire_seconds": 600.0
  },
  "auth_enabled": true
}
```

`status` 为 `ok` 表示引擎就绪；`degraded` 表示初始化失败（看 `last_error`）。

---

## GET /api/gpu

显卡与主机状态，网页控制台的数据来源。

```json
{
  "success": true,
  "gpu": {
    "available": true,
    "gpus": [{
      "index": 0, "name": "NVIDIA P106-100", "driver": "580.178.04",
      "util_gpu": 100.0, "mem_used_mb": 1941.0, "mem_total_mb": 6144.0,
      "temp_c": 45.0, "power_w": 91.1, "power_limit_w": 120.0,
      "fan_pct": 27.0, "clock_sm_mhz": 1885.0, "clock_mem_mhz": 4006.0
    }],
    "processes": [{ "pid": 4498, "name": "/opt/tts-server/venv/bin/python", "used_memory_mb": 548.0 }],
    "torch": {
      "installed": true, "version": "2.5.1+cu121", "cuda_available": true,
      "cuda_version": "12.1", "capability": "sm_61",
      "arch_list": ["sm_50", "sm_60", "sm_70", "sm_75", "sm_80", "sm_86", "sm_90"]
    }
  },
  "system": {
    "cpu_count": 4,
    "load": { "1m": 0.18, "5m": 0.34, "15m": 0.23 },
    "memory": { "total_mb": 3911, "used_mb": 1983, "used_pct": 50.7 },
    "disk": { "path": "/opt/tts-server/output", "total_gb": 117.6, "used_gb": 15.8, "used_pct": 13.4 },
    "uptime_seconds": 3600
  }
}
```

没有 NVIDIA 显卡时 `gpu.available` 为 `false`，`gpu.reason` 会给出原因。

---

## 错误响应

所有错误都是统一结构：

```json
{
  "success": false,
  "error": { "code": "rvc_failed", "message": "tts-with-rvc 执行失败: ..." }
}
```

| HTTP | code | 说明 |
| --- | --- | --- |
| 400 | `empty_text` | 文本为空 |
| 400 | `text_too_long` | 超过 `storage.max_text_length` |
| 401 | `unauthorized` | API Key 错误（或控制台未登录） |
| 503 | `queue_full` | 排队超过 `queue.max_queue_size` |
| 503 | `engine_not_ready` | 引擎还没初始化好 |
| 504 | `timeout` | 处理超时 |
| 500 | `rvc_failed` | 合成失败（在线语音不可用等） |
| 500 | `empty_audio` | 生成了空音频 |
| 500 | `ffmpeg_missing` | 没装 ffmpeg |
| 410 | `audio_expired` | 音频已过期删除 |
