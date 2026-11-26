# telegram — 让 NeoBot 在 Telegram 里当个独立 Bot

纯 Telegram 前端：**TG 用户直接和 AI 聊天，整个链路不涉及任何 QQ 会话**。

```
Telegram 用户 → 插件收消息 → 宿主的主模型服务（provider）→ 文本发回 Telegram
```

- 不经过宿主的 QQ 回复管线，不需要 QQ 群/号，不往 QQ 发任何东西
- **默认走内置反代**（`https://tg.gujin99.xyz`）：新装只需填 `token`，国内也不用配代理/域名
- 上下文由插件按 **TG 会话**自己维护（内存，默认 12 条）
- 命令只保留状态查询：`/tg`、`/tg诊断`
- 目标宿主：NeoBot **v2.1.0a16**（同时验证 2.0.1 / a9 / a7）
- 只依赖宿主已有的 `httpx` / `pydantic`

## 两种回复模式（关键选择）

| | `reply_mode = "chat"`（默认） | `reply_mode = "agent"` |
| --- | --- | --- |
| 谁生成回复 | 插件直接调宿主模型 | **宿主的完整回复管线** |
| 人格/提示词 | ✅ 沿用宿主的昵称与人格 | ✅ 宿主自己装配（含技能说明） |
| **工具 / 技能 / Agent** | ❌ 没有 | ✅ 全部生效（联网搜索、生图、解题、跨聊天…） |
| **记忆 / 画像 / 群档案** | ❌ 没有（插件自己存 12 轮上下文） | ✅ 走宿主的记忆体系 |
| 回复发到哪 | 只发 TG | 也会发到配置的 **QQ 工作区**（宿主出站，插件拦不住） |
| TG 用户之间 | 各自独立上下文 | **共享同一个工作区**（上下文与记忆是同一个会话的） |

配置 `agent` 模式：

```toml
reply_mode = "agent"
agent_endpoint_kind = "group"     # group 群聊 / private 私聊
agent_endpoint_id = "123456789"   # 你的一个 QQ 群号或 QQ 号，作为"工作区"
agent_channel = ""                # onebot / official，留空让宿主判断
agent_prefix = "[TG {name}] "     # 让模型知道是谁在说话
agent_timeout = 300.0             # 等整轮（含工具调用）完成
agent_queue_wait = 30.0           # 工作区忙时排队等待秒数
```

> **代价要清楚**：宿主的工具与记忆都是以「QQ 会话」为单位的，所以 agent 模式必须借一个
> QQ 会话当工作区——AI 的回复**同时**会出现在那个 QQ 会话里，而且所有 TG 用户共享它。
> 想要"TG 完全对等、不碰 QQ"，需要宿主层面多一个 Telegram 适配器（那属于改 NeoBot 本体，
> 不是插件能做到的）。

## 代码结构（4 个文件）

| 文件 | 职责 |
| --- | --- |
| [`__init__.py`](__init__.py) | 插件定义、配置、生命周期、两条命令、AI 对话编排 |
| [`tg_api.py`](tg_api.py) | Bot API 客户端与长轮询（重试 / 429 / 请求头） |
| [`tg_model.py`](tg_model.py) | Update 解码：拍平成事件对象 |
| [`tg_send.py`](tg_send.py) | 出站文本：HTML 转义 + 分段 |

三个 `tg_*.py` 不 import 任何 NeoBot 模块，可以单独测；`extras/` 只有自建反代用的 Worker 与说明。

## 它是怎么回话的

宿主的模型配置在面板里（`[models]`），运行时注册成 `provider` 服务，接口是：

```python
async def chat(messages: list[dict], tools=None) -> dict   # {"role": "assistant", "content": "..."}
```

插件做的事就是：**系统提示词** + 该 TG 会话的历史 + 当前消息 → `provider.chat(...)` → 把 `content` 发回 TG。

系统提示词默认**沿用你在 NeoBot 里已经写好的东西**（`prompt_source = "host"`）：

```
<你是谁>                     ← 宿主昵称（bot_nick_name）+ 人格（bot.bot_data）
  你的名字是小九。
  我是一个喜欢聊天的猫娘。
</你是谁>
…（host_prompt_sections 里指定的 prompts.toml 分区，可选）
…（system_prompt 补充提示词，可选）
<对话方式>                    ← 插件补的 TG 规则：直接输出正文、不要调工具
```

模型也是面板里配的那个，不用另填 API Key。

## 配置

```toml
# 只需要这一行：默认已走内置反代，国内无需代理、无需域名
token = "123456:ABC-DEF..."

# 以下都可选
system_prompt = ""                    # 补充提示词（host 模式下追加；custom 模式下就是全部）
prompt_source = "host"                 # host = 沿用宿主昵称/人格；custom = 只用 system_prompt
host_prompt_sections = []              # 额外拼接的 prompts.toml 分区，如 ["gentle_language"]
history_limit = 12                    # 每个 TG 会话保留几条历史（0 = 无上下文）
ai_timeout = 120.0                    # 单次模型调用超时（秒）
ai_error_reply = "（AI 暂时没有响应，请稍后再试）"
```

内置的反代是这三个默认值（想自己直连或自建反代时改它们）：

```toml
endpoint = "https://api.telegram.org"        # 反代留空时才用
reverse_proxy_url = "https://tg.gujin99.xyz" # 清空它 → 走 endpoint / proxy
reverse_proxy_secret = "v1-a5e68cb17320"     # 路径密钥
proxy_key = "pk-a5e68cb17320"                # 请求头密钥（X-Proxy-Key），不匹配 403
proxy = ""                                    # 也可写 http://127.0.0.1:7890（SOCKS 用其 HTTP 端口）
```

> 内置反代做了两道校验：URL 里的路径密钥 + 请求头 `X-Proxy-Key`。
> 只拿到地址、没有请求头密钥的请求会被 Worker 直接 403，不会转发到 Telegram。

其余配置项：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 总开关 |
| `endpoint` | `https://api.telegram.org` | 直连地址 |
| `reverse_proxy_url` / `reverse_proxy_secret` | 空 | 反代基址与路径密钥，优先级高于 `endpoint` |
| `proxy` | 空 | http(s) 代理，例如 `http://127.0.0.1:7890`（SOCKS 请用代理软件的 HTTP 端口） |
| `polling_timeout` / `retry_times` / `retry_interval` | 25 / 6 / 5.0 | 长轮询与重连 |
| `allowed_updates` | 空=全部 | 留空即包含 `chat_member` |
| `parse_mode` / `link_preview` / `chunk_size` | html / true / 3500 | 出站渲染 |
| `ignore_telegram_bots` | `true` | 忽略其它机器人 |
| `prompt_source` | `host` | `host` = 沿用宿主昵称/人格；`custom` = 只用 `system_prompt` |
| `system_prompt` | 空 | 补充提示词；`custom` 模式下是全部 |
| `host_prompt_sections` | `[]` | 额外拼接的 `prompts.toml` 分区名 |
| `history_limit` | `12` | 每个 TG 会话保留的历史条数（0 = 无上下文） |
| `ai_timeout` | `120` | 单次模型调用超时（秒） |
| `ai_error_reply` | `（AI 暂时没有响应…）` | 模型失败时的兜底文案，留空则不回复 |

## 命令（只有两条）

| 命令 | 别名 | 说明 |
| --- | --- | --- |
| `/tg` | `telegram`、`电报` | 运行状态：连接、终结点、代理、模型服务、上下文与计数器 |
| `/tg诊断` | `tgdiag`、`电报诊断` | 链路诊断：token 打码、反代/代理、轮询统计、`provider` 可用性、真实 `getMe` 探测 |

重连是自动的：启动失败会每 `max(15s, retry_interval×3)` 复检，连通后自动接上轮询。

## 安装

1. 把整个目录复制到 NeoBot 数据目录的 `plugins/` 下，目录名改成 `telegram`：

   ```text
   <NeoBot 数据目录>/plugins/telegram/plugin.toml
   ```

2. 重启 NeoBot，或在面板「插件管理」里加载；
3. 面板「插件 → 配置 → telegram」填 `token` 与网络（反代或代理）；
4. **确认 NeoBot 自己的模型已配好**（面板「模型」里能正常对话），否则插件会提示没有可用的 provider；
5. 在 Telegram 里给机器人发一句话。

## 网络：连不上 api.telegram.org 怎么办

| 情况 | 怎么配 |
| --- | --- |
| 有代理软件 | `proxy = "http://127.0.0.1:7890"`（同机）；容器里填宿主的局域网地址 |
| 有域名 | 按 [`extras/README.md`](extras/README.md) 部署 Worker，填 `reverse_proxy_url` + `reverse_proxy_secret` |
| 服务器能直连 | 把 `reverse_proxy_url` 清空 |

> 自带 SOCKS5 客户端已移除（3.0.0 精简）：`proxy` 直接交给 httpx，
> 所以只支持 `http://` / `https://` 代理。Clash / mihomo / sing-box 都同时提供 HTTP 端口
> （Clash 的 `mixed-port` 默认 7890），填它即可；纯 SOCKS 端口请先打开 HTTP 监听。

## 与 Koishi 原版的差异

| 能力 | Koishi 4.5.11 | 本插件 |
| --- | --- | --- |
| 长轮询 / 自动摘 webhook / 退避重连 | ✅ | ✅ |
| `allowed_updates` | 探活传空列表 → 收不到 `chat_member` | ✅ 默认含全集 |
| 429 限流 | 按重试间隔 | ✅ 读 `retry_after` |
| 实体解析 / 富文本还原 | ✅ | ❌ 只取纯文本交给模型 |
| 相册聚合 | ✅ | ❌ 每条独立处理（都变成占位文本） |
| 超长文本 | 直接报错 | ✅ 按段落安全分段 |
| 媒体/按钮发送 | ✅ | ❌ 只发文本（AI 回复用不到） |
| `entities: []` | 抛 TypeError | ✅ 安全 |
| Webhook | ✅ | ❌ 未实现（插件拿不到宿主入站路由） |
| QQ 相关 | — | ❌ 完全不做（2.1.0 起彻底移除） |

## 验证

```bash
cd neobot-plugin-telegram
export PYTHONPATH=<NeoBot>/packages/modloader/src:<NeoBot>/packages/contracts/src:<NeoBot>/packages/adapter/src

python -m pytest tests -q          # 46 项
python devtools/simulate.py        # 宿主模拟器：命令页 + 独立 TG 对话 + 上下文
python tools/check_compat.py --ref <宿主根目录>
python -m neobot_modloader.sdk check .   # 2.1.0a15+ 才有
```

## 已知限制

1. **不带宿主的工具/技能/记忆**：人格与昵称沿用宿主（`prompt_source = "host"`），但没有工具、记忆与 QQ 侧的一切；
2. **上下文在内存里**：重启即清空，且每个 TG 会话（私聊按用户、群聊按群）一份；
3. 群聊里所有成员共用同一个上下文；
4. 同一会话串行处理（插件加锁），模型慢时会排队；
5. Webhook 未实现，只做长轮询；
6. 只发文本（图片/文件等媒体入站会被转成 `[photo]` 之类的占位文本）。

## 许可

MIT
