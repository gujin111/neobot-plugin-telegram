"""内置代理：SOCKS5 客户端 + 本地 HTTP 隧道网关（不需要 socksio）。

背景：NeoBot 的容器里连不上 `api.telegram.org` 时，最省事的是宿主上开个 Clash 之类
的代理，让插件连它。httpx 对 **HTTP 代理**开箱即用，但 **SOCKS5 需要额外装 `socksio`**；
容器里装依赖经常不可控，于是这里自带一份最小实现：

    插件 ──HTTP CONNECT──> 127.0.0.1:<随机端口>（本模块的网关）
                              └──SOCKS5 握手──> Clash / v2ray 的 SOCKS 端口 ──> Telegram

这样 httpx 只需要一个普通 HTTP 代理地址，`socks5://` 也能用，且不引入任何新依赖。

只实现「最基础」的部分，够 Telegram Bot API 用：

* SOCKS5：无认证 / 用户名密码（RFC 1928/1929），域名与 IPv4 目标；
* HTTP CONNECT 隧道：只是双向字节转发，不做内容解析；
* 端口 0 = 系统随机分配，天然避免与宿主端口冲突。
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import struct
from dataclasses import dataclass

__all__ = [
    "ProxyEndpoint",
    "ProxyUnavailable",
    "SocksGateway",
    "detect_proxy",
    "parse_proxy",
    "socks5_open",
]

#: `proxy = "auto"` 时依次尝试的宿主地址（容器里 127.0.0.1 是容器自己）
AUTO_HOSTS = ("127.0.0.1", "host.docker.internal")
#: 常见代理端口（Clash / mihomo / v2rayN / sing-box / 通用 HTTP 代理）
AUTO_PORTS = (7890, 7891, 7897, 1080, 10808, 10809, 20171, 3128, 8889)


def default_gateway() -> str:
    """Linux 容器里读 /proc/net/route 取默认网关（Windows / macOS 返回空串）。

    容器里 `host.docker.internal` 常常不存在（只有 Docker Desktop 提供该别名），
    但默认网关在绝大多数 bridge 网络里就是宿主，因此优先试它。
    """
    try:
        with open("/proc/net/route", encoding="ascii") as handle:
            next(handle)
            for line in handle:
                fields = line.split()
                if len(fields) < 3 or fields[1] != "00000000":
                    continue
                raw = fields[2]
                return ".".join(str(int(raw[index : index + 2], 16)) for index in (6, 4, 2, 0))
    except Exception:  # noqa: BLE001 - 非 Linux 或权限问题都按"取不到"处理
        pass
    return ""


def candidate_hosts() -> list[str]:
    """按可能性排好序的宿主地址候选（自动跳过解析不了的主机名）。"""
    hosts: list[str] = ["127.0.0.1"]
    try:
        socket.gethostbyname("host.docker.internal")
        hosts.append("host.docker.internal")
    except OSError:
        pass  # 容器里没有这个别名是常态
    gateway = default_gateway()
    if gateway and gateway not in hosts:
        hosts.append(gateway)
    for index in range(4):
        host = f"172.{17 + index}.0.1"
        if host not in hosts:
            hosts.append(host)
    return hosts

#: SOCKS5 应答码
_SOCKS_REPLIES = {
    1: "一般性失败",
    2: "规则不允许",
    3: "网络不可达",
    4: "主机不可达",
    5: "连接被拒绝",
    6: "TTL 过期",
    7: "命令不支持",
    8: "地址类型不支持",
}


class ProxyUnavailable(RuntimeError):
    """代理不可用（握手失败、拒绝连接、认证失败等）。"""


@dataclass(frozen=True)
class ProxyEndpoint:
    kind: str  # http / socks5
    host: str
    port: int
    username: str = ""
    password: str = ""

    @property
    def label(self) -> str:
        return f"{self.kind}://{self.host}:{self.port}"

    def http_url(self) -> str:
        """还原成 httpx 能吃的 URL（HTTP 代理直接用它）。"""
        auth = f"{self.username}:{self.password}@" if self.username else ""
        return f"http://{auth}{self.host}:{self.port}"


def parse_proxy(value: str) -> ProxyEndpoint | None:
    """解析 ``http://`` / ``socks5://`` 形式的代理；空值与 none/off/direct 表示不用代理。"""
    raw = (value or "").strip()
    if not raw or raw.lower() in {"none", "off", "direct", "false", "0"}:
        return None
    if "://" not in raw:
        raw = "http://" + raw  # 允许直接写 127.0.0.1:7890
    scheme, _, rest = raw.partition("://")
    scheme = scheme.lower()
    rest = rest.rstrip("/")
    auth = ""
    if "@" in rest:
        auth, _, rest = rest.rpartition("@")
    if rest.startswith("["):  # [::1]:1080
        host, _, remainder = rest.partition("]")
        host = host.lstrip("[")
        port_text = remainder.lstrip(":")
    else:
        host, _, port_text = rest.partition(":")
    default_port = 1080 if scheme.startswith("socks") else 7890
    try:
        port = int(port_text or default_port)
    except ValueError as exc:
        raise ProxyUnavailable(f"代理端口不合法: {value!r}") from exc
    if not host:
        raise ProxyUnavailable(f"代理地址不合法: {value!r}")
    username, _, password = auth.partition(":")
    return ProxyEndpoint(
        kind="socks5" if scheme.startswith("socks") else "http",
        host=host,
        port=port,
        username=username,
        password=password,
    )


async def socks5_open(
    proxy: ProxyEndpoint,
    dest_host: str,
    dest_port: int,
    *,
    timeout: float = 15.0,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """经 SOCKS5 打开到目标的一条 TCP 隧道。"""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(proxy.host, proxy.port), timeout)
    except (OSError, asyncio.TimeoutError) as exc:
        raise ProxyUnavailable(f"连不上代理 {proxy.host}:{proxy.port}（{type(exc).__name__}）") from exc

    async def send(payload: bytes) -> None:
        writer.write(payload)
        await writer.drain()

    try:
        # 1) 问候：给出可用的认证方式
        methods = b"\x00" if not proxy.username else b"\x00\x02"
        await send(b"\x05" + bytes([len(methods)]) + methods)
        head = await asyncio.wait_for(reader.readexactly(2), timeout)
        if head[0] != 5:
            raise ProxyUnavailable("代理不是 SOCKS5（版本号不匹配）")
        method = head[1]
        if method == 0xFF:
            raise ProxyUnavailable("SOCKS5 没有可用的认证方式")
        if method == 0x02:
            user = proxy.username.encode()
            password = proxy.password.encode()
            await send(b"\x01" + bytes([len(user)]) + user + bytes([len(password)]) + password)
            status = await asyncio.wait_for(reader.readexactly(2), timeout)
            if status[1] != 0:
                raise ProxyUnavailable("SOCKS5 用户名/密码认证失败")

        # 2) 连接请求：目标是域名就用域名（让代理解析，等价 socks5h）
        try:
            socket.inet_aton(dest_host)
            address = b"\x01" + socket.inet_aton(dest_host)
        except OSError:
            name = dest_host.encode()
            address = b"\x03" + bytes([len(name)]) + name
        await send(b"\x05\x01\x00" + address + struct.pack("!H", dest_port))

        head = await asyncio.wait_for(reader.readexactly(4), timeout)
        if head[1] != 0:
            raise ProxyUnavailable(
                f"SOCKS5 连接 {dest_host}:{dest_port} 失败：{_SOCKS_REPLIES.get(head[1], head[1])}"
            )
        atyp = head[3]
        if atyp == 0x01:
            await reader.readexactly(4)
        elif atyp == 0x03:
            length = (await reader.readexactly(1))[0]
            await reader.readexactly(length)
        elif atyp == 0x04:
            await reader.readexactly(16)
        await reader.readexactly(2)  # 绑定端口
        return reader, writer
    except ProxyUnavailable:
        with contextlib.suppress(Exception):
            writer.close()
        raise
    except Exception as exc:  # noqa: BLE001 - 超时/协议错误统一翻译
        with contextlib.suppress(Exception):
            writer.close()
        raise ProxyUnavailable(f"SOCKS5 握手异常：{type(exc).__name__}: {exc}") from exc


class SocksGateway:
    """本地 HTTP CONNECT 网关：把 SOCKS5 上游伪装成普通 HTTP 代理。

    只在 ``127.0.0.1`` 上监听、端口随机，用完即关；不对外暴露，也不记录流量内容。
    """

    def __init__(self, proxy: ProxyEndpoint, *, logger=None, connect_timeout: float = 20.0) -> None:
        self.proxy = proxy
        self.logger = logger
        self.connect_timeout = connect_timeout
        self._server: asyncio.AbstractServer | None = None
        self.url = ""
        self._tasks: set[asyncio.Task[None]] = set()

    # ── 生命周期 ──

    async def start(self) -> str:
        if self._server is not None:
            return self.url
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        sockets = self._server.sockets or ()
        port = sockets[0].getsockname()[1] if sockets else 0
        self.url = f"http://127.0.0.1:{port}"
        return self.url

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        self.url = ""

    def _log(self, level: str, message: str, **fields) -> None:
        if self.logger is None:
            return
        try:
            getattr(self.logger, level)(message, **fields)
        except Exception:  # pragma: no cover
            pass

    # ── 连接处理 ──

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        upstream_writer: asyncio.StreamWriter | None = None
        try:
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), self.connect_timeout)
            request_line = header.split(b"\r\n", 1)[0].decode("latin-1")
            parts = request_line.split()
            if len(parts) < 2 or parts[0].upper() != "CONNECT":
                writer.write(b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            target = parts[1]
            host, _, port_text = target.rpartition(":")
            try:
                port = int(port_text)
            except ValueError:
                writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            upstream_reader, upstream_writer = await socks5_open(
                self.proxy, host.strip("[]"), port, timeout=self.connect_timeout
            )
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            await asyncio.gather(
                self._pipe(reader, upstream_writer),
                self._pipe(upstream_reader, writer),
            )
        except Exception as exc:  # noqa: BLE001 - 单条连接失败不影响网关
            self._log("debug", f"内置代理网关连接失败：{type(exc).__name__}: {exc}")
            with contextlib.suppress(Exception):
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
        finally:
            for stream in (upstream_writer, writer):
                if stream is not None:
                    with contextlib.suppress(Exception):
                        stream.close()
            if task is not None:
                self._tasks.discard(task)

    @staticmethod
    async def _pipe(source: asyncio.StreamReader, target: asyncio.StreamWriter) -> None:
        try:
            while True:
                chunk = await source.read(64 * 1024)
                if not chunk:
                    break
                target.write(chunk)
                await target.drain()
        except Exception:  # noqa: BLE001 - 对端关闭属正常
            pass
        finally:
            with contextlib.suppress(Exception):
                target.close()


async def _tcp_alive(host: str, port: int, timeout: float = 0.8) -> bool:
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except Exception:  # noqa: BLE001
        return False
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    del reader
    return True


async def detect_proxy(
    *,
    hosts: tuple[str, ...] | None = None,
    ports: tuple[int, ...] = AUTO_PORTS,
    timeout: float = 0.8,
) -> str:
    """``proxy = "auto"`` 用：找出第一个能连上的宿主代理，返回 ``host:port``。

    只做 TCP 探测（快，不做真实请求）；找不到返回空串。
    ``hosts`` 省略时用 :func:`candidate_hosts`（含容器默认网关与 docker 网桥）。
    """
    for host in hosts if hosts is not None else tuple(candidate_hosts()):
        for port in ports:
            if await _tcp_alive(host, port, timeout):
                return f"{host}:{port}"
    return ""
