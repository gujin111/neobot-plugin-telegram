# 自建反代（可选）

插件**默认已经走内置反代**，所以大多数情况什么都不用做。
想用自己的域名/Worker 时按本文做，步骤约 2 分钟。

## 为什么要反代

`api.telegram.org` 在国内多数网络不可直连；反代就是一台"能连 Telegram"的中转，
插件把 Bot API 请求发给它，它再转发给 Telegram。Cloudflare Worker 免费额度对个人机器人足够。

## 用 Cloudflare Worker（推荐）

1. <https://dash.cloudflare.com> → Workers & Pages → Create → Worker；
2. 把 [`cloudflare-worker.js`](./cloudflare-worker.js) 整体粘进去 → Deploy；
3. Settings → Variables and Secrets 加两个（强烈建议都加，加完再 Deploy 一次）：

   | 变量 | 作用 | 例子 |
   | --- | --- | --- |
   | `PATH_SECRET` | URL 路径密钥 | `v1-9f3a7c2e` |
   | `PROXY_KEY` | 请求头密钥（`X-Proxy-Key`） | `pk-9f3a7c2e` |

4. 插件配置里改成你自己的：

   ```toml
   reverse_proxy_url = "https://<你的worker>.workers.dev"
   reverse_proxy_secret = "v1-9f3a7c2e"
   proxy_key = "pk-9f3a7c2e"
   ```

   > 有自定义域名的话绑上去，然后把 `reverse_proxy_url` 换成人域名——`*.workers.dev` 在国内常被 SNI 阻断。

5. 验证（把两个密钥与 token 换成你自己的）：

   ```bash
   curl -sS -m 10 -H "X-Proxy-Key: pk-9f3a7c2e" \
     https://<你的worker>/v1-9f3a7c2e/bot<TOKEN>/getMe
   ```

   返回 `{"ok":true,...}` 即通；`403 forbidden` = `PROXY_KEY` 不匹配；`404` = 路径密钥不对或没带。

## 用 Nginx（已有公网服务器）

关键是**两条路径都要转发**，并把 `X-Proxy-Key` 当普通头透传：

```nginx
location /bot      { proxy_pass https://api.telegram.org; proxy_read_timeout 90s; }
location /file/bot { proxy_pass https://api.telegram.org; proxy_read_timeout 300s; }
```

想加头校验的话在 `location` 里加：

```nginx
if ($http_x_proxy_key != "pk-9f3a7c2e") { return 403; }
```

## 本地 Bot API server

纯内网也能用：起一个 `telegram-bot-api`，然后 `reverse_proxy_url = ""`、
`endpoint = "http://127.0.0.1:8081"`。注意它自己仍需连得上 Telegram 的 443，
所以解决不了"机器本身出不去"的问题。

## 改完怎么确认

保存配置 → 面板重载插件 → `/tg诊断`：

```
· endpoint：https://你的域名/***（来源：反代）
· proxy_key：pk-***
· 连通性探测：通 —— @你的bot（xx ms）
```

启动失败也会每 15 秒自动复检，通了就自己接上轮询，不用反复重启。
