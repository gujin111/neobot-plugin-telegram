# telegram — NeoBot 的 Telegram 桥接插件

把 Koishi 的 [`@koishijs/plugin-adapter-telegram`](https://github.com/satorijs/satori/tree/main/adapters/telegram)
（即 `@satorijs/adapter-telegram`）以**插件**形态移植到 [NeoBot](https://github.com/SuperQuail/NeoBot)：
自包含的 Telegram Bot API 客户端 + QQ ⇄ Telegram 双向桥接 + 8 条快捷命令（全部出现在 `/help` 命令展示页）。

- 目标宿主：**NeoBot v2.1.0a16（最新 release）**，同时验证 v2.0.1（最新稳定版）、v2.1.0a7 / a9
- 纯插件实现：**不改 NeoBot 本体**，只用 `neobot_modloader` 公开 API
- 只依赖宿主已有的 `httpx` / `pydantic`，无额外 Python 依赖

---

## 为什么是「桥接插件」而不是「新增一个通道」

NeoBot 的协议适配器（OneBot / QQ 官方 / local）是**宿主自己的核心服务**：
`packages/modloader/src/neobot_modloader/service_registry.py` 里 `__host__` 注册项优先级最高，
插件无法覆盖或替换 `adapter`；AI 回复的出站固定走宿主 `adapter.send(...)`，通道只认 `onebot` / `official`。

所以插件的定位是：

| 方向 | 谁来收 | 谁来发 |
| --- | --- | --- |
| Telegram → QQ | 插件（长轮询 / Webhook） | 宿主 `ctx.send_group` / `ctx.send_private` |
| QQ → Telegram | 宿主消息事件（`@plugin.message`） | 插件（Bot API `sendMessage` / `sendMediaGroup`） |

要让 Telegram 变成「像 OneBot 一样的一等通道」（进入 dual 路由、意愿判定、AI 直答），
需要在 `neobot-adapter` 里加后端并合并上游——那是另一份工作，本插件不做。

---

## 安装

1. 把整个目录复制到 NeoBot 数据目录的 `plugins/` 下，**目录名改成 `telegram`**：

   ```text
   <NeoBot 数据目录>/
   └─ plugins/
      └─ telegram/
         ├─ plugin.toml
         ├─ __init__.py
         ├─ tg_api.py / tg_model.py / tg_send.py / tg_bridge.py
         └─ ...
   ```

   ```bash
   cp -r neobot-plugin-telegram <NeoBot 数据目录>/plugins/telegram
   ```

2. 重启 NeoBot，或在网页面板「插件管理」里刷新 / 加载。
3. 打开面板「插件 → 配置 → telegram」，填 `token`（BotFather 给的 `123456:ABC-...`）
   和 `proxy`（国内直连 `api.telegram.org` 不通时必须填，如 `http://127.0.0.1:7890`），保存。
4. 在 `plugins_data/telegram/config.toml` 也可以直接改；绑定关系存在同目录的 `bindings.json`。

## 快速开始（三步）

```text
① 在 Telegram 里给机器人发一条消息（让插件记下会话 ID）
② 在 QQ 群里：@机器人 /tg会话            → 看到形如 -1001234567890 的 ID
③ 在 QQ 群里：@机器人 /tg绑定 -1001234567890
```

之后：Telegram 里发的消息会带前缀进这个 QQ 群；这个 QQ 群里成员的消息会带前缀发到 Telegram。
双向都由插件完成，宿主 AI 照常在该 QQ 群工作（AI 回复是否回传见「已知限制」）。

---

## 连不上 Telegram 怎么办

日志里出现下面这条，是**网络层不通**（不是插件 bug）：

```text
Telegram 连接异常：TelegramTransportError: Telegram transport error on getMe:
ConnectError: All connection attempts failed
```

`All connection attempts failed` 表示 TCP 连不上 `api.telegram.org`（解析出的地址全试过都失败）。
按你的实际情况选一条：

| 情况 | 怎么配 |
| --- | --- |
| 有代理软件（Clash / v2ray 等） | `proxy = "http://127.0.0.1:7890"`（端口换成你软件里的 HTTP 端口）；SOCKS5 写 `socks5://127.0.0.1:1080`，依赖 `httpx[socks]`（1.0.1 起默认安装） |
| NeoBot 跑在容器 / WSL 里 | `127.0.0.1` 指向容器自身，要填宿主机地址（如 `http://host.docker.internal:7890`）或网关 IP —— 不确定就先在容器里跑 [`extras/find-proxy.py`](extras/find-proxy.py)，它会试出并打印可用地址 |
| **没有代理 / 不想折腾** | **用插件内置的反代配置**（推荐，见下一节），不需要任何本地代理软件 |

### 反代配置（推荐，无需本地代理）

1. 部署反代：打开 <https://dash.cloudflare.com> → Workers & Pages → Create Worker →
   粘贴 [`extras/cloudflare-worker.js`](extras/cloudflare-worker.js) → Deploy；
2. 建议在 Settings → Variables and Secrets 加一个 `PATH_SECRET`（例如 `v1-9f3a7c2e`），再 Deploy；
3. 插件里只填两行（**不用自己拼 URL**）：

   ```toml
   reverse_proxy_url = "https://tg.example.workers.dev"
   reverse_proxy_secret = "v1-9f3a7c2e"     # Worker 没设密钥就留空
   ```

插件会自动拼成 `https://tg.example.workers.dev/v1-9f3a7c2e/bot<token>/<method>`，
并把 `/file/bot<token>/...` 一起走反代；密钥在日志与 `/tg诊断` 里一律打码。
部署细节与 Nginx 备选写法见 [`extras/README.md`](extras/README.md)。

改完保存 → 面板里重载插件（或 `/tg重连`）。1.0.1 起启动失败会每 15 秒自动复检一次，
连通后自己接上轮询，不用反复重启；`/tg诊断` 会做一次真实 `getMe` 探测，直接告诉你通没通、耗时多少、下一步该改什么。

## 命令（全部注册进宿主命令表 → `/help` 可见）

| 命令 | 别名 | 权限 | 说明 |
| --- | --- | --- | --- |
| `/tg [子命令]` | `telegram`、`电报` | 所有人 | 状态总览；也可 `/tg bind|unbind|list|send|chats|diag|reload` |
| `/tg绑定 <TG会话ID> [group\|private] [QQ号]` | `tgbind` | 需管理员 | 把当前 QQ 会话与 Telegram 会话绑定 |
| `/tg解绑 [TG会话ID]` | `tgunbind` | 需管理员 | 解除绑定（省略 ID 按当前 QQ 会话解） |
| `/tg列表` | `tglist`、`电报列表` | 所有人 | 列出全部绑定与方向 |
| `/tg推送 <文本>` | `tgsend`、`推送电报` | 需管理员 | 向当前 QQ 会话绑定的 Telegram 会话推送 |
| `/tg会话` | `tgchats` | 所有人 | 最近收到消息的 Telegram 会话与 ID |
| `/tg诊断` | `tgdiag`、`电报诊断` | 所有人 | token 打码、代理、轮询状态、计数器 |
| `/tg重连` | `tgreload` | 需管理员 | 重连 Telegram |

「需管理员」= 宿主权限（次级 / 超级管理员）**或**插件配置 `admins` 名单里的号。
群聊里仍要 `@机器人`；私聊不需要（命令在两种会话都能用）。

---

## 配置项

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 总开关（关掉后不连 Telegram，命令仍可用） |
| `token` | 空 | BotFather 令牌；空值只打日志、插件待机 |
| `endpoint` | `https://api.telegram.org` | 反代地址；与下面的 `reverse_proxy_url` 二选一 |
| `reverse_proxy_url` | 空 | **反代基址**（如 `https://tg.example.workers.dev`）；填了优先于 `endpoint` |
| `reverse_proxy_secret` | 空 | 反代路径密钥，与 Worker 的 `PATH_SECRET` 一致；插件自动拼成 `{基址}/{密钥}` |
| `proxy` | 空 | `http://` / `socks5://` 代理；**国内必需** |
| `protocol` | `polling` | 当前只实现 `polling`（见已知限制） |
| `polling_timeout` | `25` | 长轮询等待秒数 |
| `retry_times` / `retry_interval` | `6` / `5.0` | 初次连接重试次数 / 断开后重试间隔 |
| `allowed_updates` | 空 = 全集 | 留空即包含 `chat_member`（修正 Koishi 侧默认遗漏） |
| `parse_mode` | `html` | 发送解析格式；`none` 则发纯文本 |
| `link_preview` | `true` | 是否展示链接预览 |
| `chunk_size` | `3500` | 长文本分段长度（Telegram 上限 4096） |
| `prefix_from_tg` | `[TG {name}] ` | TG→QQ 前缀，支持 `{name}` `{chat}` `{id}` |
| `prefix_from_qq` | `[QQ {name}] ` | QQ→TG 前缀，支持 `{name}` `{group}` `{id}` |
| `forward_bot_messages` | `true` | 转发 QQ 侧机器人自己的消息（含 AI 回复） |
| `forward_user_messages` | `true` | 转发 QQ 侧其他成员的消息 |
| `forward_commands` | `false` | `/` 开头的命令是否也转发 |
| `ignore_telegram_bots` | `true` | 忽略 Telegram 侧其他机器人（防互刷） |
| `admins` | `[]` | 额外管理员 QQ 号 / 官方 openid |
| `allow_sub_admin` | `true` | 次级管理员是否可执行绑定类命令 |

---

## 桥接模型

- **绑定**：一条记录 = 一个 Telegram 会话 ⇄ 一个 QQ 会话（群或私聊），两个方向独立开关，
  存在 `plugins_data/telegram/bindings.json`（原子写）。
- **回环防护**：转发出去的内容按「归一化文本指纹」进 TTL 集合（默认 90s），
  另一端再看到同样内容就不回传，避免两个平台互相刷屏。
- **媒体**：文本桥接时用 `[图片] / [语音] / [视频] / [文件]` 占位；
  Telegram → QQ 的媒体暂不下载转存（避免大文件把 QQ 侧撑爆）。
- **相册**：同一 `media_group_id` 的消息在 1.2s 窗口内合并成一条再投递（同 Koishi 语义，
  但不阻塞轮询循环）。
- **入站渲染**：Telegram `entities` 按 Koishi 的断点算法解析成 `b/i/u/s/code/pre/spoiler/a/at`，
  桥接到 QQ 时降级为纯文本。
- **出站渲染**：QQ 消息段 → Telegram HTML（转义 + 标签安全分段 + 每行最多 5 个按钮），
  超长文本自动拆条（Koishi 侧没有这个能力，超 4096 会直接报错）。

## 与 Koishi 适配器的对照

| 能力 | Koishi 4.5.11 | 本插件 |
| --- | --- | --- |
| 长轮询 offset / 自动摘 webhook / 重连退避 | ✅ | ✅（语义照搬） |
| `allowed_updates` | 探活用空列表 → 永久收不到 `chat_member` | ✅ 默认显式含 `chat_member` |
| 429 限流 | 只按重试间隔 | ✅ 读 `parameters.retry_after` 精确退避 |
| 实体解析（含 spoiler / text_mention） | ✅ | ✅ |
| 相册聚合 | 每条 sleep 1.2s（阻塞派发） | ✅ 显式缓冲，不阻塞 |
| 超长文本 | 直接报错 | ✅ 标签安全分段 |
| 纯按钮消息 | `flush()` 守卫会漏发 | ✅ 仍会发送 |
| `entities: []` | `entities?.[0].type` 抛 TypeError | ✅ 空数组安全 |
| Webhook | ✅ | ❌ 未实现（插件层拿不到宿主入站 HTTP 端点） |
| 内部 API 101 个方法 | ✅ 动态生成 | 未做全量镜像；需要时用 `httpx` 直调或提需求 |
| 发贴纸 / 投票 / 位置 | 部分 | 未封装（音视频/图片/文件已支持） |

---

## 已知限制（都是宿主结构决定的，不是 bug）

1. **AI 回复回传依赖协议端上报自身消息**。Q→TG 方向靠 `@plugin.message` 事件；
   机器人自己发的消息只有协议端上报 `post_type=message_sent`
   （如 OneBot 实现里的「上报自身消息」开关、NeoBot 本地适配器）时才看得到。
   `/tg诊断` 会打印 `qq_bot_messages` 计数：长期为 0 就说明该通道没上报，
   这时 Telegram 侧只能看到群成员的消息——想让 AI 的话也过桥，请在协议端打开该开关。
   插件无法拦截宿主出站（`adapter.send` 由宿主独占），因此没有别的取词路径。
2. **Webhook 模式未实现**：插件拿不到宿主入站 HTTP 路由，只做长轮询；
   `protocol = webhook` 会记一条 warning 并按 polling 运行。
3. **Telegram 侧不能 @ QQ 用户**：桥过去的文本里 `@某人` 会变成普通文字（Telegram 的
   mention 需要 `tg://user?id=`，而 QQ 号与 Telegram user id 没有映射关系）。
4. **不是一等通道**：Telegram 会话不参与 NeoBot 的意愿判定、群名单、@ 必回、
   历史/记忆按会话隔离等逻辑——这些只对宿主适配器的通道生效。
5. **官方通道 openid**：绑定 QQ 侧时按文本 ID 处理（`group:openid字符串` 也支持），
   不要对它做 `int()`。

---

## 验证

```bash
cd neobot-plugin-telegram
# 下面 1/2/4 需要把 NeoBot 的源码包放进 PYTHONPATH（3 是纯 AST，不需要）
export PYTHONPATH=<NeoBot>/packages/modloader/src:<NeoBot>/packages/contracts/src:<NeoBot>/packages/adapter/src

# 1) 单元测试（57 项：纯逻辑 + 假宿主跑插件级）
python -m pytest tests -q

# 2) 宿主模拟器：打印 /help 命令展示页 + 跑通双向桥接（不需要 Telegram token）
python devtools/simulate.py

# 3) 静态兼容审计：对每个宿主版本核对 ctx / 命令注册 / 注入参数
python tools/check_compat.py --ref <宿主根目录> --ref <另一个宿主根目录>

# 4) 官方插件 SDK 自检与契约导出（neobot_modloader.sdk 是 2.1.0a15+ 才有）
python -m neobot_modloader.sdk check .
python -m neobot_modloader.sdk export .
```

`pytest` 的 `--import-mode=importlib` 已写进 `pyproject.toml`：插件根目录有 `__init__.py`，
默认的 prepend 模式会把整个目录当包导入并失败。

已实测结果见 [CHANGELOG.md](./CHANGELOG.md)。

## 目录结构

| 文件 | 职责 |
| --- | --- |
| `__init__.py` | Plugin 声明、配置模型、生命周期、命令注册、宿主消息订阅 |
| `tg_api.py` | Bot API 客户端：endpoint / 代理 / 重试退避 / 429 / 长轮询 / 文件下载 |
| `tg_model.py` | Update 解码：实体解析、媒体、相册聚合、命令与回调（纯函数） |
| `tg_send.py` | 出站渲染：HTML 转义、标签安全分段、媒体方法映射、按钮、发送计划（纯函数） |
| `tg_bridge.py` | 绑定存储、路由成型、回环防护（纯 stdlib） |
| `tests/` | 57 项单测（纯逻辑 + 用假宿主跑插件级） |
| `devtools/simulate.py` | 宿主模拟器（命令展示页 + 双向桥接） |
| `tools/check_compat.py` | 宿主 API 静态审计（多版本） |

## 许可

MIT
