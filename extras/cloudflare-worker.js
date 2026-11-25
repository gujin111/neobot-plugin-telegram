/**
 * Telegram Bot API 反向代理（Cloudflare Worker，免费额度足够个人机器人用）
 *
 * 用途：容器/服务器无法直连 api.telegram.org 时，把插件配置的 endpoint 指向这里。
 *
 * 部署（约 2 分钟）：
 *   1. https://dash.cloudflare.com → Workers & Pages → Create → Worker
 *   2. 把本文件内容整体粘进去，Deploy
 *   3. Variables and Secrets 里加两个（强烈建议都加）：
 *        PATH_SECRET = v1-xxxxxxxx    路径密钥（插件里填 reverse_proxy_secret）
 *        PROXY_KEY   = pk-xxxxxxxx    请求头密钥（插件里填 proxy_key）
 *   4. 再 Deploy 一次生效
 *
 * 鉴权逻辑：
 *   - 配了 PROXY_KEY：请求必须带 `X-Proxy-Key: <同一个值>`，否则 403；
 *   - 配了 PATH_SECRET：URL 必须是 `/<PATH_SECRET>/bot<token>/…`，否则 404。
 *
 * 只转发：
 *   POST /bot<token>/<method>         → https://api.telegram.org/bot<token>/<method>
 *   GET  /file/bot<token>/<file_path> → https://api.telegram.org/file/bot<token>/<file_path>
 * 其它路径一律 404，不记录、不缓存请求内容。
 */

export default {
  async fetch(request, env) {
    const method = request.method;
    if (method !== "GET" && method !== "POST" && method !== "HEAD") {
      return new Response("method not allowed", { status: 405 });
    }

    // 第一道：请求头密钥（防止别人拿到地址就白蹭）
    const requiredKey = String(env.PROXY_KEY || "").trim();
    if (requiredKey && request.headers.get("x-proxy-key") !== requiredKey) {
      return new Response("forbidden", { status: 403 });
    }

    // 第二道：路径密钥
    const url = new URL(request.url);
    let path = url.pathname;
    const secret = String(env.PATH_SECRET || "").trim();
    if (secret) {
      const prefix = "/" + secret.replace(/^\/+|\/+$/g, "");
      if (!path.startsWith(prefix + "/")) {
        return new Response("not found", { status: 404 });
      }
      path = path.slice(prefix.length);
    }

    if (!path.startsWith("/bot") && !path.startsWith("/file/bot")) {
      return new Response("not found", { status: 404 });
    }

    const headers = {};
    const contentType = request.headers.get("content-type");
    if (contentType) headers["content-type"] = contentType;

    const init = { method, headers, redirect: "follow" };
    if (method === "POST") {
      init.body = request.body;
    }

    let upstream;
    try {
      upstream = await fetch("https://api.telegram.org" + path + url.search, init);
    } catch (error) {
      return new Response(JSON.stringify({ ok: false, description: "upstream unreachable" }), {
        status: 502,
        headers: { "content-type": "application/json" },
      });
    }

    return new Response(upstream.body, {
      status: upstream.status,
      headers: {
        "content-type": upstream.headers.get("content-type") || "application/json",
        "cache-control": "no-store",
      },
    });
  },
};
