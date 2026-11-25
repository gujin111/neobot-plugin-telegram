# telegram — 让 NeoBot 在 Telegram 里和用户对话

把 Koishi 的 [`@koishijs/plugin-adapter-telegram`](https://github.com/satorijs/satori/tree/main/adapters/telegram)
的协议能力移植到 [NeoBot](https://github.com/SuperQuail/NeoBot) 上，做成**纯 Telegram 前端**：

```
Telegram 用户 → 插件收消息 → 宿主 AI 生成 → 文本发回 Telegram
```

- **不再有任何 QQ 桥接**（2.0.0 起移除），命令只保留状态查询
- 目标宿主：**NeoBot v2.1.0a16**（同时验证 2.0.1 / a9 / a8 / a7）
- 不改 NeoBot 本体，只用 `neobot_modloader` 公开 API
- 只依赖宿主已有的 `httpx` / `pydantic`

## 它怎么把消息变成 AI 回复

NeoBot 的回复管线以「会话」为单位，插件通过宿主服务
`reply_orchestrator.start_background_reply(...)` 把一条内容推进管线，然后读回
`ReplyEvent.generated_text`（AI 生成的原文），再发回 Telegram。

所以需要一个 **AI 工作区**：一个 QQ 群或私聊，AI 在那里生成回复。

> ⚠️ 宿主会把 AI 的回复**也发到这个 QQ 工作区**（出站发送由宿主掌控，插件拦不住）。
> 建议填你自己一个小号/小群，别用主群。多个 Telegram 会话会共用同一个工作区，
> 上下文会混在一起——单人自用没问题。

## 配置

```toml
# 必填
token = "123456:ABC-DEF..."          # BotFather 给的
ai_endpoint_kind = "private"         # private 私聊 / group 群聊
ai_endpoint_id = "10001"             # 对应 QQ 号（私聊）或群号（群聊）

# 网络（国内通常必填其一）
reverse_proxy_url = "https://tg.你的域名"   # 反代，推荐
reverse_proxy_secret = "v1-xxxxxxxx"        # 与 Worker 的 PATH_SECRET 一致
proxy = ""                                   # 用反代时留空；也可写 http://… / socks5://… / auto
```

完整配置项：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 总开关 |
| `token` | 空 | 机器人令牌 |
| `endpoint` | `https://api.telegram.org` | 直连地址 |
| `reverse_proxy_url` / `reverse_proxy_secret` | 空 | 反代基址与路径密钥，优先级高于 `endpoint` |
| `proxy` | 空 | `http://…` / `socks5://…`（内置握手）/ `auto`（自动探测宿主端口） |
| `polling_timeout` / `retry_times` / `retry_interval` | 25 / 6 / 5.0 | 长轮询与重连 |
| `allowed_updates` | 空=全部 | 留空即包含 `chat_member` |
| `parse_mode` / `link_preview` / `chunk_size` | html / true / 3500 | 出站渲染 |
| `ignore_telegram_bots` | `true` | 忽略其它机器人 |
| `ai_endpoint_kind` / `ai_endpoint_id` | private / 空 | AI 工作区（见上） |
| `ai_channel` | 空 | `onebot` / `official`，留空让宿主判断 |
| `ai_prefix` | `[TG {name}] ` | 交给 AI 时的前缀，支持 `{name}` `{chat}` `{id}` |
| `ai_timeout` | 90 | 等 AI 生成的超时（秒） |
| `ai_error_reply` | `（AI 暂时没有响应…）` | 没产出时回给用户的文案，留空则不回 |

## 命令（只有两条）

| 命令 | 别名 | 说明 |
| --- | --- | --- |
| `/tg` | `telegram`、`电报` | 运行状态：连接、终结点、代理、AI 工作区、计数器 |
| `/tg诊断` | `tgdiag`、`电报诊断` | 链路诊断：token 打码、反代/代理、轮询统计、编排器可用性、真实 `getMe` 探测 |

重连是自动的：启动失败会每 `max(15s, retry_interval×3)` 复检，连通后自动接上轮询，不需要手动命令。

## 安装

1. 把整个目录复制到 NeoBot 数据目录的 `plugins/` 下，目录名改成 `telegram`：

   ```text
   <NeoBot 数据目录>/plugins/telegram/plugin.toml
   ```

2. 重启 NeoBot，或在面板「插件管理」里加载；
3. 面板「插件 → 配置 → telegram」填 `token`、`ai_endpoint_id`，以及网络（反代或代理）；
4. 在 Telegram 里给机器人发一句话，应该能收到 AI 的回复。

## 网络：连不上 api.telegram.org 怎么办

| 情况 | 怎么配 |
| --- | --- |
| 有代理软件 | `proxy = "http://127.0.0.1:7890"`（同机）；容器里用宿主地址或 `proxy = "auto"` |
| 有域名 | 按 [`extras/README.md`](extras/README.md) 部署 Worker，填 `reverse_proxy_url` + `reverse_proxy_secret` |
| 服务器能直连 | 两个都留空 |

`socks5://` 由插件内置实现（[tg_proxy.py](tg_proxy.py)），**不需要 `socksio`**：插件在 `127.0.0.1`
起一个随机端口的 CONNECT 隧道交给 httpx，用完即关。

## 与 Koishi 原版的差异

| 能力 | Koishi 4.5.11 | 本插件 |
| --- | --- | --- |
| 长轮询 / 自动摘 webhook / 退避重连 | ✅ | ✅ |
| `allowed_updates` | 探活传空列表 → 收不到 `chat_member` | ✅ 默认含全集 |
| 429 限流 | 按重试间隔 | ✅ 读 `retry_after` |
| 实体解析 / 相册聚合 | ✅ | ✅（相册不阻塞轮询） |
| 超长文本 | 直接报错 | ✅ 标签安全分段 |
| 纯按钮消息 | 漏发 | ✅ 会发 |
| `entities: []` | 抛 TypeError | ✅ 安全 |
| Webhook | ✅ | ❌ 未实现（插件拿不到宿主入站路由） |
| QQ 桥接 | — | ❌ 2.0.0 起移除（改为 TG 直连 AI） |

## 验证

```bash
cd neobot-plugin-telegram
export PYTHONPATH=<NeoBot>/packages/modloader/src:<NeoBot>/packages/contracts/src:<NeoBot>/packages/adapter/src

python -m pytest tests -q          # 57 项
python devtools/simulate.py        # 宿主模拟器：命令页 + AI 直答链路
python tools/check_compat.py --ref <宿主根目录>
python -m neobot_modloader.sdk check .   # 2.1.0a15+ 才有
```

## 已知限制

1. **AI 回复会同时出现在 QQ 工作区**（宿主行为），所以工作区别用主群；
2. **多个 TG 会话共用同一个工作区**，AI 上下文是混的（建议单人/小范围使用）；
3. 同一 TG 会话内消息**串行**处理（插件加锁），并对宿主返回 `None`（已有回复在进行）计数 `ai_busy`；
4. Webhook 未实现，只做长轮询；
5. Telegram 侧无法 @ QQ 用户（没有 ID 映射，也不需要了）。

## 许可

MIT
