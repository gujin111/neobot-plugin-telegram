/**
 * Telegram Bot API 反向代理（Cloudflare Worker，免费额度足够个人机器人用）
 *
 * 用途：容器/服务器无法直连 api.telegram.org 时，把插件配置的 endpoint 指向这里，
 * 不需要任何本地代理软件。
 *
 * 部署（约 2 分钟）：
 *   1. https://dash.cloudflare.com → Workers & Pages → Create → Worker
 *   2. 把本文件内容整体粘进去，Deploy
 *   3. （强烈建议）Settings → Variables and Secrets → 新增 PATH_SECRET，
 *      值填一串随机字符，例如 v1-9f3a7c2e；Deploy 生效
 *   4. 记下地址，按下面的规则填插件配置
 *
 * 插件配置（plugins_data/telegram/config.toml）：
 *   设了 PATH_SECRET 时：
 *     endpoint = "https://<你的worker域名>/v1-9f3a7c2e"
 *   没设 PATH_SECRET 时：
 *     endpoint = "https://<你的worker域名>"
 *
 * 它会转发：
 *   POST /bot<token>/<method>        → https://api.telegram.org/bot<token>/<method>
 *   GET  /file/bot<token>/<file_path> → https://api.telegram.org/file/bot<token>/<file_path>
 *
 * 说明：
 *   - 只允许这两类路径，其它一律 404，避免被当成开放代理；
 *   - 不记录、不缓存请求内容（token 在 URL 里，日志里不要打 URL）；
 *   - 请求体原样流式转发，图片/文件上传与下载都能用（单次上限受 Cloudflare 套餐限制）。
 */

export default {
  async fetch(request, env) {
    const method = request.method;
    if (method !== "GET" && method !== "POST" && method !== "HEAD") {
      return new Response("method not allowed", { status: 405 });
    }

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
    const contentLength = request.headers.get("content-length");
    if (contentLength) headers["content-length"] = contentLength;

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

    const responseHeaders = {
      "content-type": upstream.headers.get("content-type") || "application/json",
      "cache-control": "no-store",
    };
    return new Response(upstream.body, { status: upstream.status, headers: responseHeaders });
  },
};
