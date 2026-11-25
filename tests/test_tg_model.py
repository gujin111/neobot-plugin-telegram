"""tg_model：实体解析、命令剥离、媒体解码、相册聚合、事件归一化。"""

from __future__ import annotations

from tg_model import (
    IncomingEvent,
    MediaGroupBuffer,
    classify_update,
    decode_message,
    decode_update,
    is_bot_command,
    merge_media_group,
    parse_text,
    spans_to_html,
    strip_command_mention,
    update_subtype,
)


def test_parse_text_styles_links_mentions_and_breaks():
    text = "hi bold link me\nnext"
    entities = [
        {"type": "bold", "offset": 3, "length": 4},
        {"type": "text_link", "offset": 8, "length": 4, "url": "https://example.com"},
        {"type": "text_mention", "offset": 13, "length": 2, "user": {"id": 42, "username": "bob"}},
    ]
    spans = parse_text(text, entities)
    kinds = [span.type for span in spans]
    assert "b" in kinds and "a" in kinds and "at" in kinds and "br" in kinds
    link = next(span for span in spans if span.type == "a")
    assert link.url == "https://example.com" and link.text == "link"
    mention = next(span for span in spans if span.type == "at")
    assert mention.user_id == "42" and mention.user_name == "bob"
    # 拼接回来必须还原原文
    assert "".join(span.text for span in spans) == text


def test_parse_text_nested_and_unknown_entities():
    spans = parse_text("code", [{"type": "code", "offset": 0, "length": 4}])
    assert spans[0].type == "code"
    # 未知实体不影响文本
    spans = parse_text("abc", [{"type": "custom_emoji", "offset": 0, "length": 3}])
    assert "".join(span.text for span in spans) == "abc"


def test_spans_to_html_escapes():
    spans = parse_text("<b>", [{"type": "bold", "offset": 0, "length": 3}])
    assert spans_to_html(spans) == "<b>&lt;b&gt;</b>"


def test_bot_command_detection_is_empty_safe():
    # Koishi 的 entities?.[0].type 在空数组上会抛 TypeError，这里必须安全
    assert is_bot_command({}) is False
    assert is_bot_command({"entities": []}) is False
    assert is_bot_command({"entities": [{"type": "bold"}]}) is False
    assert is_bot_command({"entities": [{"type": "bot_command"}]}) is True


def test_strip_command_mention():
    assert strip_command_mention("/mute@mybot 10m", "mybot") == "/mute 10m"
    assert strip_command_mention("/mute 10m", "mybot") == "/mute 10m"
    assert strip_command_mention("/mute@otherbot 10m", "mybot") is None
    assert strip_command_mention("hello", "mybot") == "hello"


def test_update_classification_and_internal_event_names():
    assert classify_update({"update_id": 1, "message": {}}) == "message"
    assert classify_update({"update_id": 1, "edited_message": {}}) == "message_updated"
    assert classify_update({"update_id": 1, "callback_query": {}}) == "callback_query"
    assert update_subtype({"update_id": 1, "chat_join_request": {}}) == "telegram/chat-join-request"
    assert classify_update({"update_id": 1}) == "unknown"


def test_decode_message_picks_largest_photo_and_reads_reply():
    message = {
        "message_id": 9,
        "date": 1700000000,
        "chat": {"id": -100, "type": "supergroup", "title": "群"},
        "from": {"id": 7, "first_name": "小明", "username": "ming"},
        "caption": "看图",
        "caption_entities": [{"type": "bold", "offset": 0, "length": 2}],
        "photo": [
            {"file_id": "small", "file_size": 10, "width": 90},
            {"file_id": "big", "file_size": 999, "width": 1280},
        ],
        "message_thread_id": 55,
        "reply_to_message": {"message_id": 3, "text": "原话"},
    }
    event = decode_message(message)
    assert event.chat_id == "-100" and event.chat_type == "supergroup"
    assert event.thread_id == 55 and event.reply_to_message_id == 3
    assert event.reply_to_text == "原话"
    assert [item.file_id for item in event.media] == ["big"]
    assert event.caption == "看图"
    assert event.spans[0].type == "b"


def test_decode_update_callback_join_request_and_member_change():
    callback = decode_update(
        {
            "update_id": 5,
            "callback_query": {
                "id": "cb1",
                "data": "mute:7",
                "from": {"id": 7, "first_name": "小明"},
                "message": {"message_id": 1, "chat": {"id": -100, "type": "supergroup", "title": "群"}},
            },
        }
    )
    assert callback is not None and callback.kind == "callback_query"
    assert callback.callback_id == "cb1" and callback.callback_data == "mute:7"

    join = decode_update(
        {
            "update_id": 6,
            "chat_join_request": {
                "date": 1700000000,
                "chat": {"id": -100, "type": "supergroup", "title": "群"},
                "from": {"id": 8, "first_name": "小红"},
            },
        }
    )
    assert join is not None and join.kind == "chat_join_request" and join.join_request_user_id == "8"

    added = decode_update(
        {
            "update_id": 7,
            "my_chat_member": {
                "date": 1700000000,
                "chat": {"id": -100, "type": "supergroup", "title": "群"},
                "from": {"id": 9},
                "old_chat_member": {"status": "left"},
                "new_chat_member": {"status": "member"},
            },
        }
    )
    assert added is not None and added.my_chat_member_state == "group-added"

    removed = decode_update(
        {
            "update_id": 8,
            "my_chat_member": {
                "date": 1700000000,
                "chat": {"id": -100, "type": "supergroup"},
                "from": {"id": 9},
                "old_chat_member": {"status": "member"},
                "new_chat_member": {"status": "kicked"},
            },
        }
    )
    assert removed is not None and removed.my_chat_member_state == "group-deleted"
    assert decode_update({"update_id": 9, "poll": {}}) is None


def test_media_group_buffer_merges_after_window():
    clock = {"now": 100.0}
    buffer = MediaGroupBuffer(window=1.2, clock=lambda: clock["now"])
    first = DecodedStub(message_id=2, media=["a"], caption="图一")
    second = DecodedStub(message_id=1, media=["b"], caption="图二")
    assert buffer.push(first) is None
    assert buffer.push(second) is None
    assert buffer.flush_due() == []  # 窗口未到
    clock["now"] += 1.3
    ready = buffer.flush_due()
    assert len(ready) == 1
    merged = ready[0]
    assert merged.message_id == 1  # 按 message_id 排序取最小
    assert merged.media_group_message_ids == [1, 2]
    assert merged.media == ["b", "a"]
    assert buffer.has_pending() is False


def DecodedStub(*, message_id: int, media: list[str], caption: str) -> IncomingEvent:
    event = IncomingEvent(kind="message", chat_id="-100", message_id=message_id, media_group_id="g1")
    event.media = list(media)  # type: ignore[assignment]
    event.spans = parse_text(caption, [])
    return event


def test_merge_media_group_keeps_head_metadata():
    merged = merge_media_group([DecodedStub(message_id=5, media=["x"], caption=""), DecodedStub(message_id=4, media=["y"], caption="")])
    assert merged.message_id == 4
    assert merged.media_group_id == "g1"
    assert merged.media == ["y", "x"]
