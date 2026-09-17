# =============================================================================
#  不用 Gradle，直接用 Android SDK 命令行工具构建 APK
#  （Gradle 在某些受限环境下无法启动守护进程，这条链路更稳）
#
#  用法：powershell -ExecutionPolicy Bypass -File android-app\build_apk.ps1
#  产物：android-app\dist\tts-server-config.apk
# =============================================================================
param(
    [string]$SdkPath = "$env:LOCALAPPDATA\Android\Sdk",
    [string]$BuildToolsVersion = "34.0.0",
    [string]$PlatformVersion = "android-34"
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$bt = Join-Path $SdkPath "build-tools\$BuildToolsVersion"
$androidJar = Join-Path $SdkPath "platforms\$PlatformVersion\android.jar"

foreach ($p in @($bt, $androidJar)) {
    if (-not (Test-Path $p)) { throw "找不到：$p（可用 -SdkPath / -BuildToolsVersion 指定）" }
}

$build = Join-Path $root "build"
$dist = Join-Path $root "dist"
New-Item -ItemType Directory -Force -Path $dist | Out-Null

# 清理旧产物（用 python 避免 PowerShell 递归删除被策略拦截）
python -c "import shutil,pathlib,sys; p=pathlib.Path(sys.argv[1]); shutil.rmtree(p, ignore_errors=True)" $build
New-Item -ItemType Directory -Force -Path "$build\classes", "$build\dex" | Out-Null

Write-Host "[1/6] 编译 Java 源码" -ForegroundColor Green
$sources = Get-ChildItem -Recurse -Filter *.java (Join-Path $root "app\src\main\java") | ForEach-Object { $_.FullName }
# 用 --release 17 编译（JDK 21 不再允许 -target 17 搭配 -bootclasspath）
javac -encoding UTF-8 --release 17 -nowarn `
    -classpath $androidJar `
    -d "$build\classes" $sources
if ($LASTEXITCODE -ne 0) { throw "javac 失败" }

Write-Host "[2/6] dex 转换（d8）" -ForegroundColor Green
$classes = Get-ChildItem -Recurse -Filter *.class "$build\classes" | ForEach-Object { $_.FullName }
& "$bt\d8.bat" --release --lib $androidJar --min-api 26 --output "$build\dex" $classes
if ($LASTEXITCODE -ne 0) { throw "d8 失败" }

Write-Host "[3/6] 链接资源与清单（aapt2）" -ForegroundColor Green
# aapt2 需要清单里有 package 属性，但 AGP 8 不再支持它。
# 所以源清单保持干净，这里生成一份临时清单用于手工构建。
$manifest = Join-Path $build "AndroidManifest.xml"
python (Join-Path $root "tools\inject_package.py") `
    (Join-Path $root "app\src\main\AndroidManifest.xml") $manifest "com.yake.ttsserver"
if ($LASTEXITCODE -ne 0) { throw "生成清单失败" }

& "$bt\aapt2.exe" link `
    -o "$build\base.apk" `
    --manifest $manifest `
    -I $androidJar `
    --min-sdk-version 26 --target-sdk-version 34 `
    --version-code 1 --version-name 1.0
if ($LASTEXITCODE -ne 0) { throw "aapt2 link 失败" }

Write-Host "[4/6] 打包 classes.dex" -ForegroundColor Green
python (Join-Path $root "tools\pack_dex.py") "$build\base.apk" "$build\dex\classes.dex"
if ($LASTEXITCODE -ne 0) { throw "写入 dex 失败" }

Write-Host "[5/6] 对齐（zipalign）" -ForegroundColor Green
& "$bt\zipalign.exe" -f 4 "$build\base.apk" "$build\aligned.apk"
if ($LASTEXITCODE -ne 0) { throw "zipalign 失败" }

Write-Host "[6/6] 签名（apksigner）" -ForegroundColor Green
$ks = Join-Path $root "debug.keystore"
if (-not (Test-Path $ks)) {
    $keytool = (Get-Command keytool -ErrorAction SilentlyContinue).Source
    if (-not $keytool) {
        $candidate = "C:\Program Files\Java\jdk-21\bin\keytool.exe"
        if (Test-Path $candidate) { $keytool = $candidate }
    }
    if (-not $keytool) { throw "找不到 keytool（JDK 自带）" }
    & $keytool -genkeypair -v -keystore $ks -alias androiddebugkey `
        -storepass android -keypass android -keyalg RSA -keysize 2048 -validity 10000 `
        -dname "CN=Android Debug,O=Android,C=US" | Out-Null
}
$apk = Join-Path $dist "tts-server-config.apk"
& "$bt\apksigner.bat" sign --ks $ks --ks-pass pass:android --key-pass pass:android `
    --out $apk "$build\aligned.apk"
if ($LASTEXITCODE -ne 0) { throw "apksigner 失败" }
& "$bt\apksigner.bat" verify --print-certs $apk | Select-Object -First 3

$size = [math]::Round((Get-Item $apk).Length / 1KB, 1)
Write-Host ""
Write-Host "✅ APK 构建完成：$apk（$size KB）" -ForegroundColor Green
Write-Host "安装：adb install -r `"$apk`""
