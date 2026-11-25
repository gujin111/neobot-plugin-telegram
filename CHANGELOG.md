# Changelog

## 1.1.1

修「容器里解析不了 `host.docker.internal`」这类报错的可操作性。

### 修复

- `proxy = "auto"` 的候选地址补上**容器默认网关**（读 `/proc/net/route`）与 `172.17.0.1`～`172.20.0.1`，
  并且**只把能解析的 `host.docker.internal` 放进候选**——普通容器里该别名不存在，
  以前会先撞一次 `getaddrinfo failed`。
- 连接失败时，若错误文本含 `getaddrinfo`，建议会直接说明：容器里没有该别名，改用
  `ip route | grep default` 的网关地址、`172.17.0.1`，或直接 `proxy = "auto"`。
- 建议里的代理地址统一按解析结果展示（原来会把用户写的末尾斜杠原样带出来）。

### 验证

- pytest 71/71（新增：`candidate_hosts` 跳过不可解析别名、`getaddrinfo` 建议文案与地址归一化）。
- `devtools/simulate.py` 通过；干净副本 `sdk check` 通过；契约导出 1.1.1。

## 1.1.0

代理能力内置：`socks5://` 不需要再装 `socksio`，`proxy` 还能自己找端口。

### 新增

- `tg_proxy.py`：纯 asyncio 的 **SOCKS5 客户端**（RFC 1928/1929；域名与 IPv4 目标、无认证或账号密码）
  ＋ **本地 HTTP CONNECT 网关**。配置 `proxy = "socks5://…"` 时，插件在 `127.0.0.1` 起一个随机端口的
  隧道交给 httpx，因此容器里只要有基础 httpx 就能连 SOCKS5，**不引入任何新依赖**。
- `proxy = "auto"`：按 `127.0.0.1` 与 `host.docker.internal` × 常见端口
  （7890/7891/7897/1080/10808/10809/20171/3128/8889）做 TCP 探测，命中即用，省得手算端口。

### 变更

- `python_dependencies` 回退为 `httpx`（不再需要 `httpx[socks]`）。
- `/tg诊断` 显示 proxy 的实际解析结果与内置网关地址；`auto` 未命中时明确告警并提示改成显式地址。
- 连接失败建议同步更新：socks5 不再提示装 socksio，改为提示"端口是否提供 SOCKS5 / 账号密码是否正确"。

### 验证

- pytest 69/69（新增 7 项：代理地址解析、SOCKS5 握手成功/认证失败/代理不可达、
  网关 CONNECT 隧道端到端、非 CONNECT 请求 405、`auto` 探测）。
- `devtools/simulate.py` 通过；干净副本 `neobot_modloader.sdk check` 通过；契约重新导出为 1.1.0。

## 1.0.2

把反代做成插件的一等配置，不用再手拼 URL；`extras/` 一并入库。

### 新增

- 配置项 **`reverse_proxy_url` + `reverse_proxy_secret`**：填反代基址与路径密钥即可，
  插件自动拼成 `{基址}/{密钥}` 并做斜杠归一化，优先级高于 `endpoint`。
  配合 `extras/cloudflare-worker.js` 使用，容器/服务器里不需要任何本地代理。
- `extras/`：
  - `cloudflare-worker.js`：只转发 `/bot<token>/...` 与 `/file/bot<token>/...` 的反代 Worker，
    支持 `PATH_SECRET` 路径密钥，其它路径一律 404；
  - `find-proxy.py`：在容器里自动探测宿主代理地址，并真发一次 `getMe` 验证；
  - `windows-host-proxy.ps1`：Windows 宿主自检（网卡、代理进程、监听地址、防火墙放行）；
  - `README.md`：三种网络方案的完整操作步骤（含 Nginx 备选）。

### 变更

- 启动日志、`/tg状态`、`/tg诊断` 统一显示**实际使用的 endpoint 与来源**
  （反代 / endpoint 配置 / 官方默认），反代密钥在日志与诊断里一律打码。
- 连接失败建议按来源分流：反代 / 自定义 endpoint / 代理 / 直连，各给对应排查项。

## 1.0.1

修「连不上时不知道该怎么办」，并把启动失败变成可自愈。

### 修复

- **连接失败给出可执行建议**。`TelegramTransportError: ... All connection attempts failed`
  这类错误按四种情况分别提示：直连且未配代理 / 代理地址不合法 / `socks5://` 缺 socksio /
  反代没转发 `/bot<token>/...`。日志同时打印实际使用的 `endpoint` 与 `proxy`。
- **启动失败不再一错到底**：新增看门狗，按 `max(15s, retry_interval×3)` 复检连通性，
  恢复后自动登录并接上轮询；`/tg重连` 仍可立即重试。
- **`/tg诊断` 增加真实连通性探测**（`getMe`，6s 超时）：显示"通/不通 + 耗时 + 原因 + 建议"。

### 变更

- `python_dependencies` 改为 `httpx[socks]>=0.27,<0.29`：支持 `socks5://` 代理。
- 代理地址不合法（例如漏了 `http://`）时给出明确错误，而不是把底层 `ValueError` 抛给用户。

## 1.0.0

首个版本：把 Koishi 的 `@koishijs/plugin-adapter-telegram`（`@satorijs/adapter-telegram` 4.5.x）
以 NeoBot 插件形态移植，并补上 Koishi 侧缺失的能力。

### 新增

- **Telegram Bot API 客户端**（`tg_api.py`）：endpoint / 代理 / 超时、业务错误包装
  （`Telegram API error <code>. <description>`，与 Koishi `SenderError` 同款文案）、
  网络与 5xx 指数退避、**429 读 `parameters.retry_after` 精确退避**、`getMe` / `getUpdates` /
  `setWebhook` / `deleteWebhook` / `getFile`、文件下载、multipart 上传。
- **长轮询**（`TelegramPoller`）：自动摘除已有 webhook、`timeout=0` 探活对齐 offset、
  初次连接失败超过 `retry_times` 转离线、之后按 `retry_interval` 继续重试；**显式带上
  `allowed_updates`（含 `chat_member`）**，修掉 Koishi 4.5.11 里「空列表探活后被沿用」的副作用。
- **Update 解码**（`tg_model.py`）：`message` / `edited_message` / `channel_post` /
  `callback_query` / `chat_join_request` / `my_chat_member` 归一化；实体断点解析
  （bold/italic/underline/strikethrough/code/pre/spoiler/mention/text_link/text_mention/br）；
  `/cmd@bot` 剥离；相册按 `media_group_id` 聚合；**`entities: []` 空数组安全**（Koishi 会抛 TypeError）。
- **出站渲染**（`tg_send.py`）：HTML 转义、**标签安全的超长文本分段**（Koishi 超 4096 直接报错）、
  caption 超 1024 时媒体照发、文字另发、媒体方法映射（gif→animation、voice→sendVoice 等）、
  多资源 `sendMediaGroup`、Inline Keyboard（每行最多 5 个）、**纯按钮消息也会发出**（Koishi 会漏发）。
- **桥接**（`tg_bridge.py` + `__init__.py`）：Telegram ⇄ QQ 双向、方向可独立开关、
  绑定持久化（原子写 JSON）、**回环防护**（归一化指纹 + TTL）、前缀模板、命令/机器人消息过滤。
- **8 条快捷命令**全部注册进宿主命令表（`/help` 列表与详情可见，含别名与参数说明）：
  `/tg`（telegram/电报）、`/tg绑定`（tgbind）、`/tg解绑`（tgunbind）、`/tg列表`（tglist/电报列表）、
  `/tg推送`（tgsend/推送电报）、`/tg会话`（tgchats）、`/tg诊断`（tgdiag/电报诊断）、
  `/tg重连`（tgreload）。绑定类命令用插件 `admins` 名单或宿主权限自查（fail-closed）。
- **`telegram` 宿主服务**：`ctx.services.require("telegram")` 可取到 `status()` / `send_text()` /
  `bindings()`，供其它插件推送。

### 兼容性（实测）

| 宿主 | 单元测试 | 宿主模拟器 | 静态审计 | 官方 SDK `check` |
| --- | --- | --- | --- | --- |
| **v2.1.0a16**（最新 release） | 57/57 | 通过 | OK | 通过（`export` 生成 `plugin.contract.json`） |
| **v2.0.1**（最新稳定版） | 57/57 | 通过 | OK | 该版本尚无 `neobot_modloader.sdk` |
| v2.1.0a9 | 57/57 | 通过 | OK | — |
| v2.1.0a8 | 57/57 | 通过 | OK | — |
| v2.1.0a7 | 57/57 | 通过 | OK | — |

`plugin.toml` 的 `min_neobot_version = "2.0.0"`；只使用 `neobot_modloader` 公开 API，
`provides=`（a15+）、`ctx.services`、`ctx.app_commands` 均按能力探测，旧宿主缺能力时降级而不是报错
（v2.0.1 / a7 / a9 上没有 `provides` 参数，代码里 try/except 退回无服务声明）。

### 已知限制

- Webhook 模式未实现（插件层拿不到宿主入站 HTTP 端点），`protocol = webhook` 时按 polling 运行并告警。
- QQ → Telegram 方向上，机器人自己的消息（含 AI 回复）能否回传取决于协议端是否上报
  `post_type=message_sent`；插件无法拦截宿主出站，`/tg诊断` 用 `qq_bot_messages` 计数提示。
- 未做 Koishi 那 101 个内部 API 的全量类型镜像；音视频/图片/文件已支持，贴纸/投票/位置未封装。
