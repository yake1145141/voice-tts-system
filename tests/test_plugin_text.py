"""括号过滤 / 文本清理单元测试。

运行：python tests/test_plugin_text.py
"""

from __future__ import annotations

from plugin_harness import check, plugin_mod, report

CASES = [
    # (输入, 期望的 TTS 文本)
    ("你好呀！", "你好呀！"),
    ("你好呀！（开心地笑）", "你好呀！"),
    ("Hello! (smile)", "Hello!"),
    ("你好！[系统提示]", "你好！"),
    ("你好！【开心】", "你好！"),
    # 嵌套括号
    ("你好！（开心地说：[笑]）", "你好！"),
    ("你好！（外层（内层）结束）继续", "你好！继续"),
    # 多行
    ("你好！[挥手]\n很高兴认识你！", "你好！\n很高兴认识你！"),
    ("【系统提示】你好，很高兴见到你。", "你好，很高兴见到你。"),
    ("你好！[开心]\n\n今天过得怎么样？（微笑）\n\n我很高兴认识你！", "你好！\n今天过得怎么样？\n我很高兴认识你！"),
    # 过滤后为空
    ("（开心）[笑]【挥手】", ""),
    ("[笑]", ""),
    ("（开心）", ""),
    # 边界
    ("你好（没关闭", "你好"),
    ("你好]世界", "你好]世界"),
    ("", ""),
    ("   ", ""),
    ("（嵌套[未闭合", ""),
    # Markdown
    ("**你好**，`世界`！", "你好，世界！"),
    ("# 标题\n- 列表项", "标题\n列表项"),
    ("1. 第一项\n2. 第二项", "第一项\n第二项"),
    # 组合
    ("# 你好（小声）[笑]", "你好"),
    # 括号内的内容（包括颜文字）一律不朗读
    ("(╯°□°)（表情）", ""),
    ("(^_^) 你好", "你好"),
]


def main() -> int:
    print("=== 括号过滤 / 文本清理 ===")
    prepare = plugin_mod.prepare_tts_text
    for raw, expected in CASES:
        actual = prepare(raw)
        check(
            actual == expected,
            f"{raw!r} -> {actual!r}（期望 {expected!r}）",
        )

    # 关闭 Markdown 清理后，符号应当保留
    check(
        prepare("**你好**", False) == "**你好**",
        "cleanup_markdown=False 时保留 Markdown 符号",
    )
    # 直接调用底层函数
    check(plugin_mod.strip_bracket_content("a（b[c]d）e") == "ae", "strip_bracket_content 嵌套正确")
    return report("文本处理")


if __name__ == "__main__":
    raise SystemExit(main())
