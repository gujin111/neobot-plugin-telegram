"""出站文本：转义与分段。

我们只发纯文本（AI 的回复），所以不需要标签平衡——先把文本整体转义，
再按段落/换行/空格切段即可。
"""

from __future__ import annotations

TEXT_LIMIT = 4096


def escape_html(text: object) -> str:
    """转义 HTML 特殊字符（配合 parse_mode=HTML 使用）。"""
    return str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def split_text(text: str, limit: int = 3500) -> list[str]:
    """切成每段不超过 limit 的块，优先在换行处断开；空串返回空列表。"""
    body = text or ""
    if not body.strip():
        return []
    if len(body) <= limit:
        return [body]

    chunks: list[str] = []
    rest = body
    while len(rest) > limit:
        window = rest[:limit]
        cut = max(window.rfind("\n"), window.rfind(" "))
        if cut < limit // 2:  # 找不到合适断点就硬切
            cut = limit
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip("\n")
    if rest.strip():
        chunks.append(rest)
    return [chunk for chunk in chunks if chunk.strip()]
