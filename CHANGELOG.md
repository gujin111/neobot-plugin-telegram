# Changelog

## 3.0.1

把"连不上"的原因直接写进日志和诊断，不用再靠猜。

### 新增

- **token 形状检查**：不是 `<数字>:<字母数字串>` 时启动即警告（误填机器人用户名、api_id、
  只填冒号后半段都会命中）；
- **404 专用建议**：明确指向 token 写错，并区分两种情况——
  Telegram 回的 `Not Found`（响应体 JSON）是 token 问题，
  Worker 回的 `not found`（小写纯文本）才是反代路径密钥不对；
- `/tg诊断` 新增一行 `· token 形状：正常 / 可疑（应为 <数字>:<字母数字串>）`。

### 实测对照（边缘 IP + SNI）

| 请求 | 响应 |
| --- | --- |
| 正确密钥 + 合法 token | `401 {"ok":false,…,"description":"Unauthorized"}` |
| 正确密钥 + 不含冒号的 token | `404 {"ok":false,…,"description":"Not Found"}` |
| 路径密钥错 | `404 not found`（小写纯文本） |
| 缺 `X-Proxy-Key` | `403 forbidden` |

### 验证

pytest **49/49**；`devtools/simulate.py` 通过；契约导出 3.0.1。

## 3.0.0

**瘦身**：删掉所有用不到的能力，代码量减半。功能不变——TG 用户照旧和 AI 对话，用户照旧只填 token。

### 删掉的东西

| 删掉的 | 原因 |
| --- | --- |
| `tg_proxy.py`（293 行）：内置 SOCKS5 客户端、本地 CONNECT 网关、`auto` 探测 | 默认走反代后没人需要；`proxy` 现在直接交给 httpx，只支持 http(s) 代理 |
| `extras/find-proxy.py`、`extras/windows-host-proxy.ps1` | 都是为 `auto` 探测服务的 |
| 标签平衡渲染、媒体发送、内联按钮、`OutgoingMedia`/`SendAction`/`PlanOptions`（tg_send 442 → 34 行） | 我们只发 AI 的纯文本：整体转义后按段落分段即可 |
| 实体解析、`Span`、相册聚合 `MediaGroupBuffer`（tg_model 492 → 152 行） | 富文本还原与相册合并对"把文本交给模型"没有意义 |
| `download_file` / `set_webhook` / `edit_message_text` / `delete_message` / 多部分上传（tg_api 493 → 392 行） | 只保留收发消息必需的调用 |
| 服务门面（`provides=["telegram"]`）、`reconnect()`、`MediaGroupBuffer` 刷新任务 | 没有消费者 |
| 配置项：`protocol`、`allowed_updates`、`parse_mode`、`link_preview`、`chunk_size` | 固定为 polling / 全集 / HTML / 无预览 / 3500 字 |

### 现状

- 运行时 4 个文件共 **1167 行**（原 2399 行，减半）：`__init__.py` 589、`tg_api.py` 392、`tg_model.py` 152、`tg_send.py` 34；
- 配置 15 项，其中只有 `token` 必填；
- 命令仍只有 `/tg`、`/tg诊断`；
- 计数器简化为 `updates / messages / replies / failures / errors`。

### 验证

pytest **46/46**（测试同步精简：出站只剩转义与分段、解码只剩拍平、客户端只留轮询与重试）；
`devtools/simulate.py` 通过；干净副本 `sdk check` 通过；契约导出 3.0.0。

## 2.3.0

给内置反代加**请求头校验**：只拿到地址也用不了。

### 新增

- Worker 侧新增 `PROXY_KEY`：请求必须带 `X-Proxy-Key: <同值>`，否则 403；
- 插件新增配置 `proxy_key`（默认与内置 Worker 一致），每个 Bot API 请求都会带上该头；
- `/tg诊断` 显示 `proxy_key：pk-***`（打码），方便确认配置对不对；
- 与原有的 `PATH_SECRET`（URL 路径密钥）组成两道校验。

### 说明

- 2.2.0 及更早的插件版本**不带这个头**，Worker 一旦开启 `PROXY_KEY` 就会 403 ——
  升级到 2.3.0 后再给 Worker 加变量（顺序很重要，见 README）；
- 自建 Worker 时把 `PROXY_KEY` 设成自己的值，并同步填到插件 `proxy_key`。

### 验证

pytest **61/61**（新增"自定义头应用到 httpx 客户端""默认 proxy_key 且诊断打码"）；
`devtools/simulate.py` 通过；契约导出 2.3.0。

## 2.2.0

**默认走作者的反代**：用户只填 `token` 就能用，不用配代理、也不用买域名。

### 新增

- 配置默认值改为 `reverse_proxy_url = "https://tg.gujin99.xyz"`、`reverse_proxy_secret` 内置，
  新装只填 `token` 即可连通国内不可直连的环境；
- 想直连就把 `reverse_proxy_url` 清空（回落到 `endpoint`），想自建就把地址与密钥换成自己的；
- `/tg状态`、`/tg诊断` 会显示 `来源：反代` 与打码后的地址，便于确认走的是哪条路。

### 说明

- 这是**共享反代**：所有用户的 API 流量都经过作者的 Cloudflare Worker，Worker 只转发
  `/bot<token>/…` 与 `/file/bot<token>/…`，不记录内容；
- Worker 不可用时（域名失效、Cloudflare 故障），用户需自行改 `endpoint` 或自己的反代；
- 后续如需更高隐私，建议自建 Worker（[extras/README.md](extras/README.md)）。

### 验证

pytest **59/59**（新增"默认为反代""清空后回落 endpoint/官方"等解析用例）；
`devtools/simulate.py` 通过；契约导出 2.2.0。

## 2.1.0

**彻底去掉 QQ 依赖**：不再借用宿主的 QQ 回复管线，改为直接调用宿主的**主模型服务**
（`provider`），上下文由插件按 TG 会话自己维护。这样 Telegram 侧完全不产生任何 QQ 消息、
也不需要填 QQ 群号/QQ 号。

### 破坏性变更

- 删除配置 `ai_endpoint_kind`、`ai_endpoint_id`、`ai_channel`、`ai_prefix`；
- 新增配置 `system_prompt`、`history_limit`、`ai_timeout`、`ai_error_reply`；
- AI 调用从「`reply_orchestrator.start_background_reply` + 读事件」改为
  「`provider.chat(messages)` 直接拿结果」。

### 新增

- **每个 TG 会话独立上下文**（内存，默认 12 条，`history_limit=0` 可关）；
- 超时（`asyncio.wait_for`）、模型异常、空回复三种情况都有明确降级与计数；
- `/tg状态`、`/tg诊断` 显示模型服务可用性、上下文条数与 `system_prompt` 长度。

### 说明

- 模型用的是 NeoBot 面板里配置的那个，**不需要另填 API Key**；
- 不经过宿主的工具/技能/人格/记忆，`system_prompt` 自己写；
- 上下文在内存里，重启清空；群聊所有成员共用一个上下文。

### 验证

- 单元测试 **59/59**（provider 调用、上下文累积与裁剪、超时/异常/空回复降级、
  命令集只有两条、配置清理、地址解析、诊断）；
- `devtools/simulate.py` 通过（假 provider 驱动两轮对话并验证上下文）；
- 契约导出为 2.1.0。

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
