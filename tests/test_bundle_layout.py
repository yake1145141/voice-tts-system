"""校验 Linux 打包产物（便携包 / PyInstaller）的目录布局与路径解析逻辑。

在没有 Linux 构建机的情况下，这个测试用系统 Python 还原"便携包解压后的目录结构"，
验证最容易出错的三件事：
  1. 从任意 cwd 以 `python <bundle>/app/main.py --config <bundle>/config.yaml` 启动能正常跑；
  2. config.yaml 里的相对路径（输出目录、模型目录）都解析到包内，而不是当前工作目录；
  3. 冻结（PyInstaller）环境下配置文件查找的是"可执行文件同目录"。

运行：python tests/test_bundle_layout.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER_DIR = ROOT / "tts-server"
PORT = int(os.environ.get("BUNDLE_TEST_PORT", "18095"))

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"  [PASS] {message}")
    else:
        print(f"  [FAIL] {message}")
        FAILURES.append(message)


def build_fake_bundle(base: Path) -> Path:
    """按 deploy/build_linux_bundle.sh 的布局生成一个假便携包。"""
    bundle = base / "tts-server-linux-amd64-cpu"
    for sub in ("app", "bin", "models", "output", "tools"):
        (bundle / sub).mkdir(parents=True, exist_ok=True)
    # 直接按源码目录里的 *.py 拷贝，避免以后新增模块（如 webui.py）忘了同步到打包脚本
    for src in sorted(SERVER_DIR.glob("*.py")):
        shutil.copy(src, bundle / "app" / src.name)
    # 网页管理面板的静态资源
    if (SERVER_DIR / "static").is_dir():
        shutil.copytree(SERVER_DIR / "static", bundle / "app" / "static")
    shutil.copy(ROOT / "tools" / "tts_client.py", bundle / "tools" / "tts_client.py")

    # 使用相对路径，模拟真实分发包里的 config.yaml（rvc 关掉，便于无模型自测）
    (bundle / "config.yaml").write_text(
        "\n".join(
            [
                "server:",
                '  host: "127.0.0.1"',
                f"  port: {PORT}",
                '  log_level: "INFO"',   # --check-config 会把校验结果打到日志里
                "security:",
                '  api_key: "bundle-key"',
                "tts:",
                '  source: "edgetts"',
                '  speaker: "zh-CN-YunxiNeural"',
                "  pitch: 5",
                "rvc:",
                "  enabled: false",
                '  model: ""',
                "  preload: false",
                "storage:",
                '  output_dir: "./output"',   # 相对路径：必须解析到包内
                "  expire_minutes: 10",
                "queue:",
                "  max_concurrent: 2",
                "  timeout: 60",
            ],
        ),
        encoding="utf-8",
    )
    return bundle


def test_check_config_from_other_cwd(bundle: Path, tmp: Path) -> None:
    print("从任意目录启动（模拟 ./run.sh 的工作方式）")
    env = {k: v for k, v in os.environ.items() if k != "TTS_SERVER_CONFIG"}
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, str(bundle / "app" / "main.py"), "--config", str(bundle / "config.yaml"),
         "--check-config"],
        cwd=str(tmp),                      # 故意不在包目录里
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        env=env,
        timeout=120,
    )
    check(proc.returncode == 0, f"--check-config 退出码 0（实际 {proc.returncode}）")
    output = proc.stdout + proc.stderr
    expected_output = str((bundle / "output").resolve())
    check(
        expected_output in output.replace("\\\\", "\\"),
        f"相对路径 output_dir 解析到包内（期望含 {expected_output}）",
    )
    check(
        str(bundle.resolve()) in output.replace("\\\\", "\\"),
        "配置文件路径指向包内 config.yaml",
    )


def test_service_and_client(bundle: Path, tmp: Path) -> None:
    print("用包内布局启动服务并用包内客户端调用")
    env = {k: v for k, v in os.environ.items() if k != "TTS_SERVER_CONFIG"}
    env.update({"PYTHONIOENCODING": "utf-8", "TTS_SERVER_CONFIG": str(bundle / "config.yaml")})
    log = (tmp / "bundle-server.log").open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(bundle / "app" / "main.py"), "--config", str(bundle / "config.yaml")],
        cwd=str(bundle),
        stdout=log,
        stderr=subprocess.STDOUT,
        env=env,
    )
    try:
        base = f"http://127.0.0.1:{PORT}"
        ready = False
        for _ in range(60):
            if proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen(f"{base}/api/health", timeout=5) as resp:
                    if json.loads(resp.read())["success"]:
                        ready = True
                        break
            except Exception:
                time.sleep(1)
        check(ready, "包内服务成功启动并响应 /api/health")
        if not ready:
            log.close()
            print((tmp / "bundle-server.log").read_text(encoding="utf-8", errors="ignore")[-1500:])
            return

        # 使用包内的测试客户端（模拟 ./tools/check.sh）
        client = subprocess.run(
            [sys.executable, str(bundle / "tools" / "tts_client.py"),
             "--url", base, "--api-key", "bundle-key", "say", "便携包自检。"],
            cwd=str(bundle),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            env=env,
            timeout=300,
        )
        check(client.returncode == 0, f"包内客户端 say 成功（退出码 {client.returncode}）")
        check("保存文件" in client.stdout, "客户端保存了音频文件")

        # 音频应落在包内 output/ 目录（相对路径解析正确）
        produced = list((bundle / "output").glob("*.wav"))
        check(bool(produced), f"音频写到了包内 output/（实际 {len(produced)} 个文件）")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


def test_frozen_config_path(tmp: Path) -> None:
    print("冻结（PyInstaller）环境的配置文件查找")
    fake_exe = tmp / "tts-server"
    code = (
        "import sys, pathlib\n"
        f"sys.frozen = True\n"
        f"sys.executable = r'{fake_exe}'\n"
        f"sys.path.insert(0, r'{SERVER_DIR}')\n"
        "import config\n"
        "print(config.default_config_path())\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        timeout=60,
    )
    check(proc.returncode == 0, "冻结环境下 import config 正常")
    check(
        proc.stdout.strip() == str((tmp / "config.yaml").resolve()),
        f"配置文件定位到可执行文件同目录（实际 {proc.stdout.strip()}）",
    )

    # 源码运行时定位到 tts-server/config.yaml
    proc = subprocess.run(
        [sys.executable, "-c",
         f"import sys; sys.path.insert(0, r'{SERVER_DIR}'); import config; "
         "print(config.default_config_path())"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        env={k: v for k, v in os.environ.items() if k != "TTS_SERVER_CONFIG"},
        timeout=60,
    )
    check(
        proc.stdout.strip() == str((SERVER_DIR / "config.yaml").resolve()),
        f"源码运行时定位到 tts-server/config.yaml（实际 {proc.stdout.strip()}）",
    )


def test_run_sh_template() -> None:
    """把打包脚本里生成的 run.sh 模板抽出来做 bash 语法校验。"""
    print("run.sh 模板语法")
    script = (ROOT / "deploy" / "build_linux_bundle.sh").read_text(encoding="utf-8")
    start = script.find("<<'RUNSH'")
    end = script.find("RUNSH", start + 10)
    if start == -1 or end == -1:
        check(False, "在 build_linux_bundle.sh 中找不到 RUNSH 模板", )
        return
    template = script[start + len("<<'RUNSH'"):end].strip("\n")
    check("exec " in template and "$HERE/config.yaml" in template, "模板包含启动命令且配置路径为包内绝对路径")
    check("CONFIG_PATH_PLACEHOLDER" not in template, "模板里没有遗留占位符")

    bash = shutil.which("bash") or (
        r"C:\Program Files\Git\bin\bash.exe" if Path(r"C:\Program Files\Git\bin\bash.exe").exists() else None
    )
    if not bash:
        print("  [SKIP] 未找到 bash，跳过语法校验")
        return
    with tempfile.TemporaryDirectory() as tmp_name:
        path = Path(tmp_name) / "run.sh"
        path.write_text(template + "\n", encoding="utf-8", newline="\n")
        proc = subprocess.run([bash, "-n", str(path)], capture_output=True, text=True, timeout=60)
    check(proc.returncode == 0, f"run.sh 模板 bash 语法正确（{proc.stderr.strip()[:80]}）")


def main() -> int:
    print("=== Linux 打包产物布局校验 ===")
    test_run_sh_template()
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        bundle = build_fake_bundle(tmp)
        test_check_config_from_other_cwd(bundle, tmp)
        test_service_and_client(bundle, tmp)
        test_frozen_config_path(tmp)

    print()
    if FAILURES:
        print(f"❌ 失败 {len(FAILURES)} 项:")
        for item in FAILURES:
            print(f"   - {item}")
        return 1
    print("✅ 打包布局校验全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
