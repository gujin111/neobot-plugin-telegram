# 网络：让容器里的 NeoBot 连上 Telegram

容器里 `127.0.0.1` 指向容器自己，所以宿主上的代理必须用**宿主地址**访问。
本文给三种能用的做法，任选其一。

## 第 0 步：先让脚本帮你找（最省事）

把插件目录里的 `extras/find-proxy.py` 拷进容器（或直接 `docker cp` 进去），然后：

```bash
python3 extras/find-proxy.py
```

它会遍历 `host.docker.internal`、容器默认网关、常见 docker 网桥和本机地址，
扫一遍常见代理端口，再**真发一次** `https://api.telegram.org/bot0:0/getMe` 验证隧道，
最后直接打印：

```
  proxy = "http://172.17.0.1:7890"
```

把这行填进 `plugins_data/telegram/config.toml`（或面板「插件 → 配置 → telegram」），重载插件即可。
它找不到东西时，说明宿主上要么没有代理、要么代理只监听 127.0.0.1 —— 看下面方案 A 或 B。

## 先确定"宿主地址"

| 你的环境 | 容器里应该写的宿主地址 |
| --- | --- |
| Docker Desktop（Windows / macOS） | `host.docker.internal`（开箱可用） |
| Docker Engine on Linux | 启动容器时加 `--add-host=host.docker.internal:host-gateway`，然后用 `host.docker.internal` |
| 其它容器运行时（podman / 群晖 / 宝塔面板） | 宿主局域网 IP（`ipconfig` / `ip a` 里 192.168.x.x 或 10.x.x.x），或 docker 网桥网关 |

查 docker 网桥网关：

```bash
docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}'
# 常见输出 172.17.0.1
```

## 方案 A：宿主上已经有代理（Clash / v2rayN / sing-box）

1. 让代理**对外监听**（默认只听 127.0.0.1，容器连不上）：
   - Clash / mihomo：配置里 `allow-lan: true`、`bind-address: '*'`
   - Clash Verge：设置 → 打开「允许局域网连接」
   - v2rayN：参数设置 → 勾选「允许来自局域网的连接」
   - sing-box：inbound 的 `listen` 改成 `"0.0.0.0"`
2. 放行防火墙入站端口（Windows Defender 防火墙 → 入站规则 → 新建，TCP 7890）。
3. 插件配置：

   ```toml
   proxy = "http://host.docker.internal:7890"    # Linux 上换成宿主 IP 或 172.17.0.1
   ```

4. 在**容器里**验证（把地址换成你的）：

   ```bash
   curl -sS -m 10 -x http://host.docker.internal:7890 \
     https://api.telegram.org/bot<TOKEN>/getMe
   ```

   返回 `{"ok":true,...}` 才算通；`Could not connect to proxy` = 代理没监听对外地址或端口没放行。

## 方案 B：没有代理 → 自建反代（推荐，不需要本地代理软件）

用 Cloudflare Worker 做一个只转发 Bot API 的反代，免费额度对个人机器人足够。

1. 打开 <https://dash.cloudflare.com> → Workers & Pages → Create → Worker。
2. 把 [`extras/cloudflare-worker.js`](./cloudflare-worker.js) 的内容整体粘进去 → Deploy。
3. 建议加一个路径密钥：Settings → Variables and Secrets → 新增 `PATH_SECRET`
   （例如 `v1-9f3a7c2e`），再 Deploy。这样别人拿到你的 worker 域名也不能白用。
4. 插件配置（**只填两行，插件自己拼 URL**）：

   ```toml
   reverse_proxy_url = "https://<你的worker>.workers.dev"
   reverse_proxy_secret = "v1-9f3a7c2e"    # Worker 没设 PATH_SECRET 就留空
   ```

   插件会拼成 `https://<worker>/v1-9f3a7c2e/bot<token>/<method>`；密钥在日志与诊断里打码。
   也支持老写法：`endpoint = "https://<worker>/v1-9f3a7c2e"`（二选一，反代字段优先）。

5. 验证：

   ```bash
   curl -sS -m 10 https://<你的worker>/bot<TOKEN>/getMe
   ```

已经有公网 Nginx 的话也可以直接反代，关键是**两条路径都要转发**：

```nginx
location /bot { proxy_pass https://api.telegram.org; proxy_read_timeout 90s; }
location /file/bot { proxy_pass https://api.telegram.org; proxy_read_timeout 300s; }
```

## 方案 C：本地 Bot API server（tdlib）

纯内网方案：起一个 `telegram-bot-api`，把 `endpoint` 指向它（例如 `http://127.0.0.1:8081`）。
注意它自己仍然需要能连上 Telegram 的 443，所以本质上解决不了翻墙问题——除非这台机器本身能通。

## 填完之后

保存配置 → 面板重载插件（或 QQ 里发 `/tg重连`）→ `/tg诊断` 会做一次真实 `getMe` 并给结论。
1.0.1 起启动失败会自动每 15 秒复检，连通后自己接上轮询，不用反复重启。
