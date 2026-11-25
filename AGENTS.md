# AGENTS.md — telegram 插件

给开发助手的最小上下文：这是 NeoBot 2.1 的第三方插件，把 Koishi 的 Telegram 适配器
（`@koishijs/plugin-adapter-telegram` / `@satorijs/adapter-telegram`）以插件形态移植过来。

## 边界（不要越界）

- **不修改 NeoBot 本体**，只用 `neobot_modloader` 公开 API。
- **不自己建 QQ 侧连接、不持有宿主适配器**：出站一律走 `ctx.send` / `ctx.send_group` / `ctx.send_private`。
- **不要试图注册新通道**：宿主 `service_registry` 里 `adapter` 是 `__host__` 注册项，
  插件覆盖不了；AI 回复的出站固定走宿主适配器。Telegram 一侧的收发必须由本插件自己完成。
- Telegram 的网络调用只走 `tg_api.TelegramAPI`，不要在别处直接 `httpx` 发 Bot API 请求
  （代理、重试、429 退避都在那里）。

## 结构

| 文件 | 职责 |
| --- | --- |
| `__init__.py` | Plugin 声明、配置、生命周期、命令注册、宿主消息订阅、桥接编排 |
| `tg_api.py` | Bot API 客户端：endpoint/代理/重试/429/长轮询/文件下载（**零宿主依赖**） |
| `tg_model.py` | Update 解码：实体解析、媒体、相册聚合（**零宿主依赖**，纯函数） |
| `tg_send.py` | 出站渲染：HTML 转义、标签安全分段、媒体映射、按钮、发送计划（**零宿主依赖**） |
| `tg_bridge.py` | 绑定存储、路由成型、回环防护（**只依赖标准库**） |

`tg_*` 四个模块**不 import 任何 NeoBot 模块**，这样可以直接单测；只有 `__init__.py`
依赖宿主，所以 `__init__.py` 的导入写成「包导入 / 顶层导入」双分支（pytest 收集会把
插件根目录当模块导入一次）。

## 必须遵守的行为约定

1. `allowed_updates` **不要传空列表**：Koishi 4.5.11 这么做导致 `chat_member` 永远收不到，
   这里默认显式带上全集。
2. 相册聚合用 `MediaGroupBuffer`，**不要**在轮询循环里 `await sleep(1.2)`（会阻塞派发）。
3. 转发前先查回环防护（`LoopGuard`），否则两侧会互相刷屏。
4. 命令判断看**正文**（`qq_body_text`），不要看带前缀的整串。
5. 绑定类命令统一 `permission=0` 注册 + 处理器里自查（`_may_admin`），
   这样插件 `admins` 名单与宿主管理员都能用；权限判断失败要 **fail-closed**。
6. 老宿主能力探测一律 `getattr`：`provides=`（a15+）、`ctx.services`、`ctx.app_commands`
   都可能不存在，缺能力时降级而不是崩。

## 提交前

```bash
# 单元测试（57 项，四个宿主版本都跑一遍）
PYTHONPATH=<NeoBot>/packages/modloader/src:<NeoBot>/packages/contracts/src:<NeoBot>/packages/adapter/src \
  python -m pytest tests -q

python devtools/simulate.py                     # 宿主模拟器（需同一 PYTHONPATH）
python tools/check_compat.py --ref <宿主根目录>   # 静态兼容审计（不需要依赖）
python -m neobot_modloader.sdk check .          # 官方 SDK（2.1.0a15+ 才有）
```

Windows 上注意：`powershell` 的文本往返会破坏中文编码，**不要**用 `Get-Content -Raw` +
`Set-Content` 改这些文件，用编辑器/`write` 工具；测试临时目录用 `tests/conftest.py` 的
`scratch` fixture，不要用 `tmp_path`（本机沙箱下会 WinError 5）。
