"""Telegram Update → 归一化事件（纯函数，不依赖 NeoBot）。

对应 Koishi 适配器的 ``utils.ts``：

* ``handleUpdate`` 的 update 分类与 ``session.type`` 映射；
* ``parseText`` 的「断点切分 + 区间取样」实体解析（bold/italic/underline/
  strikethrough/code/pre/spoiler/mention/text_link/text_mention）；
* ``media_group_id`` 相册聚合（窗口可配，默认 1.2s，与 Koishi 一致）；
* ``/cmd@bot`` 前缀剥离与「@ 的不是自己就丢弃」。

与 Koishi 版本的两处修正：

1. ``entities`` 为空数组时不再抛 ``TypeError``（Koishi 的 ``entities?.[0].type``
   只保护了 null/undefined）；
2. 相册聚合不阻塞轮询循环，窗口内的后续更新由缓冲器合并后再回调。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

#: update 字段名 → 归一化 kind
UPDATE_KINDS: dict[str, str] = {
    "message": "message",
    "edited_message": "message_updated",
    "channel_post": "message",
    "edited_channel_post": "message_updated",
    "business_message": "message",
    "edited_business_message": "message_updated",
    "callback_query": "callback_query",
    "chat_join_request": "chat_join_request",
    "my_chat_member": "my_chat_member",
    "chat_member": "chat_member",
    "inline_query": "inline_query",
    "chosen_inline_result": "chosen_inline_result",
    "poll": "poll",
    "poll_answer": "poll_answer",
    "pre_checkout_query": "pre_checkout_query",
    "shipping_query": "shipping_query",
    "message_reaction": "message_reaction",
    "message_reaction_count": "message_reaction_count",
}

#: Koishi 的 session.type 对照表（bridge 里用于决定怎么处理）
SESSION_TYPE = {
    "message": "message",
    "message_updated": "message-updated",
    "callback_query": "interaction/button",
    "chat_join_request": "guild-member-request",
    "my_chat_member": "my_chat_member",
}

#: Telegram entity → 归一化富文本类型（顺序即嵌套顺序，与 Koishi 一致）
_ENTITY_MAP: tuple[tuple[str, str], ...] = (
    ("bold", "b"),
    ("italic", "i"),
    ("underline", "u"),
    ("strikethrough", "s"),
    ("code", "code"),
    ("pre", "pre"),
    ("spoiler", "spoiler"),
)


@dataclass
class Span:
    """一段带样式的文本。"""

    type: str = "text"
    text: str = ""
    url: str = ""
    user_id: str = ""
    user_name: str = ""
    lang: str = ""


@dataclass
class MediaRef:
    """一条媒体引用。"""

    kind: str  # photo / video / audio / voice / animation / document / sticker
    file_id: str = ""
    file_unique_id: str = ""
    file_name: str = ""
    mime: str = ""
    file_size: int = 0
    width: int = 0
    height: int = 0
    duration: int = 0
    emoji: str = ""
    sticker_set: str = ""
    is_animated: bool = False


@dataclass
class IncomingEvent:
    """归一化后的一条入站事件。"""

    kind: str
    update_id: int = 0
    chat_id: str = ""
    chat_type: str = ""  # private / group / supergroup / channel
    chat_title: str = ""
    message_id: int = 0
    thread_id: int = 0
    user_id: str = ""
    user_name: str = ""
    user_is_bot: bool = False
    date: int = 0
    spans: list[Span] = field(default_factory=list)
    media: list[MediaRef] = field(default_factory=list)
    reply_to_message_id: int = 0
    reply_to_text: str = ""
    callback_id: str = ""
    callback_data: str = ""
    join_request_user_id: str = ""
    join_request_user_name: str = ""
    my_chat_member_state: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    media_group_id: str = ""
    media_group_message_ids: list[int] = field(default_factory=list)

    @property
    def text(self) -> str:
        """纯文本（样式丢弃，与 Koishi ``message.content`` 同口径）。"""
        parts: list[str] = []
        for span in self.spans:
            parts.append(span.text)
        for index, item in enumerate(self.media):
            if index == 0:
                parts.append(f" [{item.kind}]")
            else:
                parts.append(f"[{item.kind}]")
        return "".join(parts).strip()

    @property
    def caption(self) -> str:
        """用户可见的说明文字（媒体附带的 caption，不含媒体占位符）。"""
        return "".join(span.text for span in self.spans).strip()

    @property
    def is_bot_command(self) -> bool:
        raw = self.raw.get("message") or self.raw.get("channel_post") or {}
        return is_bot_command(raw)

    @property
    def session_type(self) -> str:
        if self.kind == "message" and self.is_bot_command:
            return "interaction/command"
        return SESSION_TYPE.get(self.kind, self.kind)


# ── update 分类 ──


def classify_update(update: dict[str, Any]) -> str:
    """取 ``update`` 里第一个非 ``update_id`` 的字段名并归一化。"""
    for key in update:
        if key == "update_id":
            continue
        return UPDATE_KINDS.get(key, key)
    return "unknown"


def update_subtype(update: dict[str, Any]) -> str:
    """内部事件名：``telegram/<字段名，下划线转连字符>``（与 Koishi 一致）。"""
    for key in update:
        if key == "update_id":
            continue
        return "telegram/" + key.replace("_", "-")
    return "telegram/unknown"


def is_bot_command(message: dict[str, Any]) -> bool:
    """首实体是 bot_command 才算斜线指令（空数组安全）。"""
    entities = message.get("entities") or []
    if not entities:
        return False
    first = entities[0] or {}
    return first.get("type") == "bot_command"


def strip_command_mention(text: str, bot_username: str = "") -> str | None:
    """``/cmd@someone args`` → ``/cmd args``；@ 的是别人时返回 None（丢弃）。"""
    if not text.startswith("/"):
        return text
    head, _, tail = text.partition(" ")
    name, _, mention = head[1:].partition("@")
    if mention and bot_username and mention != bot_username:
        return None
    return "/" + name + ((" " + tail) if tail else "")


# ── 实体解析（Koishi parseText 的移植） ──


def parse_text(text: str | None, entities: Sequence[dict[str, Any]] | None) -> list[Span]:
    """把 text + entities 切成带样式的片段。"""
    if not text:
        return []
    entities = list(entities or [])
    breakpoints: set[int] = set()
    for entity in entities:
        offset = int(entity.get("offset", 0) or 0)
        length = int(entity.get("length", 0) or 0)
        breakpoints.add(offset)
        breakpoints.add(offset + length)
    breakpoints.add(len(text))
    for index, char in enumerate(text):
        if char == "\n":
            breakpoints.add(index)
            breakpoints.add(index + 1)

    def attributes_at(position: int) -> tuple[list[str], str, dict[str, Any] | None]:
        attrs: list[str] = []
        url = ""
        user: dict[str, Any] | None = None
        for entity in entities:
            offset = int(entity.get("offset", 0) or 0)
            length = int(entity.get("length", 0) or 0)
            if offset <= position < offset + length:
                kind = str(entity.get("type") or "")
                attrs.append(kind)
                if kind == "text_link":
                    url = str(entity.get("url") or "")
                elif kind == "text_mention":
                    candidate = entity.get("user")
                    user = candidate if isinstance(candidate, dict) else None
        return attrs, url, user

    spans: list[Span] = []
    start = 0
    for point in sorted(breakpoints):
        if point <= start:
            continue
        content = text[start:point]
        if content == "\n":
            spans.append(Span(type="br", text="\n"))
            start = point
            continue
        attrs, url, user = attributes_at(start)
        chosen = "text"
        for entity_type, mapped in _ENTITY_MAP:
            if entity_type in attrs:
                chosen = mapped
        if url:
            spans.append(Span(type="a", text=content, url=url))
        elif user is not None:
            spans.append(
                Span(
                    type="at",
                    text=content,
                    user_id=str(user.get("id") or ""),
                    user_name=str(user.get("username") or user.get("first_name") or ""),
                )
            )
        elif "mention" in attrs:
            spans.append(Span(type="at", text=content, user_name=content.lstrip("@")))
        else:
            spans.append(Span(type=chosen, text=content))
        start = point
    return spans


def spans_to_html(spans: Iterable[Span]) -> str:
    """把片段拼成 Telegram HTML（用于诊断 / 二次发送）。"""
    try:
        from .tg_send import escape_html  # 以包形式加载时
    except ImportError:  # pragma: no cover - 作为顶层模块单测时
        from tg_send import escape_html

    parts: list[str] = []
    for span in spans:
        if span.type == "br":
            parts.append("\n")
        elif span.type in ("b", "i", "u", "s", "code", "pre", "spoiler"):
            tag = "tg-spoiler" if span.type == "spoiler" else span.type
            parts.append(f"<{tag}>{escape_html(span.text)}</{tag}>")
        elif span.type == "a":
            parts.append(f'<a href="{escape_html(span.url, quote=True)}">{escape_html(span.text)}</a>')
        elif span.type == "at":
            parts.append(escape_html(span.text or ("@" + span.user_name)))
        else:
            parts.append(escape_html(span.text))
    return "".join(parts)


# ── 消息解码 ──


def _pick_photo(photo: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """取尺寸最大的一张（Koishi 按 file_size 排序）。"""
    items = [item for item in photo if isinstance(item, dict)]
    if not items:
        return {}
    return sorted(items, key=lambda item: int(item.get("file_size") or 0), reverse=True)[0]


def decode_media(message: dict[str, Any]) -> list[MediaRef]:
    """按 Koishi 的顺序提取媒体（location 优先，其余取一个）。"""
    if message.get("photo"):
        chosen = _pick_photo(message["photo"])
        return [
            MediaRef(
                kind="photo",
                file_id=str(chosen.get("file_id") or ""),
                file_unique_id=str(chosen.get("file_unique_id") or ""),
                file_size=int(chosen.get("file_size") or 0),
                width=int(chosen.get("width") or 0),
                height=int(chosen.get("height") or 0),
            )
        ]
    for key, kind in (
        ("sticker", "sticker"),
        ("voice", "voice"),
        ("animation", "animation"),
        ("video", "video"),
        ("document", "document"),
        ("audio", "audio"),
    ):
        payload = message.get(key)
        if isinstance(payload, dict):
            return [
                MediaRef(
                    kind=kind,
                    file_id=str(payload.get("file_id") or ""),
                    file_unique_id=str(payload.get("file_unique_id") or ""),
                    file_name=str(payload.get("file_name") or ""),
                    mime=str(payload.get("mime_type") or ""),
                    file_size=int(payload.get("file_size") or 0),
                    width=int(payload.get("width") or 0),
                    height=int(payload.get("height") or 0),
                    duration=int(payload.get("duration") or 0),
                    emoji=str(payload.get("emoji") or ""),
                    sticker_set=str(payload.get("set_name") or ""),
                    is_animated=bool(payload.get("is_animated")),
                )
            ]
    if isinstance(message.get("location"), dict):
        location = message["location"]
        return [
            MediaRef(
                kind="location",
                file_name=f"{location.get('latitude')},{location.get('longitude')}",
            )
        ]
    return []


def decode_message(message: dict[str, Any], *, update: dict[str, Any] | None = None) -> IncomingEvent:
    """把一条 Telegram message 变成 :class:`IncomingEvent`。"""
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    text = message.get("text") or message.get("caption")
    entities = [*(message.get("entities") or []), *(message.get("caption_entities") or [])]
    chat_type = str(chat.get("type") or "")
    title = str(chat.get("title") or "")
    if not title:
        title = " ".join(
            part for part in (str(sender.get("first_name") or ""), str(sender.get("last_name") or "")) if part
        ).strip()
    reply = message.get("reply_to_message") or {}
    event = IncomingEvent(
        kind="message",
        update_id=int((update or {}).get("update_id") or 0),
        chat_id=str(chat.get("id") or ""),
        chat_type=chat_type,
        chat_title=title,
        message_id=int(message.get("message_id") or 0),
        thread_id=int(message.get("message_thread_id") or 0),
        user_id=str(sender.get("id") or ""),
        user_name=str(sender.get("username") or sender.get("first_name") or ""),
        user_is_bot=bool(sender.get("is_bot")),
        date=int(message.get("date") or 0),
        spans=parse_text(text, entities),
        media=decode_media(message),
        media_group_id=str(message.get("media_group_id") or ""),
        raw=dict(update or {"message": message}),
    )
    if reply:
        event.reply_to_message_id = int(reply.get("message_id") or 0)
        reply_spans = parse_text(reply.get("text") or reply.get("caption"), reply.get("entities") or [])
        event.reply_to_text = "".join(span.text for span in reply_spans)[:200]
    return event


def decode_update(update: dict[str, Any], *, bot_username: str = "") -> IncomingEvent | None:
    """把任意 update 归一化成事件；不认识的类型返回 None。"""
    kind = classify_update(update)
    if kind in ("message", "message_updated"):
        raw = (
            update.get("message")
            or update.get("edited_message")
            or update.get("channel_post")
            or update.get("edited_channel_post")
            or update.get("business_message")
            or update.get("edited_business_message")
            or {}
        )
        event = decode_message(raw, update=update)
        event.kind = kind
        if kind == "message" and is_bot_command(raw):
            stripped = strip_command_mention(event.text, bot_username)
            if stripped is None:
                return None  # /cmd@别人，丢弃
            if stripped != event.text:
                event.spans = parse_text(stripped, [])
        return event

    if kind == "callback_query":
        query = update.get("callback_query") or {}
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        sender = query.get("from") or {}
        return IncomingEvent(
            kind=kind,
            update_id=int(update.get("update_id") or 0),
            chat_id=str(chat.get("id") or ""),
            chat_type=str(chat.get("type") or ""),
            chat_title=str(chat.get("title") or ""),
            message_id=int(message.get("message_id") or 0),
            thread_id=int(message.get("message_thread_id") or 0),
            user_id=str(sender.get("id") or ""),
            user_name=str(sender.get("username") or sender.get("first_name") or ""),
            user_is_bot=bool(sender.get("is_bot")),
            date=int(query.get("date") or 0),
            callback_id=str(query.get("id") or ""),
            callback_data=str(query.get("data") or ""),
            spans=parse_text(str(query.get("data") or ""), []),
            raw=update,
        )

    if kind == "chat_join_request":
        request = update.get("chat_join_request") or {}
        chat = request.get("chat") or {}
        sender = request.get("from") or {}
        return IncomingEvent(
            kind=kind,
            update_id=int(update.get("update_id") or 0),
            chat_id=str(chat.get("id") or ""),
            chat_type=str(chat.get("type") or ""),
            chat_title=str(chat.get("title") or ""),
            user_id=str(sender.get("id") or ""),
            user_name=str(sender.get("username") or sender.get("first_name") or ""),
            user_is_bot=bool(sender.get("is_bot")),
            date=int(request.get("date") or 0),
            join_request_user_id=str(sender.get("id") or ""),
            join_request_user_name=str(sender.get("username") or sender.get("first_name") or ""),
            raw=update,
        )

    if kind == "my_chat_member":
        payload = update.get("my_chat_member") or {}
        chat = payload.get("chat") or {}
        sender = payload.get("from") or {}
        old = (payload.get("old_chat_member") or {}).get("status")
        new = (payload.get("new_chat_member") or {}).get("status")
        state = ""
        if new == "left" or new == "kicked":
            state = "group-deleted"
        elif new in ("member", "administrator", "creator") and old in ("left", "kicked", None):
            state = "group-added"
        return IncomingEvent(
            kind=kind,
            update_id=int(update.get("update_id") or 0),
            chat_id=str(chat.get("id") or ""),
            chat_type=str(chat.get("type") or ""),
            chat_title=str(chat.get("title") or ""),
            user_id=str(sender.get("id") or ""),
            user_name=str(sender.get("username") or sender.get("first_name") or ""),
            user_is_bot=bool(sender.get("is_bot")),
            date=int(payload.get("date") or 0),
            my_chat_member_state=state,
            raw=update,
        )

    return None


# ── 相册聚合 ──


@dataclass
class _GroupState:
    first_seen: float
    events: list[IncomingEvent] = field(default_factory=list)


class MediaGroupBuffer:
    """按 ``media_group_id`` 聚合相册。

    Koishi 用「每条更新 sleep 1.2s，最后一条负责合并」的写法，会把派发延迟 1.2s
    且无法处理窗口外的迟到消息。这里改成显式缓冲：``push()`` 返回填满窗口后应派发的
    合并事件（``None`` 表示已并入缓冲）；``flush_due(now)`` 供定时器兜底。

    参数：
        window: 合并窗口（秒），默认 1.2 与 Koishi 一致。
        clock:  注入时钟，便于测试。
    """

    def __init__(self, window: float = 1.2, clock: Any = None) -> None:
        self.window = max(0.0, float(window))
        self._clock = clock or _monotonic
        self._groups: dict[str, _GroupState] = {}

    def has_pending(self) -> bool:
        return bool(self._groups)

    def push(self, event: IncomingEvent, *, now: float | None = None) -> IncomingEvent | None:
        """并入相册；返回 None 表示等窗口到期，返回事件表示这是最后一条（合并完成）。"""
        group_id = event.media_group_id
        if not group_id:
            return event
        stamp = self._clock() if now is None else now
        state = self._groups.get(group_id)
        if state is None or stamp - state.first_seen > self.window:
            state = _GroupState(first_seen=stamp)
            self._groups[group_id] = state
        state.events.append(event)
        # 窗口内的第一条不立即派发；窗口到期由 flush_due() 兜底
        return None

    def flush_due(self, *, now: float | None = None) -> list[IncomingEvent]:
        """返回所有已过窗口的合并事件，并从缓冲里删除。"""
        stamp = self._clock() if now is None else now
        ready: list[IncomingEvent] = []
        for group_id in list(self._groups):
            state = self._groups[group_id]
            if stamp - state.first_seen < self.window:
                continue
            del self._groups[group_id]
            ready.append(merge_media_group(state.events))
        return ready

    def flush_all(self) -> list[IncomingEvent]:
        """强制取走全部（关停时用）。"""
        ready = [merge_media_group(state.events) for state in self._groups.values()]
        self._groups.clear()
        return ready


def merge_media_group(events: Sequence[IncomingEvent]) -> IncomingEvent:
    """把同一相册的多条事件合并成一条（按 message_id 排序，媒体拼接）。"""
    ordered = sorted(events, key=lambda item: item.message_id)
    head = ordered[0]
    merged = IncomingEvent(
        kind=head.kind,
        update_id=head.update_id,
        chat_id=head.chat_id,
        chat_type=head.chat_type,
        chat_title=head.chat_title,
        message_id=head.message_id,
        thread_id=head.thread_id,
        user_id=head.user_id,
        user_name=head.user_name,
        user_is_bot=head.user_is_bot,
        date=head.date,
        reply_to_message_id=head.reply_to_message_id,
        reply_to_text=head.reply_to_text,
        media_group_id=head.media_group_id,
        media_group_message_ids=[item.message_id for item in ordered],
        raw=head.raw,
    )
    for item in ordered:
        if item.caption and item.caption not in merged.caption:
            merged.spans.extend(item.spans)
        merged.media.extend(item.media)
    return merged


def _monotonic() -> float:
    import time

    return time.monotonic()
