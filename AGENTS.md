# AGENTS.md — telegram 插件

给开发助手的最小上下文：NeoBot 2.x 的第三方插件，把 Telegram 做成 **NeoBot 的独立对话前端**
（TG 用户直接和 AI 聊天）。**完全不涉及 QQ**——2.1.0 起连模型调用都不经过 QQ。

## 边界（不要越界）

- **不修改 NeoBot 本体**，只用 `neobot_modloader` 公开 API。
- **AI 只借宿主的模型服务**：调用 `ctx.services.get("provider").chat(messages)`，
  接口是 `async chat(messages: list[dict], tools=None) -> dict`。**不要**走
  `reply_orchestrator.start_background_reply`——那条路以 QQ 会话为单位，会把回复发到 QQ。
- **不要试图注册新通道**：宿主 `adapter` 是 `__host__` 服务，插件覆盖不了。
- Telegram 的网络调用只走 `tg_api.TelegramAPI`（代理、重试、429 都在那里）。

## 结构

| 文件 | 职责 |
| --- | --- |
| `__init__.py` | Plugin、配置、生命周期、命令（只两条）、入站分发、AI 直答编排 |
| `tg_api.py` | Bot API 客户端：endpoint/代理/重试/429/长轮询/文件（**零宿主依赖**） |
| `tg_model.py` | Update 解码：实体、媒体、相册聚合（**零宿主依赖**） |
| `tg_send.py` | 出站渲染：HTML 转义、标签安全分段、媒体映射、按钮（**零宿主依赖**） |
| `tg_proxy.py` | 内置 SOCKS5 + 本地 CONNECT 网关 + `auto` 探测（**只依赖标准库**） |

`tg_*` 四个模块不 import 任何 NeoBot 模块；只有 `__init__.py` 依赖宿主，导入写成
「包导入 / 顶层导入」双分支（pytest 收集会把插件根目录当模块导入一次）。

## 必须遵守的行为约定

1. **同一个 TG 会话串行**：`reply_with_ai` 用每会话 `asyncio.Lock`，别并发打模型。
2. **上下文自己管**：`_history[chat_id]` 保留 `history_limit * 2` 条（user+assistant 成对），
   超了从头裁；`history_limit=0` 表示不带历史。
3. **等待要有上限**：`asyncio.wait_for(provider.chat(...), ai_timeout)`；
   超时/异常/空回复都要降级成 `ai_error_reply` 并计 `ai_failures`。
4. **配置值先清洗**：所有字符串配置走 `strip_wrapping_quotes`（粘贴常带引号），
   反代/endpoint 缺协议头时按 `https://` 补全。
5. **密钥打码**：`/tg状态`、`/tg诊断`、日志里 token 与 `reverse_proxy_secret` 一律不能出现明文。
6. **能力探测一律 getattr**：`provides=`（a15+）、`ctx.services`、`ctx.app_commands` 在老宿主上可能不存在。
7. **不要重新引入 QQ**：用户明确要求"单独的 tgbot"；要加回来先确认。

## 提交前

```bash
# 单元测试（59 项，建议至少跑 a16 与 2.0.1）
PYTHONPATH=<NeoBot>/packages/modloader/src:<NeoBot>/packages/contracts/src:<NeoBot>/packages/adapter/src \
  python -m pytest tests -q

python devtools/simulate.py                     # 宿主模拟器（需同一 PYTHONPATH）
python tools/check_compat.py --ref <宿主根目录>   # 静态兼容审计
python -m neobot_modloader.sdk check .          # 官方 SDK（2.1.0a15+ 才有）
```

Windows 注意：`powershell` 的文本往返会破坏中文编码，别用 `Get-Content -Raw` + `Set-Content`
改这些文件；测试临时目录用 `tests/conftest.py` 的 `scratch` fixture，不要用 `tmp_path`。
