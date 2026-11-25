"""出站文本：转义与分段。"""

from __future__ import annotations

from tg_send import escape_html, split_text


def test_escape_html_covers_specials_and_non_strings():
    assert escape_html("<b>&</b>") == "&lt;b&gt;&amp;&lt;/b&gt;"
    assert escape_html(None) == ""
    assert escape_html(123) == "123"


def test_split_text_empty_and_short():
    assert split_text("") == []
    assert split_text("   \n ") == []
    assert split_text("你好") == ["你好"]


def test_split_text_prefers_newline():
    body = "x" * 90 + "\n" + "y" * 90
    chunks = split_text(body, 100)
    assert len(chunks) == 2
    assert chunks[0] == "x" * 90
    assert chunks[1] == "y" * 90


def test_split_text_hard_cuts_without_break_point():
    body = "z" * 250
    chunks = split_text(body, 100)
    assert all(len(chunk) <= 100 for chunk in chunks)
    assert "".join(chunks) == body


def test_split_text_keeps_every_character_at_scale():
    body = "\n".join(f"第 {index} 行内容" for index in range(500))
    chunks = split_text(body, 200)
    assert all(len(chunk) <= 200 for chunk in chunks)
    assert "".join(chunks).replace("\n", "") == body.replace("\n", "")
