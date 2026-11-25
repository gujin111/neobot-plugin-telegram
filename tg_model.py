"""Update 解码：把 Telegram 的原始更新拍平成事件对象。

只关心「谁、在哪个会话、说了什么、带了什么媒体」——实体、相册、命令前缀都不需要，
因为我们把纯文本交给模型，不做富文本还原、也不在 Telegram 侧处理命令。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Telegram 消息里可能出现、我们只当作占位符上报的媒体字段
_MEDIA_KEYS = (
    ("photo", "photo"),
    ("video", "video"),
    ("video_note", "video_note"),
    ("animation", "animation"),
    ("voice", "voice"),
    ("audio", "audio"),
    ("document", "document"),
    ("sticker", "sticker"),
    ("contact", "contact"),
    ("location", "location"),
    ("venue", "venue"),
    ("poll", "poll"),
    ("dice", "dice"),
)

#: update 里除 update_id / message 之外的常见类型（日志用）
_UPDATE_TYPES = (
    "edited_message",
    "channel_post",
    "edited_channel_post",
    "callback_query",
    "inline_query",
    "chosen_inline_result",
    "shipping_query",
    "pre_checkout_query",
    "poll",
    "poll_answer",
    "my_chat_member",
    "chat_member",
    "chat_join_request",
)


@dataclass
class IncomingEvent:
    """一条入站消息（或按钮回调）的扁平表示。"""

    kind: str = "message"
    chat_id: str = ""
    chat_type: str = ""
    chat_title: str = ""
    message_id: int = 0
    user_id: str = ""
    user_name: str = ""
    user_is_bot: bool = False
    text: str = ""
    caption: str = ""
    media: list[str] = field(default_factory=list)
    callback_id: str = ""
    callback_data: str = ""

    @property
    def body(self) -> str:
        """正文：优先 text，其次 caption；没有正文时用媒体占位。"""
        content = (self.text or self.caption or "").strip()
        if content:
            return content
        if self.media:
            return "[" + "、".join(self.media) + "]"
        return ""


def media_kinds(message: dict[str, Any]) -> list[str]:
    """消息里带的媒体种类（顺序固定，便于测试）。"""
    return [name for key, name in _MEDIA_KEYS if message.get(key) is not None]


def display_name(user: dict[str, Any]) -> str:
    """优先 username，其次昵称，最后 id。"""
    for key in ("username", "first_name", "title"):
        value = str(user.get(key) or "").strip()
        if value:
            return value
    return str(user.get("id") or "")


def _chat_title(chat: dict[str, Any]) -> str:
    title = str(chat.get("title") or "").strip()
    if title:
        return title
    parts = [str(chat.get("first_name") or ""), str(chat.get("last_name") or "")]
    joined = " ".join(part for part in parts if part).strip()
    return joined or str(chat.get("username") or "")


def decode_message(message: dict[str, Any]) -> IncomingEvent:
    """把一条 Telegram 消息拍平成 IncomingEvent。"""
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    return IncomingEvent(
        kind="message",
        chat_id=str(chat.get("id") or ""),
        chat_type=str(chat.get("type") or ""),
        chat_title=_chat_title(chat),
        message_id=int(message.get("message_id") or 0),
        user_id=str(sender.get("id") or ""),
        user_name=display_name(sender),
        user_is_bot=bool(sender.get("is_bot")),
        text=str(message.get("text") or ""),
        caption=str(message.get("caption") or ""),
        media=media_kinds(message),
    )


def decode_callback(query: dict[str, Any]) -> IncomingEvent:
    """按钮回调：只需要回一个 answerCallbackQuery。"""
    sender = query.get("from") or {}
    message = query.get("message") or {}
    chat = message.get("chat") or {}
    return IncomingEvent(
        kind="callback_query",
        chat_id=str(chat.get("id") or ""),
        chat_type=str(chat.get("type") or ""),
        message_id=int(message.get("message_id") or 0),
        user_id=str(sender.get("id") or ""),
        user_name=display_name(sender),
        user_is_bot=bool(sender.get("is_bot")),
        callback_id=str(query.get("id") or ""),
        callback_data=str(query.get("data") or ""),
    )


def decode_update(update: dict[str, Any]) -> IncomingEvent | None:
    """解出一条可用事件；与我们无关的更新返回 None。"""
    if not isinstance(update, dict):
        return None
    if isinstance(update.get("message"), dict):
        return decode_message(update["message"])
    if isinstance(update.get("callback_query"), dict):
        return decode_callback(update["callback_query"])
    return None


def update_subtype(update: dict[str, Any]) -> str:
    """给日志用的更新类型名。"""
    if not isinstance(update, dict):
        return "unknown"
    for key in _UPDATE_TYPES:
        if update.get(key) is not None:
            return key
    return "message" if "message" in update else "unknown"
