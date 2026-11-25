#!/usr/bin/env python3
"""在 NeoBot 容器里运行：自动找出可用的「宿主代理地址」。

用法（容器内，不需要任何依赖）：

    python3 extras/find-proxy.py            # 自动探测
    python3 extras/find-proxy.py --probe 172.17.0.1:7890   # 只验证一个地址

它会做三件事：
  1. 收集候选宿主地址：host.docker.internal、容器默认网关、常见 docker 网桥、局域网地址；
  2. 对每个候选地址扫常见代理端口；
  3. 对连通的地址**真发一次 HTTPS 请求到 api.telegram.org**（走代理），
     能拿到 HTTP 响应（哪怕是 401/404）就说明隧道通了。

最后打印一行可直接粘进 plugins_data/telegram/config.toml 的 `proxy = "..."`。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import urllib.error
import urllib.request

PORTS = [7890, 7891, 7897, 7898, 1080, 10808, 10809, 20171, 20172, 3128, 8889, 8118]
PROBE_URL = "https://api.telegram.org/bot0:0/getMe"
TIMEOUT = 8


def default_gateway() -> str:
    """从 /proc/net/route 取默认网关（Linux 容器）。"""
    try:
        with open("/proc/net/route", encoding="ascii") as handle:
            next(handle)
            for line in handle:
                fields = line.split()
                if len(fields) < 3 or fields[1] != "00000000":
                    continue
                raw = fields[2]
                octets = [str(int(raw[index : index + 2], 16)) for index in (6, 4, 2, 0)]
                return ".".join(octets)
    except Exception:  # noqa: BLE001
        pass
    return ""


def candidate_hosts() -> list[tuple[str, str]]:
    hosts: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(label: str, host: str) -> None:
        host = (host or "").strip()
        if not host or host in seen:
            return
        seen.add(host)
        hosts.append((label, host))

    add("环境变量 HOST_PROXY_HOST", os.environ.get("HOST_PROXY_HOST", ""))

    # host.docker.internal 在 Docker Desktop 里可直接解析
    try:
        resolved = socket.gethostbyname("host.docker.internal")
        add("host.docker.internal", resolved)
    except OSError:
        pass

    gateway = default_gateway()
    if gateway:
        add("容器默认网关", gateway)

    for index in range(4):
        add(f"docker 网桥 172.{17 + index}.0.1", f"172.{17 + index}.0.1")

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                add("容器自身地址", ip)
    except OSError:
        pass

    return hosts


def port_open(host: str, port: int, timeout: float = 0.8) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        return sock.connect_ex((host, port)) == 0
    finally:
        sock.close()


def proxy_works(proxy: str) -> tuple[bool, str]:
    """走代理请求 Telegram；拿到任何 HTTP 响应就说明隧道通。"""
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    )
    request = urllib.request.Request(PROBE_URL, headers={"User-Agent": "neobot-proxy-check"})
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            body = response.read(200).decode("utf-8", "replace")
            return True, f"HTTP {response.status} {body[:80]}"
    except urllib.error.HTTPError as exc:  # 401/404 也算通
        body = exc.read(200).decode("utf-8", "replace")
        return True, f"HTTP {exc.code} {body[:80]}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc)[:100]}"


def main() -> int:
    parser = argparse.ArgumentParser(description="找出容器可用的宿主代理地址")
    parser.add_argument("--probe", help="只验证这个地址，例如 172.17.0.1:7890")
    parser.add_argument("--ports", help="逗号分隔的端口列表（默认常见代理端口）")
    args = parser.parse_args()
    ports = [int(item) for item in args.ports.split(",") if item.strip()] if args.ports else PORTS

    print("== 候选 ==")
    if args.probe:
        targets = [("手工指定", args.probe)]
    else:
        targets = []
        for label, host in candidate_hosts():
            print(f"  {label}: {host}")
            for port in ports:
                targets.append((label, f"{host}:{port}"))

    print("\n== 扫描并验证 ==")
    working: list[str] = []
    for label, target in targets:
        if ":" not in target:
            continue
        host, _, port_text = target.rpartition(":")
        try:
            port = int(port_text)
        except ValueError:
            continue
        if not port_open(host, port):
            continue
        proxy = f"http://{target}"
        ok, detail = proxy_works(proxy)
        print(f"  {'PASS' if ok else 'FAIL'} {proxy:32} {detail}")
        if ok:
            working.append(proxy)

    print("\n== 结论 ==")
    if working:
        print("可以填（plugins_data/telegram/config.toml，或面板「插件 → 配置 → telegram」）：\n")
        print(f'  proxy = "{working[0]}"\n')
        if len(working) > 1:
            print("其它可用：" + "、".join(working[1:]))
        print("\n然后重启插件或发 /tg重连；/tg诊断 会再探一次。")
        return 0

    print("没找到可用代理。两种走法：")
    print("  1) 宿主上有代理但没对外监听：改 allow-lan / 0.0.0.0，并放行防火墙端口；")
    print("  2) 没有代理：用 extras/cloudflare-worker.js 部署一个反代，把 endpoint 指过去。")
    print("候选地址与端口可以手工指定再试：")
    print("  python3 extras/find-proxy.py --probe <宿主IP>:7890")
    return 1


if __name__ == "__main__":
    sys.exit(main())
