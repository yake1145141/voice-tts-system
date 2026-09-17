# =============================================================================
#  Windows / macOS 上用 Docker 构建 Linux amd64 产物（免安装便携包）
#
#  用法（在项目根目录执行）：
#     powershell -ExecutionPolicy Bypass -File deploy\build_with_docker.ps1
#     powershell -ExecutionPolicy Bypass -File deploy\build_with_docker.ps1 -Cuda -CudaVersion 12.8
#     powershell -ExecutionPolicy Bypass -File deploy\build_with_docker.ps1 -Model C:\path\voice.pth -Index C:\path\voice.index
#     powershell -ExecutionPolicy Bypass -File deploy\build_with_docker.ps1 -PyInstaller
#
#  前置：安装并启动 Docker Desktop（能运行 linux/amd64 容器），首次构建会下载 2~3GB。
# =============================================================================

[CmdletBinding()]
param(
    [switch]$Cuda,
    [string]$CudaVersion = "12.8",
    [string]$Model = "",
    [string]$Index = "",
    [string]$Assets = "",
    [string]$Name = "",
    [switch]$PyInstaller,
    [switch]$NoFfmpeg
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$distDir = Join-Path $projectRoot "dist"
New-Item -ItemType Directory -Force -Path $distDir | Out-Null

Write-Host "[docker] 项目目录: $projectRoot" -ForegroundColor Green
Write-Host "[docker] 产物目录: $distDir" -ForegroundColor Green

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "未找到 docker 命令。请先安装 Docker Desktop 并启动它。"
}

# 1) 构建镜像（含编译工具链）
Write-Host "[docker] 构建构建镜像 tts-builder ..." -ForegroundColor Green
docker build --platform linux/amd64 -f (Join-Path $projectRoot "deploy/Dockerfile.build") -t tts-builder $projectRoot
if ($LASTEXITCODE -ne 0) { throw "镜像构建失败" }

# 2) 组装构建命令
$argsList = @()
if ($PyInstaller) {
    $argsList += "bash", "deploy/build_linux_executable.sh"
} else {
    $argsList += "bash", "deploy/build_linux_bundle.sh"
}
$argsList += if ($Cuda) { "--cuda", $CudaVersion } else { "--cpu" }
$argsList += "--out-dir", "/dist"
if ($NoFfmpeg) { $argsList += "--no-ffmpeg" }

# 模型/权重需要先放进镜像再使用：把路径转成容器内的 /src/... 形式
function Convert-ToContainerPath([string]$path, [string]$prefix) {
    if (-not $path) { return "" }
    $full = (Resolve-Path $path).Path
    if (-not $full.StartsWith($projectRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "文件必须在项目目录内才能带进 Docker 镜像：$full`n请先复制到 $projectRoot\$prefix\ 下再构建。"
    }
    $relative = $full.Substring($projectRoot.Length).TrimStart('\', '/')
    # 直接引用镜像里 /src 下对应的位置
    return "/src/$($relative -replace '\\','/')"
}

if ($Model) { $argsList += "--model", (Convert-ToContainerPath $Model "models") }
if ($Index) { $argsList += "--index", (Convert-ToContainerPath $Index "models") }
if ($Assets) { $argsList += "--assets", (Convert-ToContainerPath $Assets "assets") }
if ($Name) { $argsList += "--name", $Name }

Write-Host "[docker] 运行构建：$($argsList -join ' ')" -ForegroundColor Green

# 3) 运行构建容器，把 dist 挂载出来
$mount = "$($distDir):/dist"
docker run --rm -v $mount tts-builder @argsList
if ($LASTEXITCODE -ne 0) { throw "构建失败" }

Write-Host ""
Write-Host "[docker] 构建完成，产物：" -ForegroundColor Green
Get-ChildItem $distDir -Filter "*.tar.gz" | ForEach-Object {
    Write-Host ("  {0}  ({1:N1} MB)" -f $_.Name, ($_.Length / 1MB))
}
Write-Host ""
Write-Host "分发到 Linux 服务器：" -ForegroundColor Green
Write-Host "  scp dist\<包名>.tar.gz user@server:/opt/"
Write-Host "  ssh user@server 'cd /opt && tar -xzf <包名>.tar.gz && cd <解压目录> && ./run.sh'"
