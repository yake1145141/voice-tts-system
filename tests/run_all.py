"""一键运行全部自测。

    python tests/run_all.py

包含：
  1. 语音处理端 API 自测（桩函数，秒级）
  2. 插件文本处理自测
  3. 插件拦截流程自测（AstrBot 桩）
  4. 测试客户端算法一致性自测
  5. Linux 打包产物布局校验（便携包目录结构 / 冻结路径 / run.sh 语法）
  6. 显存不足时的降级行为（低显存走 CPU / OOM 自动降级重试）
  7. 单文件客户端 voice_tts.py（假 HTTP 服务，覆盖合成/播放/发现/错误）
  8. 真实 AstrBot 集成校验（仅当当前 Python 环境已安装 AstrBot 时执行）
  9. 网页控制台与密码验证（登录 / 会话 Cookie / 篡改拒绝 / 登出）
 10. 长文本分段（切分规则 / 不丢内容 / 音频拼接 / 语音源回退顺序）

可选：``python tests/run_all.py --real`` 额外做真实的 Edge TTS 合成与真实服务进程端到端测试（需要联网）。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent


def run(script: str, args: list[str] | None = None) -> int:
    print(f"\n{'=' * 70}\n>>> {script} {' '.join(args or [])}\n{'=' * 70}")
    result = subprocess.run(
        [sys.executable, str(TESTS_DIR / script), *(args or [])],
        check=False,
    )
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--real", action="store_true", help="额外执行真实 Edge TTS 合成")
    args = parser.parse_args()

    codes = {
        "语音处理端 API": run("test_tts_server_api.py", ["--real"] if args.real else []),
        "插件文本处理": run("test_plugin_text.py"),
        "插件拦截流程": run("test_plugin_flow.py"),
        "测试客户端一致性": run("test_tools_client.py"),
        "Linux 打包布局": run("test_bundle_layout.py"),
        "低显存降级行为": run("test_low_vram.py"),
        "单文件客户端": run("test_voice_tts.py"),
        "真实 AstrBot 集成": run("test_astrbot_real_api.py"),
        "网页控制台与密码验证": run("test_webui_auth.py"),
        "长文本分段": run("test_chunking.py"),
    }
    if args.real:
        codes["真实服务进程端到端"] = run("test_server_live.py")

    print(f"\n{'=' * 70}")
    failed = [name for name, code in codes.items() if code != 0]
    for name, code in codes.items():
        print(f"  {'✅' if code == 0 else '❌'} {name} (exit={code})")
    if failed:
        print(f"\n❌ 有 {len(failed)} 个测试套件失败: {', '.join(failed)}")
        return 1
    print("\n✅ 全部测试套件通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
