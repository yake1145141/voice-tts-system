# =============================================================================
#  构建 Windows「整合包」——复制到目标机后双击即用，无需安装 Python
#
#  用法：
#     powershell -ExecutionPolicy Bypass -File deploy\windows\build_windows_bundle.ps1
#     powershell -ExecutionPolicy Bypass -File deploy\windows\build_windows_bundle.ps1 -Variant cpu
#     powershell -ExecutionPolicy Bypass -File deploy\windows\build_windows_bundle.ps1 -Zip
#
#  产出：dist\windows\<名称>\
#          ├ 启动语音服务.bat / 后台启动.bat / 停止服务.bat / 测试合成.bat / 编辑配置.bat
#          ├ python\            独立 Python 运行时 + 全部依赖（torch、tts-with-rvc…）
#          ├ app\               服务端代码 + 测试客户端
#          ├ models\            RVC 模型
#          ├ hubert_base.pt / rmvpe.pt
#          ├ bin\ffmpeg.exe
#          ├ config.yaml        唯一配置文件（可直接编辑）
#          └ astrbot_plugin_voice_reply\   直接拷进 AstrBot 的插件目录
# =============================================================================
param(
    [ValidateSet("cuda", "cpu")]
    [string]$Variant = "cuda",
    [string]$TorchVersion = "",        # 例：2.11.0（留空=该变体的最新版）
    [string]$CudaTag = "",             # 例：cu121 / cu124 / cu128；留空=cu128（cpu 变体忽略）
    [string]$PythonVersion = "3.10.21",
    [string]$PythonTag = "20260901",
    [string]$OutDir = "",
    [string]$Name = "",
    [string]$Model = "",
    [string]$Index = "",
    [string]$Assets = "",
    [string]$ApiKey = "win-tts-key",
    [int]$Port = 8080,
    [switch]$SkipFfmpeg,
    [switch]$SkipAssets,
    [switch]$Zip,
    [switch]$LaunchersOnly
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
if (-not $OutDir) { $OutDir = Join-Path $root "dist\windows" }
if (-not $Name) { $Name = "tts-server-win64-$Variant" }
$bundle = Join-Path $OutDir $Name
$cache = Join-Path $root ".build-cache"
$py = Join-Path $bundle "python\python.exe"

function Say($msg) { Write-Host "[build] $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "[build] $msg" -ForegroundColor Yellow }
function Die($msg) { Write-Host "[build] $msg" -ForegroundColor Red; exit 1 }

# 源码目录：开发仓库里叫 tts-server\，发布包里叫 server\，两个都认
$ServerDir = Join-Path $root "tts-server"
if (-not (Test-Path $ServerDir)) { $ServerDir = Join-Path $root "server" }
if (-not (Test-Path (Join-Path $ServerDir "main.py"))) {
    Die "找不到服务端源码（tts-server\ 或 server\），请在仓库/发布包根目录执行本脚本"
}

if (-not $Model) {
    Die "请用 -Model 指定 RVC 模型 .pth，例如：`n  -File deploy\windows\build_windows_bundle.ps1 -Model D:\models\MyVoice.pth -Index D:\models\MyVoice.index"
}
if (-not (Test-Path $Model)) { Die "模型文件不存在：$Model" }
if ($Index -and -not (Test-Path $Index)) { Die "索引文件不存在：$Index" }
if ($Assets -and -not (Test-Path $Assets)) { Die "assets 目录不存在：$Assets" }

New-Item -ItemType Directory -Force -Path $OutDir, $cache | Out-Null

Say "变体        : $Variant"
Say "Python      : $PythonVersion"
Say "产物目录    : $bundle"
Say "源码目录    : $ServerDir"
Say "模型        : $(Split-Path $Model -Leaf)"

if ((Test-Path $bundle) -and -not $LaunchersOnly) {
    python -c "import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)" $bundle
}
if (-not $LaunchersOnly) {
    foreach ($d in @("python", "app", "models", "bin", "output", "logs", "tools")) {
        New-Item -ItemType Directory -Force -Path (Join-Path $bundle $d) | Out-Null
    }
}

if ($LaunchersOnly) {
    Say "只重新生成启动脚本（跳过依赖安装）"
} else {
Say "1/7 准备独立 Python 运行时"
$pyAsset = "cpython-$PythonVersion+$PythonTag-x86_64-pc-windows-msvc-install_only.tar.gz"
$pyUrl = "https://github.com/astral-sh/python-build-standalone/releases/download/$PythonTag/$pyAsset"
$pyTar = Join-Path $cache $pyAsset
if (-not (Test-Path $pyTar)) {
    Say "    下载 $pyAsset"
    curl.exe -fL --retry 3 -o $pyTar $pyUrl
    if ($LASTEXITCODE -ne 0) { Die "下载 Python 运行时失败" }
}
tar -xzf $pyTar -C $bundle
if (-not (Test-Path $py)) { Die "解压后找不到 $py" }
& $py --version

Say "2/7 安装依赖（torch 体积大，首次会慢一些）"
& $py -m pip install --upgrade pip --quiet
if ($Variant -eq "cuda") {
    $tag = if ($CudaTag) { $CudaTag } else { "cu128" }
    $index = "https://download.pytorch.org/whl/$tag"
    # 老显卡（Pascal/图灵等）需要 cu121/cu118 这种仍编译了旧架构的版本
    if ($TorchVersion) {
        # torch 轮子动辄 2GB+，用 curl 断点续传下载（网络抖动时可重跑续传），再本地安装
        $cp = "cp" + ($PythonVersion -replace '^3\.(\d+).*', '3$1')
        $wheelDir = Join-Path $cache "torch-wheels"
        New-Item -ItemType Directory -Force -Path $wheelDir | Out-Null
        $spec = "$TorchVersion+$tag"
        $wheels = @()
        foreach ($pkg in @("torch", "torchaudio")) {
            $file = "$pkg-$TorchVersion%2B$tag-$cp-$cp-win_amd64.whl".Replace("%2B", "+")
            $dest = Join-Path $wheelDir $file
            if ((-not (Test-Path $dest)) -or ((Get-Item $dest).Length -lt 50MB)) {
                $url = "https://download.pytorch.org/whl/$tag/$($pkg)-$TorchVersion%2B$tag-$cp-$cp-win_amd64.whl"
                Say "    下载 $pkg $spec（$cp，断点续传）"
                curl.exe -fL -C - --retry 8 --retry-delay 3 --retry-all-errors -o $dest $url
                if ($LASTEXITCODE -ne 0) { Die "下载 $pkg $spec 失败（可重新运行本脚本续传）" }
            } else {
                Say "    使用已下载的 $file"
            }
            $wheels += $dest
        }
        Say "    本地安装 torch $spec"
        & $py -m pip install --no-deps @wheels
        & $py -m pip install @wheels
        if ($LASTEXITCODE -ne 0) { Die "torch 安装失败" }
    } else {
        Say "    安装 torch（$tag，最新版）"
        & $py -m pip install --timeout 120 --retries 10 --index-url $index torch torchaudio
    }
} else {
    & $py -m pip install --timeout 120 --retries 10 --index-url https://download.pytorch.org/whl/cpu torch torchaudio
}
if ($LASTEXITCODE -ne 0) { Die "torch 安装失败" }
& $py -m pip install --timeout 120 --retries 10 -r (Join-Path $ServerDir "requirements.txt")
if ($LASTEXITCODE -ne 0) { Die "服务端依赖安装失败" }

Say "3/7 拷贝服务端代码与插件"
# 遍历源码目录里的 *.py，新增模块（如 webui.py）自动带上
Get-ChildItem (Join-Path $ServerDir "*.py") | ForEach-Object {
    Copy-Item $_.FullName (Join-Path $bundle ("app\" + $_.Name)) -Force
}
$staticSrc = Join-Path $ServerDir "static"
if (Test-Path $staticSrc) {
    Copy-Item $staticSrc (Join-Path $bundle "app\static") -Recurse -Force
}
# 客户端源码在开发仓库里是 tools\、astrbot_plugin_voice_reply\，
# 发布包里是 client\tools\、client\astrbot_plugin_voice_reply\，两个都认
$toolsDir = Join-Path $root "tools"
if (-not (Test-Path $toolsDir)) { $toolsDir = Join-Path $root "client\tools" }
foreach ($f in @("tts_client.py", "voice_tts.py", "sample_texts.txt")) {
    $src = Join-Path $toolsDir $f
    if (Test-Path $src) { Copy-Item $src (Join-Path $bundle "app\$f") -Force }
    else { Warn "缺少客户端文件 $f（跳过）" }
}
$pluginSrc = Join-Path $root "astrbot_plugin_voice_reply"
if (-not (Test-Path $pluginSrc)) { $pluginSrc = Join-Path $root "client\astrbot_plugin_voice_reply" }
if (Test-Path $pluginSrc) {
    Copy-Item $pluginSrc (Join-Path $bundle "astrbot_plugin_voice_reply") -Recurse -Force
}

Say "4/7 放入 RVC 模型与推理权重"
if (-not $SkipAssets) {
    foreach ($pair in @(@($Model, "models"), @($Index, "models"))) {
        $src = $pair[0]
        if ($src -and (Test-Path $src)) {
            Copy-Item $src (Join-Path $bundle $pair[1]) -Force
        } elseif ($src) {
            Warn "找不到 $src，跳过"
        }
    }
    foreach ($asset in @("hubert_base.pt", "rmvpe.pt")) {
        $src = Join-Path $Assets $asset
        if (Test-Path $src) {
            Copy-Item $src $bundle -Force
        } else {
            Warn "找不到 $src（首次推理会自动下载，约 350MB）"
        }
    }
} else {
    Warn "已跳过模型与权重（-SkipAssets），请自行放入 models\ 与 hubert_base.pt / rmvpe.pt"
}

Say "5/7 准备 ffmpeg"
$ffmpegDst = Join-Path $bundle "bin\ffmpeg.exe"
if ($SkipFfmpeg) {
    Warn "已跳过内置 ffmpeg，请确保目标机 PATH 中有 ffmpeg"
} elseif (Test-Path $ffmpegDst) {
    Say "    已存在"
} else {
    $sysFfmpeg = (Get-Command ffmpeg -ErrorAction SilentlyContinue).Source
    if ($sysFfmpeg) {
        Copy-Item $sysFfmpeg $ffmpegDst -Force
        Say "    已复制系统 ffmpeg：$sysFfmpeg"
    } else {
        Warn "构建机没有 ffmpeg，尝试下载静态版…"
        $zipPath = Join-Path $cache "ffmpeg-win64.zip"
        if (-not (Test-Path $zipPath)) {
            curl.exe -fL --retry 3 -o $zipPath "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
        }
        if ((Test-Path $zipPath) -and ((Get-Item $zipPath).Length -gt 1MB)) {
            $tmp = Join-Path $cache "ffmpeg-extract"
            python -c "import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)" $tmp
            Expand-Archive -Path $zipPath -DestinationPath $tmp -Force
            $exe = Get-ChildItem -Recurse -Filter ffmpeg.exe $tmp | Select-Object -First 1
            if ($exe) { Copy-Item $exe.FullName $ffmpegDst -Force; Say "    已内置 ffmpeg" }
        } else {
            Warn "ffmpeg 下载失败：目标机需自行安装 ffmpeg 并加入 PATH"
        }
    }
}

    # ---- 补全 VC++ 运行库（防止目标机报 c10.dll / WinError 1114）----
    Say "5.5/7 内置 VC++ 运行库（msvcp140 / vcruntime140 系列）"
    $crtNames = @(
        "msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll",
        "vcruntime140.dll", "vcruntime140_1.dll", "concrt140.dll"
    )
    $crtSources = @(
        "$env:SystemRoot\System32",
        (Join-Path $bundle "python")
    )
    $crtTargets = @(
        (Join-Path $bundle "python"),
        (Join-Path $bundle "python\Lib\site-packages\torch\lib")
    )
    $crtCopied = 0
    foreach ($name in $crtNames) {
        $src = $null
        foreach ($dir in $crtSources) {
            if ($dir -and (Test-Path (Join-Path $dir $name))) { $src = Join-Path $dir $name; break }
        }
        if (-not $src) { Warn "    构建机上没有 $name，跳过"; continue }
        foreach ($target in $crtTargets) {
            if (Test-Path $target) { Copy-Item $src (Join-Path $target $name) -Force }
        }
        $crtCopied++
    }
    Say "    已内置 $crtCopied / $($crtNames.Count) 个运行库文件"
}   # LaunchersOnly 分支结束：以下为第 6 步

Say "6/7 生成配置与一键脚本"

$configYaml = @"
# 语音处理端配置（改完保存，重启服务生效）
server:
  host: "0.0.0.0"          # 0.0.0.0 = 局域网可访问；只本机用可改 127.0.0.1
  port: $Port
  log_level: "INFO"

security:
  api_key: "$ApiKey"       # AstrBot 插件里填同一个值；留空则不校验

rvc:
  enabled: true
  model: "$(Split-Path $Model -Leaf)"
  model_dir: "./models"    # 把 .pth 放这里；也可以写绝对路径
  index: "$(Split-Path $Index -Leaf)"
  f0_method: "rmvpe"       # rmvpe 音质最好；想更快可改 pm / dio
  device: "auto"           # auto = 有 GPU 用 GPU，否则 CPU
  min_vram_mb: 2500        # auto 模式下显存低于该值就直接用 CPU（1~2GB 显存请保持）
  allow_cpu_fallback: true # 运行中显存不足时自动降级到 CPU 重试，不会让语音失败
  is_half: true
  preload: true

tts:
  # edgetts = 微软在线语音（最自然，需联网）；sapi = Windows 本地离线语音；
  # auto    = 先用在线，失败自动改用本地（推荐，几乎不会再失败）
  source: "auto"
  speaker: "zh-CN-YunxiNeural"
  pitch: 5
  retries: 3               # 在线语音偶发失败的重试次数
  sapi_voice: ""           # 本地语音名，留空 = 自动选中文语音
  sapi_rate: 0             # 本地语音语速 -10 ~ 10

storage:
  output_dir: "./output"
  expire_minutes: 10       # 生成的音频 10 分钟后自动删除
  cleanup_interval_minutes: 2
  cache_by_text: true
  max_text_length: 2000

queue:
  max_concurrent: 1        # RVC 推理本身串行，保持 1 最稳
  max_queue_size: 16
  timeout: 180

audio:
  output_format: "wav"
"@
if (-not $LaunchersOnly) {
    [IO.File]::WriteAllText((Join-Path $bundle "config.yaml"), $configYaml, (New-Object Text.UTF8Encoding($false)))
}

$header = @"
@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PATH=%~dp0bin;%~dp0python;%PATH%"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "TTS_SERVER_CONFIG=%~dp0config.yaml"
"@

$batStart = @"
echo ============================================
echo  Voice service starting (close this window to stop)
echo  Health check: http://127.0.0.1:$Port/api/health
echo ============================================
echo.
"%~dp0python\python.exe" -s "%~dp0app\main.py" --config "%~dp0config.yaml"
echo.
echo Service exited. Press any key to close.
pause >nul
"@

$batBackground = @"
if not exist "%~dp0logs" mkdir "%~dp0logs"
start "tts-server" /min cmd /c ""%~dp0python\python.exe" -s "%~dp0app\main.py" --config "%~dp0config.yaml" >> "%~dp0logs\server.log" 2>&1"
echo Started in background (minimized window). Log file: logs\server.log
ping -n 4 127.0.0.1 >nul
"@

$batStop = @"
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { `$_.CommandLine -like '*app\main.py*' } | ForEach-Object { Write-Host ('stopping pid ' + `$_.ProcessId); Stop-Process -Id `$_.ProcessId -Force }"
echo Stopped (no output above means it was not running).
ping -n 3 127.0.0.1 >nul
"@

$batCheck = @"
echo ===== 1) config check =====
"%~dp0python\python.exe" -s "%~dp0app\main.py" --config "%~dp0config.yaml" --check-config
echo.
echo ===== 2) torch / GPU =====
"%~dp0python\python.exe" -c "import torch;print('torch',torch.__version__);print('cuda_available',torch.cuda.is_available());print('arch_list',torch.cuda.get_arch_list() if torch.cuda.is_available() else '-');print('device',torch.cuda.get_device_name(0) if torch.cuda.is_available() else '-');print('capability',torch.cuda.get_device_capability(0) if torch.cuda.is_available() else '-')"
echo.
echo ===== done, press any key =====
pause
"@

$batTest = @"
set "TEXT=%~1"
if "%TEXT%"=="" set /p TEXT=Text to synthesize (press Enter for default): 
if "%TEXT%"=="" set "TEXT=Hello, this is a TTS test from the Windows bundle."
"%~dp0python\python.exe" -s "%~dp0app\tts_client.py" --url http://127.0.0.1:$Port say "%TEXT%" --play --output-dir "%~dp0output"
echo.
pause
"@

$batConfig = @"
start "" notepad "%~dp0config.yaml"
"@

$batLog = @"
if exist "%~dp0logs\server.log" (
  start "" notepad "%~dp0logs\server.log"
) else (
  echo No log yet. Please run the start script first.
  pause >nul
)
"@

$batOpen = @"
start "" "%~dp0output"
"@

$batDiag = @"
set "OUT=%~dp0diagnostics.txt"
echo ===== diagnostics ===== > "%OUT%"
echo --- Windows --- >> "%OUT%"
ver >> "%OUT%" 2>&1
powershell -NoProfile -Command "(Get-CimInstance Win32_OperatingSystem).Caption; (Get-CimInstance Win32_OperatingSystem).Version" >> "%OUT%" 2>&1
echo --- CPU --- >> "%OUT%"
powershell -NoProfile -Command "Get-CimInstance Win32_Processor | Select-Object -Property Name,NumberOfCores,NumberOfLogicalProcessors | Format-List" >> "%OUT%" 2>&1
echo --- GPU --- >> "%OUT%"
powershell -NoProfile -Command "Get-CimInstance Win32_VideoController | Select-Object -Property Name,DriverVersion | Format-List" >> "%OUT%" 2>&1
nvidia-smi >> "%OUT%" 2>&1
echo --- VC++ runtime --- >> "%OUT%"
if exist "%SystemRoot%\System32\msvcp140.dll" (echo system msvcp140.dll OK >> "%OUT%") else (echo system msvcp140.dll MISSING >> "%OUT%")
if exist "%SystemRoot%\System32\vcruntime140.dll" (echo system vcruntime140.dll OK >> "%OUT%") else (echo system vcruntime140.dll MISSING >> "%OUT%")
if exist "%~dp0python\msvcp140.dll" (echo bundled msvcp140.dll OK >> "%OUT%") else (echo bundled msvcp140.dll MISSING >> "%OUT%")
echo --- Python / torch --- >> "%OUT%"
"%~dp0python\python.exe" -V >> "%OUT%" 2>&1
"%~dp0python\python.exe" -c "import torch;print('torch',torch.__version__);print('arch_list',torch.cuda.get_arch_list());print('cuda_available',torch.cuda.is_available());print('device',torch.cuda.get_device_name(0));print('capability',torch.cuda.get_device_capability(0))" >> "%OUT%" 2>&1
echo --- config --- >> "%OUT%"
"%~dp0python\python.exe" -s "%~dp0app\main.py" --config "%~dp0config.yaml" --check-config >> "%OUT%" 2>&1
echo.
echo Diagnostics written to: diagnostics.txt
start "" notepad "%OUT%"
"@

$files = @{
    "启动语音服务.bat" = $batStart
    "后台启动.bat" = $batBackground
    "停止服务.bat" = $batStop
    "首次运行检查.bat" = $batCheck
    "诊断信息.bat" = $batDiag
    "测试合成.bat" = $batTest
    "编辑配置.bat" = $batConfig
    "查看日志.bat" = $batLog
    "打开输出目录.bat" = $batOpen
}
foreach ($name in $files.Keys) {
    # 注意：header 与正文之间必须显式加换行，否则第一条命令会被拼到 set 行后面
    $content = ($header + "`n" + $files[$name]) -replace "`r`n", "`n" -replace "`n", "`r`n"
    [IO.File]::WriteAllText((Join-Path $bundle $name), $content, (New-Object Text.UTF8Encoding($false)))
}

$readme = @"
# 语音服务整合包（Windows / $Variant 版）

**双击「启动语音服务.bat」即可使用**，无需安装 Python、ffmpeg 或任何依赖。

## 快速开始

1. 双击 **启动语音服务.bat**（保持这个窗口开着；关掉窗口就等于停止服务）
2. 浏览器打开 http://127.0.0.1:$Port/api/health ，看到 `"status`":`"ok`" 就成功了
3. 双击 **测试合成.bat**，输入一句话试试（会保存到 output\ 并自动播放）

想让它长期在后台跑：双击 **后台启动.bat**（最小化运行，日志写入 logs\server.log），
停止用 **停止服务.bat**。

## 接到 AstrBot

1. 把本目录下的 ``astrbot_plugin_voice_reply`` 整个文件夹拷到 ``AstrBot/data/plugins/``
2. 在 AstrBot WebUI 里重载插件，配置填：

   ```yaml
   tts_server:
     url: "http://127.0.0.1:$Port"
     api_key: "$ApiKey"
     delivery: "file"
   ```

   AstrBot 装在别的机器上时，把 url 换成这台机器的局域网地址，
   例如 http://192.168.1.10:$Port（config.yaml 里 host 保持 0.0.0.0，并在防火墙放行 TCP $Port）。

## 目录说明

| 文件/目录 | 作用 |
| --- | --- |
| 启动语音服务.bat | 前台启动，能看到日志（关窗口即停止） |
| 后台启动.bat / 停止服务.bat | 后台常驻运行 / 停止 |
| 测试合成.bat | 输入文本 → 合成 → 自动播放 |
| app\voice_tts.py | 单文件调用库（复制到别的 Python 项目里用；用法见文件头注释） |
| 首次运行检查.bat | 检查配置与 CUDA 是否可用 |
| 编辑配置.bat | 用记事本打开 config.yaml |
| config.yaml | 唯一配置文件（端口 / API Key / 模型 / f0 / 保留时间 / 并发） |
| app\ | 服务端代码 + 测试客户端 |
| python\ | 独立 Python 运行时与全部依赖（请勿修改） |
| models\ | RVC 模型（.pth / .index） |
| hubert_base.pt、rmvpe.pt | 推理权重（已内置，无需联网） |
| bin\ffmpeg.exe | 内置 ffmpeg |
| output\ | 生成的音频（默认 10 分钟后自动清理） |
| logs\ | 后台运行时的日志 |

## 常见问题

| 现象 | 处理 |
| --- | --- |
| 双击后窗口一闪而过 | 先跑「首次运行检查.bat」看具体报错，常见是模型名与 models\ 里的文件不一致 |
| 提示 CUDA 不可用 | 没装 NVIDIA 驱动或不支持；服务会自动退回 CPU（慢一些但能用） |
| 显存只有 1~2GB | 会自动用 CPU（日志里会写明原因），这是预期行为；想用显卡请换 ≥4GB 的卡 |
| 合成慢 | 看「首次运行检查.bat」里的 `CUDA 可用:`；是 True 才在用显卡 |
| 想换音色 | 新 .pth（和 .index）放进 models\，改 config.yaml 的 rvc.model / rvc.index |
| 端口被占用 | 改 config.yaml 的 server.port，同时改 AstrBot 插件里的 url |
| 语音多长会转文字 | 由插件侧 ``voice.max_text_length``（默认 300 字）决定 |

构建时间：$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
"@
[IO.File]::WriteAllText((Join-Path $bundle "使用说明.md"), $readme, (New-Object Text.UTF8Encoding($false)))

$versionText = @"
bundle: $Name
variant: $Variant
python: $PythonVersion
torch: $(if ($Variant -eq 'cuda') { '2.11.0+cu128' } else { 'cpu' })
built_at: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
model: $(Split-Path $Model -Leaf)
"@
[IO.File]::WriteAllText((Join-Path $bundle "VERSION.txt"), $versionText, (New-Object Text.UTF8Encoding($false)))

if (-not $LaunchersOnly) {
    Say "7/7 自检"
    & $py -s (Join-Path $bundle "app\main.py") --config (Join-Path $bundle "config.yaml") --check-config
    & $py -c "import torch, fastapi, tts_with_rvc; print('torch', torch.__version__, '| CUDA:', torch.cuda.is_available())"
}

if ($Zip) {
    $zipPath = "$bundle.zip"
    if (Test-Path $zipPath) { python -c "import os,sys; os.remove(sys.argv[1])" $zipPath }
    $sevenZip = (Get-Command 7z -ErrorAction SilentlyContinue).Source
    if ($sevenZip) {
        Say "使用 7-Zip 压缩…"
        & $sevenZip a -tzip -mx=1 $zipPath (Join-Path $OutDir $Name) | Out-Null
    } else {
        Say "使用 Python zipfile 压缩（较慢）…"
        python (Join-Path $PSScriptRoot "zip_bundle.py") $bundle $zipPath 1
    }
    if (Test-Path $zipPath) {
        Say ("压缩包: {0}（{1:N1} GB）" -f $zipPath, ((Get-Item $zipPath).Length / 1GB))
    }
}

$size = (Get-ChildItem $bundle -Recurse -File | Measure-Object Length -Sum).Sum / 1GB
Write-Host ""
Say ("✅ 整合包构建完成：{0}" -f $bundle)
Say ("   大小 {0:N2} GB，双击「启动语音服务.bat」即可使用" -f $size)
