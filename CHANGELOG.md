# Changelog

## 2.0.0

**破坏性变更**：移除 QQ 桥接，改为「Telegram 直连 AI」。

原来它是 QQ ⇄ Telegram 的双向转发桥（消息在两边同步，AI 只在 QQ 侧工作）。
现在改成：TG 用户发消息 → 宿主 AI 生成 → 回复直接发回 TG。QQ 相关命令与配置全部删除，
只保留状态查询。

### 破坏性变更

- 删除 `tg_bridge.py`（绑定存储、回环防护、双向文本成型）；
- 删除命令：`/tg绑定`、`/tg解绑`、`/tg列表`、`/tg推送`、`/tg会话`、`/tg重连`；
- 删除配置：`prefix_from_tg`、`prefix_from_qq`、`forward_bot_messages`、`forward_user_messages`、
  `forward_commands`、`admins`、`allow_sub_admin`；
- 只保留 `/tg`（状态）与 `/tg诊断`（链路诊断）。

### 新增

- **AI 直答**：TG 消息 → `reply_orchestrator.start_background_reply(kind, conversation_id, content, channel, proactive)`
  → 轮询 `ReplyEvent.generated_text` → 发回 Telegram；
- 配置项：`ai_endpoint_kind` / `ai_endpoint_id` / `ai_channel` / `ai_prefix` / `ai_timeout` / `ai_error_reply`；
- 同一 TG 会话内串行处理（`asyncio.Lock`），宿主返回 `None`（已有回复进行中）计入 `ai_busy`；
- 未配置 AI 工作区时，直接给 TG 用户一句可操作的提示；
- 计数器扩展：`messages` / `ai_replies` / `ai_failures` / `ai_busy` / `dropped_bot`。

### 保留

长轮询与重连、内置 SOCKS5（不需要 socksio）、`proxy = "auto"`、反代配置与密钥打码、
HTML 渲染与标签安全分段、相册聚合、`/tg诊断` 的真实 `getMe` 探测、启动失败自动复检。

### 已知限制

- AI 的回复会**同时**发到配置的 QQ 工作区（宿主发送，插件拦不住），建议用专用小号/小群；
- 多个 TG 会话共用同一个工作区，AI 上下文会混在一起；
- Webhook 未实现，只做长轮询。

### 验证

- 单元测试 **57/57**（重写：命令集只剩两条、AI 直答成功/失败/忙/超时、配置清理、地址解析、诊断）；
- `devtools/simulate.py` 通过（假编排器驱动完整链路）；
- 干净副本 `neobot_modloader.sdk check` 通过；契约导出为 2.0.0。

## 1.1.2

修「配置里多带了一个引号 / 漏了协议头」导致反代连不上。

- `token` / `endpoint` / `reverse_proxy_url` / `reverse_proxy_secret` / `proxy` 自动剥掉粘贴带来的引号；
- 缺 `http(s)://` 时按 `https://` 补全；
- `/tg状态`、`/tg诊断` 展示清理后的地址。

## 1.1.1

修「容器里解析不了 `host.docker.internal`」：`proxy = "auto"` 的候选补上容器默认网关与
`172.17.0.1`～`172.20.0.1`，只把能解析的 `host.docker.internal` 放进候选；`getaddrinfo`
报错时直接给出替代写法。

## 1.1.0

代理能力内置：纯 asyncio 的 SOCKS5 客户端 + 本地 HTTP CONNECT 网关（`socks5://` 不需要
`socksio`）；新增 `proxy = "auto"` 自动探测宿主代理端口；`/tg诊断` 显示解析结果与网关地址。

## 1.0.2

把反代做成插件配置：`reverse_proxy_url` + `reverse_proxy_secret`（自动拼 URL、斜杠归一化、
密钥打码）；`extras/` 一并入库（Cloudflare Worker 反代脚本、容器内自动探测代理、
Windows 宿主自检脚本、部署说明）。

## 1.0.1

连接失败给出可执行建议（直连未配代理 / 代理地址不合法 / socks 缺依赖 / 反代未转发），
启动失败自动复检并恢复轮询；`/tg诊断` 增加真实 `getMe` 探测。

## 1.0.0

首个版本：移植 Koishi 的 Telegram 适配器，双向 QQ ⇄ Telegram 桥接 + 8 条命令
（该桥接能力已在 2.0.0 移除）。
