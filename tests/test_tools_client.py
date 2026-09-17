"""校验测试客户端与服务端/插件的算法一致性。

重点：``tools/tts_client.py`` 里的文本过滤是插件算法的副本（为了让测试客户端
不依赖 AstrBot 也能预览"会发给 TTS 的文本"），本测试用固定用例 + 随机串
确保两者结果**逐字符一致**，避免日后改了一边忘了另一边。

运行：python tests/test_tools_client.py
"""

from __future__ import annotations

import importlib.util
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from plugin_harness import check, plugin_mod, report  # noqa: E402  (会注入 AstrBot 桩)


def load_client_module():
    spec = importlib.util.spec_from_file_location(
        "tts_test_client",
        ROOT / "tools" / "tts_client.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


client = load_client_module()

CASES = [
    "你好呀！",
    "你好呀！（开心地笑）",
    "Hello! (smile)",
    "你好！[系统提示]",
    "你好！【开心】",
    "你好！（开心地说：[笑]）",
    "你好！（外层（内层）结束）继续",
    "你好！[挥手]\n很高兴认识你！",
    "【系统提示】你好，很高兴见到你。",
    "（开心）[笑]【挥手】",
    "# 标题\n- 列表项\n1. 第一项",
    "**你好**，`世界`！",
    "你好（没关闭",
    "你好]世界",
    "(^_^) 你好",
    "",
    "   ",
    "正常文本，没有任何括号。",
]

FUZZ_CHARS = list("你好呀吗呢！？。，、（）()[]【】abc 12\n*-_`>#\u200b")


def main() -> int:
    print("=== 测试客户端算法一致性 ===")

    print("固定用例：prepare_tts_text")
    for raw in CASES:
        a = plugin_mod.prepare_tts_text(raw)
        b = client.prepare_tts_text(raw)
        check(a == b, f"{raw!r}: 插件={a!r} 客户端={b!r}")

    print("固定用例：strip_bracket_content / cleanup_markdown / normalize_text")
    for raw in CASES:
        check(
            plugin_mod.strip_bracket_content(raw) == client.strip_bracket_content(raw),
            f"strip_bracket_content 一致: {raw!r}",
        )
        check(
            plugin_mod.cleanup_markdown(raw) == client.cleanup_markdown(raw),
            f"cleanup_markdown 一致: {raw!r}",
        )
        check(
            plugin_mod.normalize_text(raw) == client.normalize_text(raw),
            f"normalize_text 一致: {raw!r}",
        )

    print("随机串一致性（2000 条）")
    rng = random.Random(20260913)
    mismatches = 0
    for _ in range(2000):
        raw = "".join(rng.choice(FUZZ_CHARS) for _ in range(rng.randint(0, 40)))
        if plugin_mod.prepare_tts_text(raw) != client.prepare_tts_text(raw):
            mismatches += 1
            if mismatches <= 3:
                print(f"    不一致: {raw!r}")
    check(mismatches == 0, f"2000 条随机文本过滤结果完全一致（不一致 {mismatches} 条）")

    print("其余工具函数")
    import inspect

    default_cleanup = inspect.signature(client.prepare_tts_text).parameters["cleanup_md"].default
    plugin_default = inspect.signature(plugin_mod.prepare_tts_text).parameters["cleanup_md"].default
    check(
        default_cleanup is True and plugin_default is True,
        f"两边默认都开启 Markdown 清理（客户端={default_cleanup}, 插件={plugin_default}）",
    )
    plain = client.prepare_tts_text("**你好**（笑）")
    check(plain == "你好", f"组合处理正确（{plain!r}）")

    return report("测试客户端一致性")


if __name__ == "__main__":
    raise SystemExit(main())
