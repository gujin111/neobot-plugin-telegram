"""桥接核心：绑定关系、路由判定、回环防护、文本成型（纯 stdlib）。

设计要点（README「桥接模型」一节有完整说明）：

* **绑定（binding）**：一条记录把 Telegram 会话与 QQ 会话对起来，
  两个方向可独立开关；
* **单向可信**：Telegram → QQ 用宿主 ``ctx.send_group/send_private`` 投递；
  QQ → Telegram 由 ``@plugin.message`` 处理器在收到消息时转发；
* **回环防护**：转出去的消息指纹进 TTL 集合，短时间内在另一端看到同样的
  内容就不再回传，避免两个平台互相刷屏。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

QQ_KINDS = {"group", "private"}
_DIRECTION_KEYS = ("tg_to_qq", "qq_to_tg")


@dataclass
class Binding:
    """一条 Telegram ⇄ QQ 绑定。"""

    tg_chat_id: str
    qq_kind: str = "group"
    qq_id: str = ""
    tg_title: str = ""
    qq_title: str = ""
    tg_to_qq: bool = True
    qq_to_tg: bool = True
    note: str = ""
    created_at: float = 0.0

    def __post_init__(self) -> None:
        self.tg_chat_id = str(self.tg_chat_id or "").strip()
        self.qq_kind = str(self.qq_kind or "group").strip() or "group"
        self.qq_id = str(self.qq_id or "").strip()
        if self.qq_kind not in QQ_KINDS:
            raise ValueError(f"qq_kind 只能是 group / private，收到 {self.qq_kind!r}")
        if not self.tg_chat_id:
            raise ValueError("tg_chat_id 不能为空")
        if not self.qq_id:
            raise ValueError("qq_id 不能为空")
        if not self.created_at:
            self.created_at = time.time()

    @property
    def key(self) -> str:
        return f"{self.tg_chat_id}->{self.qq_kind}:{self.qq_id}"

    def describe(self) -> str:
        tg = self.tg_title or self.tg_chat_id
        qq = self.qq_title or self.qq_id
        arrows = []
        if self.tg_to_qq:
            arrows.append("TG→QQ")
        if self.qq_to_tg:
            arrows.append("QQ→TG")
        return f"{tg} ⇄ {qq}（{'/'.join(arrows) or '无方向'}）"


class BindingStore:
    """绑定持久化：一个 JSON 文件，原子写（与宿主 user_qq_map.json 同风格）。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._bindings: list[Binding] = []

    # ── 读写 ──

    def load(self) -> list[Binding]:
        if not self.path.is_file():
            self._bindings = []
            return self._bindings
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            self._bindings = []
            return self._bindings
        items = raw.get("bindings") if isinstance(raw, dict) else raw
        loaded: list[Binding] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            try:
                loaded.append(Binding(**{k: v for k, v in item.items() if k in Binding.__dataclass_fields__}))
            except (TypeError, ValueError):
                continue
        self._bindings = loaded
        return loaded

    def save(self) -> None:
        payload = {
            "version": 1,
            "updated_at": time.time(),
            "bindings": [asdict(item) for item in self._bindings],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    # ── 增删查 ──

    @property
    def bindings(self) -> list[Binding]:
        return list(self._bindings)

    def add(self, binding: Binding) -> Binding:
        """新增或覆盖同 key 的绑定。"""
        self._bindings = [item for item in self._bindings if item.key != binding.key]
        self._bindings.append(binding)
        return binding

    def remove(self, *, tg_chat_id: str = "", qq_kind: str = "", qq_id: str = "") -> int:
        """按 TG 会话或 QQ 会话删除，返回删除条数。"""
        tg_chat_id = str(tg_chat_id or "").strip()
        qq_id = str(qq_id or "").strip()
        before = len(self._bindings)
        kept: list[Binding] = []
        for item in self._bindings:
            if tg_chat_id and item.tg_chat_id == tg_chat_id:
                continue
            if qq_id and item.qq_id == qq_id and (not qq_kind or item.qq_kind == qq_kind):
                continue
            kept.append(item)
        self._bindings = kept
        return before - len(kept)

    def by_tg(self, tg_chat_id: str) -> list[Binding]:
        target = str(tg_chat_id)
        return [item for item in self._bindings if item.tg_chat_id == target]

    def by_qq(self, qq_kind: str, qq_id: str) -> list[Binding]:
        kind = str(qq_kind)
        target = str(qq_id)
        return [item for item in self._bindings if item.qq_kind == kind and item.qq_id == target]

    def set_direction(self, *, tg_chat_id: str, tg_to_qq: bool | None = None, qq_to_tg: bool | None = None) -> int:
        changed = 0
        for item in self._bindings:
            if item.tg_chat_id != str(tg_chat_id):
                continue
            if tg_to_qq is not None and item.tg_to_qq != tg_to_qq:
                item.tg_to_qq = tg_to_qq
                changed += 1
            if qq_to_tg is not None and item.qq_to_tg != qq_to_tg:
                item.qq_to_tg = qq_to_tg
                changed += 1
        return changed


class LoopGuard:
    """回环防护：最近转发过的内容指纹，TTL 内不重复回传。"""

    def __init__(self, ttl: float = 90.0, capacity: int = 512) -> None:
        self.ttl = max(1.0, float(ttl))
        self.capacity = max(16, int(capacity))
        self._seen: OrderedDict[str, float] = OrderedDict()

    @staticmethod
    def fingerprint(text: str) -> str:
        normalized = re.sub(r"\s+", " ", str(text or "")).strip().casefold()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]

    def _evict(self, now: float) -> None:
        for key in list(self._seen):
            if now - self._seen[key] > self.ttl:
                del self._seen[key]
        while len(self._seen) > self.capacity:
            self._seen.popitem(last=False)

    def mark(self, text: str, *, now: float | None = None) -> None:
        stamp = time.monotonic() if now is None else now
        self._seen[self.fingerprint(text)] = stamp
        self._seen.move_to_end(self.fingerprint(text))
        self._evict(stamp)

    def is_echo(self, text: str, *, now: float | None = None) -> bool:
        stamp = time.monotonic() if now is None else now
        self._evict(stamp)
        key = self.fingerprint(text)
        seen_at = self._seen.get(key)
        return seen_at is not None and stamp - seen_at <= self.ttl


# ── 文本成型 ──


_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


def format_prefix(template: str, **fields: Any) -> str:
    """填充前缀模板；缺字段替换成空串（不抛 KeyError）。"""
    if not template:
        return ""

    def replace(match: re.Match[str]) -> str:
        return str(fields.get(match.group(1), "") or "")

    return _PLACEHOLDER_RE.sub(replace, template)


def render_tg_to_qq(event: Any, *, prefix: str = "", reply_text: str = "") -> str:
    """Telegram 事件 → 投递到 QQ 的文本（纯文本，OneBot 段由调用方包）。"""
    name = getattr(event, "user_name", "") or getattr(event, "user_id", "?")
    body = getattr(event, "caption", "") or getattr(event, "text", "")
    lines: list[str] = []
    if reply_text:
        lines.append(f"↩ {reply_text[:80]}")
    media = getattr(event, "media", []) or []
    if media:
        kinds = "、".join(sorted({str(item.kind) for item in media}))
        body = f"{body} [{kinds}]".strip() if body else f"[{kinds}]"
    lines.append(f"{name}: {body}" if body else f"{name}:")
    return format_prefix(prefix, name=name, chat=getattr(event, "chat_title", ""), id=getattr(event, "user_id", "")) + "\n".join(lines)


def render_qq_to_tg(event: dict[str, Any], *, prefix: str = "", strip_at: Iterable[str] = ()) -> str:
    """QQ 事件 → 发到 Telegram 的纯文本（at 段在 Telegram 上没有等价物）。"""
    body = qq_body_text(event, strip_at=strip_at)
    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
    name = str(sender.get("card") or sender.get("nickname") or event.get("user_id") or "?")
    group = str(event.get("group_id") or "")
    head = format_prefix(prefix, name=name, group=group, id=event.get("user_id"))
    return f"{head}{name}: {body}" if body else f"{head}{name}:"


def qq_body_text(event: dict[str, Any], *, strip_at: Iterable[str] = ()) -> str:
    """只取消息正文（不含前缀与发言人），用于命令判断等。"""
    segments = event.get("message") or []
    self_ids = {str(item) for item in strip_at if item}
    parts: list[str] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        kind = str(segment.get("type") or "")
        data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
        if kind == "text":
            parts.append(str(data.get("text") or ""))
        elif kind == "at":
            target = str(data.get("qq") or data.get("user_id") or "")
            if target in self_ids:
                continue
            name = str(data.get("name") or data.get("nickname") or target)
            parts.append(f"@{name}")
        elif kind == "image":
            parts.append("[图片]")
        elif kind in ("record", "audio"):
            parts.append("[语音]")
        elif kind == "video":
            parts.append("[视频]")
        elif kind == "file":
            parts.append("[文件]")
        elif kind in ("face", "emoji"):
            parts.append(f"[{data.get('name') or '表情'}]")
        elif kind == "reply":
            continue
        else:
            raw = str(data.get("text") or "")
            if raw:
                parts.append(raw)
    return "".join(parts).strip()


def conversation_of(event: dict[str, Any]) -> tuple[str, str]:
    """从 QQ 事件里取 (kind, id)。"""
    message_type = str(event.get("message_type") or "group")
    if message_type in ("private", "friend"):
        return "private", str(event.get("user_id") or "")
    return "group", str(event.get("group_id") or "")


def looks_like_command(text: str, prefixes: Iterable[str] = ("/",)) -> bool:
    stripped = str(text or "").lstrip()
    return any(stripped.startswith(prefix) for prefix in prefixes)


__all__ = [
    "Binding",
    "BindingStore",
    "LoopGuard",
    "conversation_of",
    "format_prefix",
    "looks_like_command",
    "qq_body_text",
    "render_qq_to_tg",
    "render_tg_to_qq",
]
