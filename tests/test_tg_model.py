"""Update 解码：拍平成事件对象。"""

from __future__ import annotations

from tg_model import decode_callback, decode_message, decode_update, display_name, media_kinds, update_subtype


def message(**overrides) -> dict:
    base = {
        "message_id": 11,
        "date": 1700000000,
        "chat": {"id": 778899, "type": "private", "first_name": "小明", "username": "ming"},
        "from": {"id": 7, "first_name": "小明", "username": "ming"},
        "text": "你好",
    }
    base.update(overrides)
    return base


def test_decode_message_basic_fields():
    event = decode_message(message())
    assert event.kind == "message"
    assert event.chat_id == "778899"
    assert event.chat_type == "private"
    assert event.message_id == 11
    assert event.user_id == "7"
    assert event.user_name == "ming"
    assert event.user_is_bot is False
    assert event.body == "你好"


def test_decode_message_marks_bots():
    event = decode_message(message(**{"from": {"id": 5, "is_bot": True, "username": "other_bot"}}))
    assert event.user_is_bot is True


def test_body_falls_back_to_caption_then_media():
    assert decode_message(message(**{"text": None, "caption": "图片说明"})).body == "图片说明"
    photo = decode_message(message(**{"text": None, "caption": None, "photo": [{"file_id": "f"}]}))
    assert photo.body == "[photo]"
    empty = decode_message(message(**{"text": None, "caption": None}))
    assert empty.body == ""


def test_media_kinds_are_ordered_and_deduplicated():
    kinds = media_kinds({"photo": [{}], "document": {"file_id": "d"}, "text": "x"})
    assert kinds == ["photo", "document"]
    assert media_kinds({}) == []


def test_display_name_prefers_username():
    assert display_name({"username": "u", "first_name": "昵称"}) == "u"
    assert display_name({"first_name": "昵称"}) == "昵称"
    assert display_name({"id": 42}) == "42"


def test_decode_update_handles_message_callback_and_noise():
    assert decode_update({"update_id": 1, "message": message()}).kind == "message"
    callback = decode_update(
        {"update_id": 2, "callback_query": {"id": "cb1", "data": "ok", "from": {"id": 7}, "message": message()}}
    )
    assert callback.kind == "callback_query"
    assert callback.callback_id == "cb1"
    assert callback.callback_data == "ok"
    assert decode_update({"update_id": 3, "chat_member": {}}) is None
    assert decode_update("nonsense") is None


def test_decode_callback_without_message():
    event = decode_callback({"id": "cb2", "from": {"id": 9, "username": "x"}})
    assert event.chat_id == ""
    assert event.user_id == "9"
    assert event.callback_id == "cb2"


def test_update_subtype_for_logs():
    assert update_subtype({"message": {}}) == "message"
    assert update_subtype({"chat_member": {}}) == "chat_member"
    assert update_subtype({"edited_message": {}}) == "edited_message"
    assert update_subtype({}) == "unknown"
