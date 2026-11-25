"""内置代理：SOCKS5 客户端、本地 CONNECT 网关、auto 探测。

用假 SOCKS5 服务器做端到端验证，不需要真的 Clash。
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from tg_proxy import (
    ProxyUnavailable,
    SocksGateway,
    candidate_hosts,
    detect_proxy,
    parse_proxy,
    socks5_open,
)


def run(coro):
    return asyncio.run(coro)


# ── 解析 ──


def test_parse_proxy_variants():
    http = parse_proxy("http://127.0.0.1:7890")
    assert (http.kind, http.host, http.port) == ("http", "127.0.0.1", 7890)
    assert http.http_url() == "http://127.0.0.1:7890"

    socks = parse_proxy("socks5://clash.local:1080")
    assert (socks.kind, socks.host, socks.port) == ("socks5", "clash.local", 1080)

    bare = parse_proxy("127.0.0.1:7897")  # 省略协议默认 http
    assert (bare.kind, bare.port) == ("http", 7897)

    auth = parse_proxy("socks5://user:pwd@10.0.0.2:1080")
    assert (auth.username, auth.password) == ("user", "pwd")
    assert auth.http_url() == "http://user:pwd@10.0.0.2:1080"

    ipv6 = parse_proxy("http://[::1]:7890")
    assert (ipv6.host, ipv6.port) == ("::1", 7890)

    for empty in ("", "   ", "none", "OFF", "direct"):
        assert parse_proxy(empty) is None

    with pytest.raises(ProxyUnavailable):
        parse_proxy("http://127.0.0.1:not-a-port")


# ── 假 SOCKS5 服务器 ──


async def fake_socks(*, require_auth: bool = False, user: str = "u", password: str = "p"):
    """握手后把收到的字节原样回显，用来验证隧道。"""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readexactly(2)
            await reader.readexactly(head[1])  # 客户端给出的认证方式
            if require_auth:
                writer.write(b"\x05\x02")
                await writer.drain()
                sub = await reader.readexactly(2)
                username = (await reader.readexactly(sub[1])).decode()
                length = (await reader.readexactly(1))[0]
                supplied = (await reader.readexactly(length)).decode()
                ok = username == user and supplied == password
                writer.write(b"\x01" + (b"\x00" if ok else b"\x01"))
                await writer.drain()
                if not ok:
                    return
            else:
                writer.write(b"\x05\x00")
                await writer.drain()
            request = await reader.readexactly(4)
            atyp = request[3]
            if atyp == 1:
                await reader.readexactly(4)
            elif atyp == 3:
                size = (await reader.readexactly(1))[0]
                await reader.readexactly(size)
            await reader.readexactly(2)
            writer.write(b"\x05\x00\x00\x01" + b"\x00" * 4 + b"\x00\x00")
            await writer.drain()
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    break
                writer.write(chunk)
                await writer.drain()
        except Exception:  # noqa: BLE001 - 测试替身
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


# ── SOCKS5 直连 ──


def test_socks5_open_echo_roundtrip():
    async def scenario():
        server, port = await fake_socks()
        proxy = parse_proxy(f"socks5://127.0.0.1:{port}")
        try:
            reader, writer = await socks5_open(proxy, "api.telegram.org", 443)
            writer.write(b"ping")
            await writer.drain()
            assert await reader.readexactly(4) == b"ping"
            writer.close()
        finally:
            server.close()
            await server.wait_closed()

    run(scenario())


def test_socks5_auth_success_and_failure():
    async def scenario():
        server, port = await fake_socks(require_auth=True, user="u", password="p")
        try:
            good = parse_proxy(f"socks5://u:p@127.0.0.1:{port}")
            reader, writer = await socks5_open(good, "example.com", 80)
            writer.write(b"ok")
            await writer.drain()
            assert await reader.readexactly(2) == b"ok"
            writer.close()

            bad = parse_proxy(f"socks5://u:wrong@127.0.0.1:{port}")
            with pytest.raises(ProxyUnavailable):
                await socks5_open(bad, "example.com", 80)
        finally:
            server.close()
            await server.wait_closed()

    run(scenario())


def test_socks5_unreachable_proxy_raises():
    async def scenario():
        proxy = parse_proxy("socks5://127.0.0.1:9")  # discard 端口，基本没人监听
        with pytest.raises(ProxyUnavailable):
            await socks5_open(proxy, "example.com", 443, timeout=1.5)

    run(scenario())


# ── 本地 CONNECT 网关 ──


def test_gateway_tunnels_connect_to_echo():
    async def scenario():
        server, port = await fake_socks()
        gateway = SocksGateway(parse_proxy(f"socks5://127.0.0.1:{port}"))
        try:
            url = await gateway.start()
            assert url.startswith("http://127.0.0.1:")
            local_port = int(url.rsplit(":", 1)[1])
            reader, writer = await asyncio.open_connection("127.0.0.1", local_port)
            writer.write(b"CONNECT api.telegram.org:443 HTTP/1.1\r\nHost: api.telegram.org:443\r\n\r\n")
            await writer.drain()
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            assert b"200 Connection Established" in head
            writer.write(b"hello")
            await writer.drain()
            assert await asyncio.wait_for(reader.readexactly(5), 5) == b"hello"
            writer.close()
        finally:
            await gateway.stop()
            server.close()
            await server.wait_closed()

    run(scenario())


def test_gateway_rejects_non_connect():
    async def scenario():
        server, port = await fake_socks()
        gateway = SocksGateway(parse_proxy(f"socks5://127.0.0.1:{port}"))
        try:
            url = await gateway.start()
            local_port = int(url.rsplit(":", 1)[1])
            reader, writer = await asyncio.open_connection("127.0.0.1", local_port)
            writer.write(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
            await writer.drain()
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            assert b"405" in head
            writer.close()
        finally:
            await gateway.stop()
            server.close()
            await server.wait_closed()

    run(scenario())


# ── auto 探测 ──


def test_candidate_hosts_skips_unresolvable_alias():
    hosts = candidate_hosts()
    assert "127.0.0.1" in hosts
    assert any(item.startswith("172.") for item in hosts)
    try:
        socket.gethostbyname("host.docker.internal")
        assert "host.docker.internal" in hosts
    except OSError:
        # 普通容器里没有这个别名是常态：不该出现在候选里，否则 auto 会先解析失败
        assert "host.docker.internal" not in hosts


def test_detect_proxy_finds_open_port_and_ignores_closed():
    async def scenario():
        async def accept_and_close(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            # 立刻关掉连接：否则 Server.wait_closed() 会等所有连接结束
            writer.close()

        server = await asyncio.start_server(accept_and_close, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            found = await detect_proxy(hosts=("127.0.0.1",), ports=(port,), timeout=0.5)
            assert found == f"127.0.0.1:{port}"
            missing = await detect_proxy(hosts=("127.0.0.1",), ports=(9,), timeout=0.3)
            assert missing == ""
        finally:
            server.close()
            await asyncio.wait_for(server.wait_closed(), 10)

    run(scenario())


