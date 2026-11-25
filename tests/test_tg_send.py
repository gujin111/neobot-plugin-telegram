"""tg_send：HTML 转义、标签安全分段、媒体方法映射、发送计划。"""

from __future__ import annotations

from tg_send import (
    CAPTION_LIMIT,
    PlanOptions,
    RenderedMessage,
    build_actions,
    escape_html,
    media_method,
    render_segments,
    split_html,
)


def test_escape_html():
    assert escape_html("<b>&") == "&lt;b&gt;&amp;"
    assert escape_html('a"b', quote=True) == "a&quot;b"


def test_render_segments_covers_common_onebot_segments():
    segments = [
        {"type": "reply", "data": {"id": 42}},
        {"type": "at", "data": {"qq": "7", "name": "小明"}},
        {"type": "text", "data": {"text": " 你好 <b>"}},
        {"type": "image", "data": {"file": "a.png"}},
        {"type": "face", "data": {"id": "1"}},
    ]
    rendered = render_segments(segments)
    assert rendered.reply_to_message_id == 42
    assert '<a href="tg://user?id=7">@小明</a>' in rendered.text
    assert "&lt;b&gt;" in rendered.text
    assert len(rendered.media) == 1 and rendered.media[0].kind == "photo"
    assert "[图片]" in rendered.plain


def test_render_segments_buttons_and_grouping():
    segments = [
        {"type": "text", "data": {"text": "选一个"}},
        {
            "type": "button-group",
            "children": [
                {"type": "button", "data": {"id": "a", "type": "callback"}, "children": [{"type": "text", "data": {"text": "A"}}]},
                {"type": "button", "data": {"href": "https://x", "type": "link"}, "children": [{"type": "text", "data": {"text": "B"}}]},
            ],
        },
    ]
    rendered = render_segments(segments)
    assert rendered.rows == [[{"text": "A", "callback_data": "a"}, {"text": "B", "url": "https://x"}]]


def test_split_html_keeps_tags_balanced():
    html = "<b>" + "x" * 30 + "</b>" + "y" * 30
    chunks = split_html(html, limit=20)
    assert len(chunks) >= 3
    for chunk in chunks:
        assert chunk.count("<b>") == chunk.count("</b>")
    assert "".join(chunk.replace("<b>", "").replace("</b>", "") for chunk in chunks) == "x" * 30 + "y" * 30


def test_media_method_mapping():
    assert media_method("photo", "a.gif", "image/gif") == ("sendAnimation", "animation")
    assert media_method("photo", "a.jpg", "image/jpeg") == ("sendPhoto", "photo")
    assert media_method("voice", "a.ogg", "audio/ogg") == ("sendVoice", "voice")
    assert media_method("audio", "a.mp3", "audio/mpeg") == ("sendAudio", "audio")
    assert media_method("video", "a.mp4", "video/mp4") == ("sendVideo", "video")
    assert media_method("document", "", "") == ("sendDocument", "document")
    assert media_method("unknown", "", "application/pdf") == ("sendDocument", "document")


def test_build_actions_single_media_with_buttons():
    rendered = RenderedMessage(text="说明", media=[_photo(b"1")], rows=[[{"text": "按钮", "callback_data": "x"}]])
    actions = build_actions(-100, rendered, options=PlanOptions())
    assert len(actions) == 1
    action = actions[0]
    assert action.method == "sendPhoto"
    assert action.params["caption"] == "说明"
    assert action.params["reply_markup"]["inline_keyboard"][0][0]["text"] == "按钮"
    assert action.files and action.files[0][1][2] == "image/png"


def test_build_actions_media_group_then_button_message():
    rendered = RenderedMessage(
        text="相册说明",
        media=[_photo(b"1"), _photo(b"2"), _photo(b"3")],
        rows=[[{"text": "按钮", "callback_data": "x"}]],
    )
    actions = build_actions(-100, rendered, options=PlanOptions(), reply_to_message_id=9)
    assert [action.method for action in actions] == ["sendMediaGroup", "sendMessage"]
    group = actions[0]
    assert group.params["reply_to_message_id"] == 9
    assert '"attach://file0"' in group.params["media"]
    assert len(group.files) == 3
    assert actions[1].params["text"] == "相册说明"


def test_build_actions_long_text_is_split_and_buttons_on_last_chunk():
    rendered = RenderedMessage(text="字" * 40, rows=[[{"text": "b", "callback_data": "x"}]])
    actions = build_actions(-100, rendered, options=PlanOptions(chunk_size=15))
    assert len(actions) == 3
    assert all(action.method == "sendMessage" for action in actions)
    assert "reply_markup" not in actions[0].params
    assert "reply_markup" in actions[-1].params


def test_build_actions_caption_overflow_sends_text_separately():
    rendered = RenderedMessage(text="x" * (CAPTION_LIMIT + 10), media=[_photo(b"1")])
    actions = build_actions(-100, rendered, options=PlanOptions(chunk_size=5000))
    assert actions[0].method == "sendPhoto"
    assert actions[0].params["caption"] == ""
    assert actions[1].method == "sendMessage"
    assert actions[1].params["text"].startswith("x")


def test_build_actions_buttons_only_message_is_still_sent():
    rendered = RenderedMessage(text="", rows=[[{"text": "只有按钮", "callback_data": "x"}]])
    actions = build_actions(-100, rendered)
    assert len(actions) == 1 and actions[0].method == "sendMessage"
    assert actions[0].params["reply_markup"]["inline_keyboard"][0][0]["text"] == "只有按钮"


def _photo(data: bytes):
    from tg_send import OutgoingMedia

    return OutgoingMedia(kind="photo", data=data, filename="a.png", mime="image/png")
