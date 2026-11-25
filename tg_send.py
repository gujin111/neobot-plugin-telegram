"""出站渲染：NeoBot 消息段 → Telegram 发送计划（纯函数，不依赖 NeoBot）。

对应 Koishi 适配器的 ``message.ts``（``TelegramMessageEncoder``）：

* 文本元素白名单透传（b/strong/i/em/u/ins/s/del/a）、``spl`` → ``<tg-spoiler>``、
  ``code`` / ``code-block`` → ``<code>`` / ``<pre><code class=language-x>``、
  ``at`` → ``<a href="tg://user?id=...">``、``quote`` → ``reply_to_message_id``、
  ``button`` / ``button-group`` → 每行最多 5 个的 Inline Keyboard；
* 媒体映射表（audio/voice→sendAudio、video→sendVideo、animation→sendAnimation、
  image/photo→sendPhoto、document→sendDocument、其它→sendDocument）；
* 多资源走 sendMediaGroup（caption 挂第一个），有按钮时再补一条带键盘的消息。

相对 Koishi 版本补齐的两点（Koishi 侧缺失）：

1. **超长文本分段**：Telegram 上限 4096（caption 1024），Koishi 直接报错；这里按
   标签安全的切分算法拆成多条；
2. **caption 超限**：媒体照发、文字另发，不截断用户内容。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024
#: Inline Keyboard 每行最多按钮数（与 Koishi ``lastRow`` 一致）
BUTTONS_PER_ROW = 5

_TAG_RE = re.compile(r"</?([a-zA-Z][\w-]*)(?:\s[^>]*)?>")
_VOID_TAGS = {"br"}


def escape_html(text: object, *, quote: bool = False) -> str:
    """Telegram HTML 只需要转义 ``< > &``；属性值再加引号。"""
    value = "" if text is None else str(text)
    value = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if quote:
        value = value.replace('"', "&quot;")
    return value


# ── 消息段归一化（兼容 dict 与 MessageChain 对象） ──


def segment_type(segment: Any) -> str:
    value = segment.get("type") if isinstance(segment, dict) else getattr(segment, "type", None)
    value = getattr(value, "value", value)
    return str(value or "")


def segment_data(segment: Any) -> dict[str, Any]:
    data = segment.get("data") if isinstance(segment, dict) else getattr(segment, "data", None)
    return data if isinstance(data, dict) else {}


def segment_children(segment: Any) -> list[Any]:
    children = segment.get("children") if isinstance(segment, dict) else getattr(segment, "children", None)
    return list(children) if children else []


def children_text(segment: Any) -> str:
    """递归取子节点里的纯文本（按钮标签用）。"""
    parts: list[str] = []
    for child in segment_children(segment):
        kind = segment_type(child)
        data = segment_data(child)
        if kind in ("text", ""):
            value = data.get("text")
            if value is None:
                value = child if isinstance(child, str) else ""
            parts.append(str(value))
        else:
            nested = children_text(child)
            if nested:
                parts.append(nested)
    return "".join(parts)


# ── 渲染 ──


@dataclass
class OutgoingMedia:
    kind: str
    source: str = ""  # 本地路径 / http(s) URL / base64 / data URL
    data: bytes | None = None
    filename: str = ""
    mime: str = ""
    spoiler: bool = False

    @property
    def is_upload(self) -> bool:
        return self.data is not None or bool(self.source) and not self.source.startswith(("http://", "https://"))


@dataclass
class RenderedMessage:
    """一条待发送消息的渲染结果。"""

    text: str = ""  # Telegram HTML
    plain: str = ""
    media: list[OutgoingMedia] = field(default_factory=list)
    reply_to_message_id: int = 0
    rows: list[list[dict[str, Any]]] = field(default_factory=list)
    link_preview: bool = False

    @property
    def has_content(self) -> bool:
        return bool(self.text.strip() or self.media)


def _wrap(span_type: str, html: str, attrs: dict[str, Any] | None = None) -> str:
    attrs = attrs or {}
    if span_type == "a" and attrs.get("href"):
        return f'<a href="{escape_html(attrs["href"], quote=True)}">{html}</a>'
    if span_type == "at":
        user_id = attrs.get("id") or attrs.get("qq")
        if user_id and str(user_id).isdigit():
            return f'<a href="tg://user?id={escape_html(user_id)}">{html}</a>'
        return html
    if span_type == "spoiler":
        return f"<tg-spoiler>{html}</tg-spoiler>"
    if span_type == "strong":
        return f"<b>{html}</b>"
    if span_type == "em":
        return f"<i>{html}</i>"
    if span_type == "ins":
        return f"<u>{html}</u>"
    if span_type == "del":
        return f"<s>{html}</s>"
    if span_type in ("b", "i", "u", "s", "code", "pre"):
        return f"<{span_type}>{html}</{span_type}>"
    return html


def render_segments(
    segments: Iterable[Any] | None,
    *,
    reply_to_message_id: int = 0,
    link_preview: bool = False,
) -> RenderedMessage:
    """把 NeoBot / OneBot 风格消息段渲染成 :class:`RenderedMessage`。"""
    result = RenderedMessage(reply_to_message_id=int(reply_to_message_id or 0), link_preview=link_preview)
    html_parts: list[str] = []
    plain_parts: list[str] = []

    def add_text(html: str, plain: str) -> None:
        html_parts.append(html)
        plain_parts.append(plain)

    for segment in segments or []:
        kind = segment_type(segment)
        data = segment_data(segment)
        if kind == "text":
            value = str(data.get("text") or "")
            add_text(escape_html(value), value)
        elif kind in ("br", "newline"):
            add_text("\n", "\n")
        elif kind == "at":
            name = str(data.get("name") or data.get("nickname") or "")
            target = data.get("qq") or data.get("user_id") or data.get("id") or ""
            label = escape_html(("@" + name) if name else f"@{target}")
            html_parts.append(_wrap("at", label, {"id": target}))
            plain_parts.append(f"@{name or target}")
        elif kind == "reply":
            result.reply_to_message_id = int(data.get("id") or data.get("message_id") or 0)
        elif kind == "image":
            source = str(data.get("file") or data.get("url") or data.get("src") or "")
            payload = data.get("data")
            result.media.append(
                OutgoingMedia(
                    kind="photo",
                    source=source,
                    data=payload if isinstance(payload, bytes) else None,
                    filename=str(data.get("filename") or ""),
                    mime=str(data.get("mime") or ""),
                    spoiler=bool(data.get("spoiler")),
                )
            )
            plain_parts.append("[图片]")
        elif kind in ("record", "audio", "voice"):
            result.media.append(
                OutgoingMedia(
                    kind="voice" if kind == "record" else "audio",
                    source=str(data.get("file") or data.get("url") or ""),
                    data=data.get("data") if isinstance(data.get("data"), bytes) else None,
                    filename=str(data.get("filename") or ""),
                    mime=str(data.get("mime") or ""),
                )
            )
            plain_parts.append("[语音]" if kind == "record" else "[音频]")
        elif kind in ("video", "file", "document"):
            result.media.append(
                OutgoingMedia(
                    kind="video" if kind == "video" else "document",
                    source=str(data.get("file") or data.get("url") or ""),
                    data=data.get("data") if isinstance(data.get("data"), bytes) else None,
                    filename=str(data.get("filename") or data.get("name") or ""),
                    mime=str(data.get("mime") or ""),
                )
            )
            plain_parts.append("[视频]" if kind == "video" else "[文件]")
        elif kind in ("face", "emoji", "sharp", "market_face"):
            label = str(data.get("name") or data.get("id") or "")
            add_text(escape_html(f"[{label}]"), f"[{label}]")
        elif kind in ("json", "xml", "markdown", "card"):
            raw = str(data.get("data") or data.get("text") or "")
            if raw:
                add_text(escape_html(raw[:TEXT_LIMIT]), raw[:TEXT_LIMIT])
        elif kind == "button":
            label = children_text(segment) or str(data.get("text") or "")
            result.rows.append([_decode_button(data, label)])
        elif kind == "button-group":
            children = [seg for seg in segment_children(segment) if segment_type(seg) == "button"]
            row: list[dict[str, Any]] = []
            for seg in children:
                seg_data = segment_data(seg)
                row.append(_decode_button(seg_data, children_text(seg) or str(seg_data.get("text") or "")))
            if row:
                result.rows.append(row)
        else:
            children = segment_children(segment)
            if children:
                nested = render_segments(children)
                html_parts.append(nested.text)
                plain_parts.append(nested.plain)
                result.media.extend(nested.media)

    result.text = "".join(html_parts).strip("\n")
    result.plain = "".join(plain_parts).strip("\n")
    result.rows = _normalize_rows(result.rows)
    return result


def _decode_button(data: dict[str, Any], label: str) -> dict[str, Any]:
    """按钮三态（与 Koishi ``decodeButton`` 一致）。"""
    kind = str(data.get("type") or "").lower()
    text = str(label or data.get("text") or "按钮")[:64]
    if kind == "link":
        return {"text": text, "url": str(data.get("href") or data.get("url") or "")}
    if kind == "input":
        return {"text": text, "switch_inline_query_current_chat": str(data.get("text") or "")}
    return {"text": text, "callback_data": str(data.get("id") or data.get("callback_data") or text)[:64]}


def _normalize_rows(rows: Sequence[Sequence[dict[str, Any]]]) -> list[list[dict[str, Any]]]:
    """每行最多 5 个按钮，超出的折到下一行（与 Koishi ``lastRow`` 一致）。"""
    normalized: list[list[dict[str, Any]]] = []
    for row in rows:
        current: list[dict[str, Any]] = []
        for button in row:
            if len(current) >= BUTTONS_PER_ROW:
                normalized.append(current)
                current = []
            current.append(button)
        if current:
            normalized.append(current)
    return normalized


# ── 长度与切分 ──


def split_html(html: str, limit: int = TEXT_LIMIT) -> list[str]:
    """按 Telegram 长度上限切分，且不破坏标签。

    每段自身标签闭合（跨段时把还开着的标签在段尾补齐、在下一段段首重开），
    这样即使某些客户端按段独立解析也不会漏格式。计数按 HTML 源码长度，比
    Telegram 的「解析后字符数」更保守，属于安全方向。
    """
    if limit <= 0 or len(html) <= limit:
        return [html]
    chunks: list[str] = []
    stack: list[str] = []

    def close_suffix() -> str:
        return "".join(f"</{tag}>" for tag in reversed(stack))

    def flush() -> None:
        nonlocal buffer
        if buffer.strip():
            chunks.append(buffer + close_suffix())
        buffer = "".join(f"<{tag}>" for tag in stack)

    def add(run: str) -> None:
        nonlocal buffer
        while run:
            room = limit - len(buffer)
            if room <= 0:
                flush()
                room = limit - len(buffer)
            buffer += run[:room]
            run = run[room:]

    buffer = ""
    position = 0
    for match in _TAG_RE.finditer(html):
        add(html[position : match.start()])
        position = match.end()
        raw = match.group(0)
        tag = match.group(1).lower()
        if len(buffer) + len(raw) > limit:
            flush()
        buffer += raw
        if raw.startswith("</"):
            if tag in stack:
                stack.remove(tag)
        elif tag not in _VOID_TAGS:
            stack.append(tag)
    add(html[position:])
    if buffer.strip():
        chunks.append(buffer + close_suffix())
    return [chunk for chunk in chunks if chunk.strip()]


def split_plain(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    if limit <= 0 or len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = window.rfind("\n")
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


# ── 媒体方法映射 ──

_MIME_METHODS: tuple[tuple[str, str, str], ...] = (
    ("image/gif", "sendAnimation", "animation"),
    ("image/", "sendPhoto", "photo"),
    ("video/", "sendVideo", "video"),
    ("audio/ogg", "sendVoice", "voice"),
    ("audio/", "sendAudio", "audio"),
    ("application/", "sendDocument", "document"),
    ("text/", "sendDocument", "document"),
)

_KIND_METHODS: dict[str, tuple[str, str]] = {
    "photo": ("sendPhoto", "photo"),
    "image": ("sendPhoto", "photo"),
    "video": ("sendVideo", "video"),
    "animation": ("sendAnimation", "animation"),
    "audio": ("sendAudio", "audio"),
    "voice": ("sendVoice", "voice"),
    "document": ("sendDocument", "document"),
    "file": ("sendDocument", "document"),
}


def media_method(kind: str = "", filename: str = "", mime: str = "") -> tuple[str, str]:
    """返回 (方法名, 字段名)。文件名以 .gif 结尾按 animation（Koishi 同款判断）。"""
    if filename and filename.lower().endswith(".gif"):
        return "sendAnimation", "animation"
    if mime:
        lowered = mime.lower()
        for prefix, method, field_name in _MIME_METHODS:
            if lowered.startswith(prefix):
                return method, field_name
    return _KIND_METHODS.get(kind, ("sendDocument", "document"))


# ── 发送计划 ──


@dataclass
class SendAction:
    """一次 Bot API 调用：方法 + 参数 + 待上传文件。"""

    method: str
    params: dict[str, Any] = field(default_factory=dict)
    files: list[tuple[str, tuple[str, bytes, str]]] = field(default_factory=list)
    note: str = ""


@dataclass
class PlanOptions:
    parse_mode: str = "html"
    link_preview: bool = True
    chunk_size: int = 3500
    thread_id: int = 0


def build_actions(
    chat_id: int | str,
    rendered: RenderedMessage,
    *,
    options: PlanOptions | None = None,
    reply_to_message_id: int | None = None,
) -> list[SendAction]:
    """把渲染结果变成一串可直接执行的发送动作。

    分段策略与 Koishi ``flush()`` 对齐：多资源 → sendMediaGroup（有按钮再补一条
    文本消息）；单资源 → 对应 sendXxx；无资源 → sendMessage。
    """
    options = options or PlanOptions()
    reply_id = rendered.reply_to_message_id or int(reply_to_message_id or 0)
    thread = int(options.thread_id or 0)
    actions: list[SendAction] = []

    def base_extra() -> dict[str, Any]:
        extra: dict[str, Any] = {}
        if thread:
            extra["message_thread_id"] = thread
        return extra

    keyboard = {"inline_keyboard": rendered.rows} if rendered.rows else None
    media = rendered.media
    text = rendered.text
    caption = text if len(text) <= CAPTION_LIMIT else ""
    overflow_text = text if not caption and text else ""

    if media:
        if len(media) > 1:
            group: list[dict[str, Any]] = []
            files: list[tuple[str, tuple[str, bytes, str]]] = []
            for index, item in enumerate(media):
                attach = f"file{index}"
                entry: dict[str, Any] = {"type": item.kind if item.kind != "voice" else "audio", "media": f"attach://{attach}"}
                if item.spoiler:
                    entry["has_spoiler"] = True
                if index == 0 and caption:
                    entry["caption"] = caption
                    entry["parse_mode"] = options.parse_mode
                group.append(entry)
                if item.data is not None:
                    files.append((attach, (item.filename or f"{attach}.bin", item.data, item.mime or "application/octet-stream")))
                else:
                    entry["media"] = item.source or ""
            params: dict[str, Any] = {"chat_id": chat_id, "media": json.dumps(group, ensure_ascii=False), **base_extra()}
            if reply_id:
                params["reply_to_message_id"] = reply_id
            actions.append(SendAction("sendMediaGroup", params, files, note="相册"))
            if rendered.rows and caption:
                text_params: dict[str, Any] = {
                    "chat_id": chat_id,
                    "text": caption,
                    "parse_mode": options.parse_mode,
                    "disable_web_page_preview": not options.link_preview,
                    "reply_markup": keyboard,
                    **base_extra(),
                }
                actions.append(SendAction("sendMessage", text_params, note="相册按钮"))
                overflow_text = ""
        else:
            item = media[0]
            method, field_name = media_method(item.kind, item.filename, item.mime)
            params = {
                "chat_id": chat_id,
                "caption": caption,
                "parse_mode": options.parse_mode,
                "has_spoiler": bool(item.spoiler),
                **base_extra(),
            }
            if reply_id:
                params["reply_to_message_id"] = reply_id
            if item.data is not None:
                attach = "file0"
                params[field_name] = f"attach://{attach}"
                files = [(attach, (item.filename or f"file0{_suffix(item)}", item.data, item.mime or "application/octet-stream"))]
            else:
                params[field_name] = item.source or ""
                files = []
            if keyboard:
                params["reply_markup"] = keyboard
            actions.append(SendAction(method, params, files, note=item.kind))
        if overflow_text:
            for chunk in split_html(overflow_text, options.chunk_size):
                actions.append(
                    SendAction(
                        "sendMessage",
                        {
                            "chat_id": chat_id,
                            "text": chunk,
                            "parse_mode": options.parse_mode,
                            "disable_web_page_preview": not options.link_preview,
                            **base_extra(),
                        },
                        note="caption 超限补发",
                    )
                )
        return actions

    chunks = split_html(text, options.chunk_size) if text else []
    if not chunks and rendered.rows:
        chunks = [""]  # 纯按钮消息也要发（Koishi 会漏发）
    for index, chunk in enumerate(chunks):
        params = {
            "chat_id": chat_id,
            "text": chunk or "…",
            "parse_mode": options.parse_mode,
            "disable_web_page_preview": not options.link_preview,
            **base_extra(),
        }
        if reply_id and index == 0:
            params["reply_to_message_id"] = reply_id
        if keyboard and index == len(chunks) - 1:
            params["reply_markup"] = keyboard
        actions.append(SendAction("sendMessage", params))
    return actions


def _suffix(item: OutgoingMedia) -> str:
    if item.filename and "." in item.filename:
        return item.filename[item.filename.rfind(".") :]
    if item.kind == "photo":
        return ".jpg"
    if item.kind in ("voice", "audio"):
        return ".ogg"
    if item.kind == "video":
        return ".mp4"
    return ".bin"
