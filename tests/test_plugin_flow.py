"""插件消息拦截流程测试（模拟 AstrBot 事件与 TTS 服务）。

运行：python tests/test_plugin_flow.py
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from plugin_harness import (
    FAILURES,
    check,
    config_with,
    event_for,
    make_plugin,
    plugin_mod,
    report,
    stub,
)


async def test_success_flow() -> None:
    print("成功替换为语音")
    plugin, client = await make_plugin()
    try:
        event = event_for("你好呀！（开心地笑）")
        await plugin.on_decorating_result(event)
        chain = event.get_result().chain
        check(len(chain) == 1, f"消息链只剩 1 段（实际 {len(chain)}）")
        check(isinstance(chain[0], stub.Record), "文本消息段被替换为语音消息段 Record")
        check(bool(chain[0].file), "Record 带有音频文件路径")
        check(chain[0].text == "你好呀！", "语音段附带过滤后的文本")
        check(
            bool(client.calls) and client.calls[0][1].endswith("/api/tts/file"),
            "默认使用文件方式请求音频",
        )
        audio_path = Path(str(chain[0].file))
        check(audio_path.exists() and audio_path.stat().st_size > 512, "音频已保存到本地缓存目录")
    finally:
        await plugin.terminate()


async def test_keep_text_flow() -> None:
    print("语音 + 原文双发（keep_text）")
    plugin, _ = await make_plugin(config_with(keep_text=True))
    try:
        event = event_for("你好呀！（开心地笑）")
        await plugin.on_decorating_result(event)
        chain = event.get_result().chain
        check(len(chain) == 2, f"消息链包含 2 段（实际 {len(chain)}）")
        check(isinstance(chain[0], stub.Record), "第一段是语音")
        check(isinstance(chain[1], stub.Plain), "第二段是原始文本")
    finally:
        await plugin.terminate()


async def test_multi_component_chain() -> None:
    print("图文混合消息链")
    plugin, _client = await make_plugin()
    try:
        event = stub.AstrMessageEvent(
            chain=[stub.Plain("你好呀！（开心）"), stub.Record(file="/tmp/sticker.wav")],
        )
        await plugin.on_decorating_result(event)
        chain = event.get_result().chain
        check(len(chain) == 2, f"保持 2 段（实际 {len(chain)}）")
        check(isinstance(chain[0], stub.Record) and chain[0].file != "/tmp/sticker.wav", "文本段被替换成新语音")
        check(isinstance(chain[1], stub.Record) and chain[1].file == "/tmp/sticker.wav", "原语音段保留")
    finally:
        await plugin.terminate()


async def test_url_delivery() -> None:
    print("URL 交付方式")
    config = {"tts_server": {"url": "http://127.0.0.1:8080", "api_key": "secret", "delivery": "url"}}
    plugin, client = await make_plugin(config, mode="url_only")
    try:
        event = event_for("你好")
        await plugin.on_decorating_result(event)
        chain = event.get_result().chain
        check(isinstance(chain[0], stub.Record), "替换为语音段")
        check(chain[0].file == "http://server/audio/abc.wav", f"使用服务返回的音频 URL（{chain[0].file}）")
        check(all(not url.endswith("/api/tts/file") for _, url in client.calls), "未调用文件接口")
    finally:
        await plugin.terminate()


async def test_auto_fallback_to_url() -> None:
    print("auto 模式：文件方式失败后回退 URL")
    plugin, client = await make_plugin(mode="url_only")
    try:
        event = event_for("你好")
        await plugin.on_decorating_result(event)
        chain = event.get_result().chain
        urls = [url for _, url in client.calls]
        check(any(url.endswith("/api/tts/file") for url in urls), "先尝试文件接口")
        check(any(url.endswith("/api/tts") for url in urls), "再回退到 JSON 接口")
        check(
            isinstance(chain[0], stub.Record) and str(chain[0].file).startswith("http"),
            "最终使用 URL 发送",
        )
    finally:
        await plugin.terminate()


async def test_failure_degrade() -> None:
    print("失败自动降级为文字（语音永远是增强功能）")
    cases = [
        ("connect_error", "服务无法连接"),
        ("timeout", "请求超时"),
        ("http_500", "RVC 转换失败"),
        ("unauthorized", "API Key 错误"),
        ("empty_audio", "音频生成失败"),
    ]
    for mode, label in cases:
        plugin, _ = await make_plugin(mode=mode)
        try:
            event = event_for("你好呀！")
            await plugin.on_decorating_result(event)
            chain = event.get_result().chain
            check(
                len(chain) == 1
                and isinstance(chain[0], stub.Plain)
                and chain[0].text == "你好呀！",
                f"{label}：原始文字回复保持不变",
            )
        finally:
            await plugin.terminate()


async def test_skip_conditions() -> None:
    print("不调用 TTS 的场景")
    plugin, client = await make_plugin()
    try:
        # 过滤后没有可朗读文本
        event = event_for("（开心）[笑]【挥手】")
        await plugin.on_decorating_result(event)
        check(isinstance(event.get_result().chain[0], stub.Plain), "只有括号内容 -> 保持文字")
        check(not client.calls, "未请求 TTS 服务")

        # 文本过长
        long_text = "这是一段很长的文本。" * 10
        event = event_for(long_text)
        await plugin.on_decorating_result(event)
        check(event.get_result().chain[0].text == long_text, "超长文本 -> 保持文字")
        check(not client.calls, "超长文本未请求 TTS 服务")

        # 平台不支持语音
        event = event_for("你好呀！", platform="qq_official")
        await plugin.on_decorating_result(event)
        check(isinstance(event.get_result().chain[0], stub.Plain), "QQ 官方接口 -> 保持文字")
        check(not client.calls, "不支持的平台未请求 TTS")

        # 全局开关关闭
        plugin._enabled_override = False
        event = event_for("你好呀！")
        await plugin.on_decorating_result(event)
        check(isinstance(event.get_result().chain[0], stub.Plain), "语音回复关闭 -> 保持文字")
        plugin._enabled_override = True

        # 插件自身指令的回复
        event = event_for("你好呀！")
        event.set_extra(plugin_mod.FLAG_SKIP, True)
        await plugin.on_decorating_result(event)
        check(isinstance(event.get_result().chain[0], stub.Plain), "带有 skip 标记 -> 保持文字")

        # 流式输出片段
        event = event_for("你好呀！")
        event.get_result().result_content_type = type("R", (), {"name": "STREAMING_RESULT"})()
        await plugin.on_decorating_result(event)
        check(isinstance(event.get_result().chain[0], stub.Plain), "流式片段 -> 保持文字")

        # 流式输出结束（AstrBot 不会发送该结果，插件只警告）
        event = event_for("你好呀！")
        event.get_result().result_content_type = type("R", (), {"name": "STREAMING_FINISH"})()
        await plugin.on_decorating_result(event)
        check(isinstance(event.get_result().chain[0], stub.Plain), "流式结束帧 -> 保持文字并提示")

        # 没有结果 / 空消息链
        event = stub.AstrMessageEvent(chain=[])
        event._result = None
        await plugin.on_decorating_result(event)
        check(True, "没有结果时不报错")
    finally:
        await plugin.terminate()


async def test_no_duplicate_processing() -> None:
    print("防重复处理（避免无限循环）")
    plugin, client = await make_plugin()
    try:
        event = event_for("你好呀！")
        await plugin.on_decorating_result(event)
        first_chain = list(event.get_result().chain)
        check(isinstance(first_chain[0], stub.Record), "第一次处理已转成语音")
        check(event.get_extra(plugin_mod.FLAG_HANDLED) is True, "事件被标记为已处理")
        count = len(client.calls)

        await plugin.on_decorating_result(event)
        check(len(client.calls) == count, "第二次进入钩子未再次请求 TTS")
        check(event.get_result().chain == first_chain, "消息链未被再次修改")

        event2 = stub.AstrMessageEvent(chain=[stub.Record(file="/tmp/x.wav")])
        await plugin.on_decorating_result(event2)
        check(len(client.calls) == count, "纯语音消息链不触发 TTS")
        check(len(event2.get_result().chain) == 1, "纯语音消息链保持原样")
    finally:
        await plugin.terminate()


async def test_only_llm_result() -> None:
    print("only_llm_result 配置")
    plugin, client = await make_plugin(config_with(only_llm_result=True))
    try:
        event = stub.AstrMessageEvent(chain=[stub.Plain("来自其它插件的文本")])
        event.get_result().is_model_result = lambda: False
        await plugin.on_decorating_result(event)
        check(not client.calls, "非 LLM 结果被跳过（保持文字）")
    finally:
        await plugin.terminate()


async def test_error_is_never_raised() -> None:
    print("异常兜底")
    plugin, _ = await make_plugin()
    try:
        event = event_for("你好呀！")

        async def boom(*args, **kwargs):
            raise RuntimeError("意外错误")

        plugin._synthesize = boom  # type: ignore[method-assign]
        await plugin.on_decorating_result(event)
        check(
            isinstance(event.get_result().chain[0], stub.Plain),
            "内部异常不会抛出，原始文字回复保持",
        )
    finally:
        await plugin.terminate()


async def test_commands() -> None:
    print("管理指令 /voice on|off|status")
    plugin, _client = await make_plugin()
    try:
        handler = plugin.voice_command

        results = [item async for item in handler(event_for("/voice on"), "on")]
        check(plugin.enabled is True, "/voice on 开启语音回复")
        check(bool(results) and "开启" in results[0][1], "返回开启提示")
        check(await plugin.get_kv_data("enabled", None) is True, "开关已持久化到 KV 存储")

        results = [item async for item in handler(event_for("/voice off"), "off")]
        check(plugin.enabled is False, "/voice off 关闭语音回复")
        check(bool(results) and "关闭" in results[0][1], "返回关闭提示")

        results = [item async for item in handler(event_for("/voice status"), "status")]
        status_text = results[0][1]
        check("语音回复状态" in status_text, "/voice status 输出状态")
        check("cuda:0" in status_text and "MyVoice.pth" in status_text, "状态包含服务端信息")
        check("音频保留=10分钟" in status_text, "状态包含服务端音频保留时长（10 分钟）")

        results = [item async for item in handler(event_for("/voice"), "status")]
        check("语音回复状态" in results[0][1], "无参数时默认查询状态")

        results = [item async for item in handler(event_for("/voice foo"), "foo")]
        check("用法" in results[0][1], "未知参数返回用法说明")

        event = event_for("/voice status")
        [item async for item in handler(event, "status")]
        check(event.get_extra(plugin_mod.FLAG_SKIP) is True, "指令回复带有 skip 标记（不会被转语音）")

        # 服务不可用时 status 仍然正常返回
        plugin._client.mode = "connect_error"
        results = [item async for item in handler(event_for("/voice status"), "status")]
        check("无法连接" in results[0][1], "服务不可用时状态查询提示无法连接")
    finally:
        await plugin.terminate()


async def test_cache_cleanup() -> None:
    print("本地音频缓存清理")
    plugin, _ = await make_plugin()
    try:
        assert plugin._cache_dir is not None
        plugin.cache_expire_minutes = 1
        fresh = plugin._cache_dir / "fresh.wav"
        fresh.write_bytes(b"x")
        stale = plugin._cache_dir / "stale.wav"
        stale.write_bytes(b"x")
        old = time.time() - 3600
        os.utime(stale, (old, old))
        removed = plugin._cleanup_cache_once()
        check(removed == 1, f"清理了 1 个过期文件（实际 {removed}）")
        check(fresh.exists(), "未过期文件保留")
        check(not stale.exists(), "过期文件被删除")
    finally:
        await plugin.terminate()


async def test_config_layer() -> None:
    print("配置读取（含 WebUI 字符串类型）")
    config = {
        "tts_server": {"url": "http://example.com:9000/", "api_key": "", "delivery": "invalid"},
        "voice": {
            "enabled": "false",
            "max_text_length": "200",
            "timeout": "45",
            "max_concurrent": "3",
            "skip_platforms": "qq_official, telegram",
        },
    }
    plugin = plugin_mod.VoiceReplyPlugin(context=stub.Context(), config=config)
    try:
        check(plugin.server_url == "http://example.com:9000", "URL 末尾斜杠被去掉")
        check(plugin.enabled is False, "字符串 'false' 正确解析为布尔值")
        check(plugin.max_text_length == 200, "字符串长度正确解析为整数")
        check(plugin.timeout == 45.0, "字符串超时正确解析")
        check(plugin.max_concurrent == 3, "字符串并发数正确解析")
        check(plugin.delivery == "auto", "非法 delivery 回退为 auto")
        check(plugin.skip_platforms == {"qq_official", "telegram"}, "逗号分隔的平台列表解析正确")
    finally:
        pass

    # 空配置时使用默认值
    plugin = plugin_mod.VoiceReplyPlugin(context=stub.Context(), config={})
    check(plugin.server_url == "http://127.0.0.1:8080", "缺省配置使用默认服务地址")
    check(plugin.max_text_length == 300, "缺省最大长度 300")
    check(plugin.enabled is True, "缺省开启语音回复")


async def main_async() -> int:
    print("=== AstrBot 语音回复插件自测 ===")
    await test_success_flow()
    await test_keep_text_flow()
    await test_multi_component_chain()
    await test_url_delivery()
    await test_auto_fallback_to_url()
    await test_failure_degrade()
    await test_skip_conditions()
    await test_no_duplicate_processing()
    await test_only_llm_result()
    await test_error_is_never_raised()
    await test_commands()
    await test_cache_cleanup()
    await test_config_layer()
    return report("插件流程")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
