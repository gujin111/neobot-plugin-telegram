# AGENTS.md — telegram 插件

给开发助手的最小上下文：NeoBot 2.x 的第三方插件，把 Telegram 做成 NeoBot 的**对话前端**
（TG 用户直接和宿主 AI 聊天）。**不涉及 QQ 桥接**——2.0.0 起已移除。

## 边界（不要越界）

- **不修改 NeoBot 本体**，只用 `neobot_modloader` 公开 API。
- **不自己实现 AI**：回复必须走宿主的 `reply_orchestrator.start_background_reply(...)`，
  文本从返回的 `ReplyEvent.generated_text` 读，不要自己拼提示词或直连模型。
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

1. **同一个 TG 会话串行**：`reply_with_ai` 用每会话 `asyncio.Lock`；宿主返回 `None`
   （该工作区已有管线在跑）时计 `ai_busy` 并返回空，不要重试轰炸宿主。
2. **等待要有上限**：轮询 `generated_text` 直到 `ai_timeout` 或状态终态
   （`COMPLETED`/`FAILED`/`CANCELLED`）。
3. **配置值先清洗**：所有字符串配置走 `strip_wrapping_quotes`（粘贴常带引号），
   反代/endpoint 缺协议头时按 `https://` 补全。
4. **密钥打码**：`/tg状态`、`/tg诊断`、日志里 token 与 `reverse_proxy_secret` 一律不能出现明文。
5. **能力探测一律 getattr**：`provides=`（a15+）、`ctx.services`、`ctx.app_commands` 在老宿主上可能不存在。
6. **不要重新引入 QQ 桥接**：用户明确要求删掉；要加回来先确认。

## 提交前

```bash
# 单元测试（57 项，建议至少跑 a16 与 2.0.1）
PYTHONPATH=<NeoBot>/packages/modloader/src:<NeoBot>/packages/contracts/src:<NeoBot>/packages/adapter/src \
  python -m pytest tests -q

python devtools/simulate.py                     # 宿主模拟器（需同一 PYTHONPATH）
python tools/check_compat.py --ref <宿主根目录>   # 静态兼容审计
python -m neobot_modloader.sdk check .          # 官方 SDK（2.1.0a15+ 才有）
```

Windows 注意：`powershell` 的文本往返会破坏中文编码，别用 `Get-Content -Raw` + `Set-Content`
改这些文件；测试临时目录用 `tests/conftest.py` 的 `scratch` fixture，不要用 `tmp_path`。
