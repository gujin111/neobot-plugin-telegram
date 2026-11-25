"""tg_bridge：绑定存储、回环防护、双向文本成型。"""

from __future__ import annotations

import json

import pytest
from tg_bridge import (
    Binding,
    BindingStore,
    LoopGuard,
    conversation_of,
    format_prefix,
    looks_like_command,
    render_qq_to_tg,
    render_tg_to_qq,
)
from tg_model import IncomingEvent, parse_text


def test_binding_validation_and_key():
    binding = Binding(tg_chat_id="-100", qq_kind="group", qq_id=123)
    assert binding.key == "-100->group:123"
    assert binding.created_at > 0
    with pytest.raises(ValueError):
        Binding(tg_chat_id="", qq_kind="group", qq_id=1)
    with pytest.raises(ValueError):
        Binding(tg_chat_id="-1", qq_kind="guild", qq_id=1)
    with pytest.raises(ValueError):
        Binding(tg_chat_id="-1", qq_kind="group", qq_id="")


def test_store_roundtrip_replace_and_remove(scratch):
    path = scratch / "bindings.json"
    store = BindingStore(path)
    assert store.load() == []
    store.add(Binding(tg_chat_id="-100", qq_kind="group", qq_id="1", tg_title="TG群"))
    store.add(Binding(tg_chat_id="-100", qq_kind="group", qq_id="2"))
    store.add(Binding(tg_chat_id="55", qq_kind="private", qq_id="9"))
    # 同 key 覆盖，不产生重复
    store.add(Binding(tg_chat_id="-100", qq_kind="group", qq_id="1", note="改了"))
    store.save()

    reloaded = BindingStore(path).load()
    assert len(reloaded) == 3
    fresh = BindingStore(path)
    fresh.load()
    # 覆盖后的那条带 note（按 key 查，别依赖列表顺序）
    target = next(item for item in fresh.by_tg("-100") if item.qq_id == "1")
    assert target.note == "改了"
    assert store.by_qq("private", "9")[0].tg_chat_id == "55"
    # 该 TG 会话有两条绑定，方向一起改
    assert store.set_direction(tg_chat_id="-100", qq_to_tg=False) == 2
    assert all(item.qq_to_tg is False for item in store.by_tg("-100"))
    assert store.remove(tg_chat_id="55") == 1
    assert store.remove(qq_kind="group", qq_id="2") == 1
    assert len(store.bindings) == 1


def test_store_survives_broken_file(scratch):
    path = scratch / "bindings.json"
    path.write_text("{ not json", encoding="utf-8")
    assert BindingStore(path).load() == []
    path.write_text(
        json.dumps({"bindings": [{"tg_chat_id": "-1", "qq_kind": "group", "qq_id": "1"}]}),
        encoding="utf-8",
    )
    assert len(BindingStore(path).load()) == 1


def test_loop_guard_ttl_and_normalization():
    guard = LoopGuard(ttl=10.0)
    guard.mark("Hello   World", now=100.0)
    assert guard.is_echo("hello world", now=101.0) is True
    assert guard.is_echo("hello world", now=111.0) is False
    assert guard.is_echo("other", now=101.0) is False
    assert LoopGuard.fingerprint(" A  b ") == LoopGuard.fingerprint("a b")


def test_format_prefix_missing_placeholder_becomes_empty():
    assert format_prefix("[TG {name}] ", name="小明") == "[TG 小明] "
    assert format_prefix("[TG {name}{unknown}] ", name="x") == "[TG x] "
    assert format_prefix("", name="x") == ""


def test_render_tg_to_qq_with_media_and_quote():
    event = IncomingEvent(
        kind="message",
        chat_id="-100",
        chat_title="TG群",
        user_id="7",
        user_name="ming",
        spans=parse_text("看图", []),
        media=[type("M", (), {"kind": "photo"})()],
        reply_to_text="原话",
    )
    text = render_tg_to_qq(event, prefix="[TG {name}] ", reply_text=event.reply_to_text)
    assert text.startswith("[TG ming] ")
    assert "↩ 原话" in text
    assert "ming: 看图 [photo]" in text


def test_render_qq_to_tg_strips_self_at_and_maps_media():
    event = {
        "message_type": "group",
        "group_id": "123",
        "user_id": "7",
        "sender": {"card": "小明"},
        "message": [
            {"type": "at", "data": {"qq": "999"}},
            {"type": "at", "data": {"qq": "8", "name": "小红"}},
            {"type": "text", "data": {"text": " 早上好"}},
            {"type": "image", "data": {"file": "a.png"}},
        ],
    }
    text = render_qq_to_tg(event, prefix="[QQ {name}] ", strip_at={"999"})
    assert text == "[QQ 小明] 小明: @小红 早上好[图片]"


def test_conversation_of_and_command_detection():
    assert conversation_of({"message_type": "group", "group_id": 12}) == ("group", "12")
    assert conversation_of({"message_type": "private", "user_id": 34}) == ("private", "34")
    assert conversation_of({"user_id": 1}) == ("group", "")
    assert looks_like_command(" /help") is True
    assert looks_like_command("你好 /help") is False
