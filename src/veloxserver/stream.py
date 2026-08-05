from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass(frozen=True)
class StreamProxyConfig:
    name: str
    protocol: str
    listen_host: str
    listen_port: int
    upstream_host: str
    upstream_port: int
    upstreams: tuple[tuple[str, int], ...] = ()
    load_balance: str = "round_robin"
    proxy_protocol: bool = False
    max_connections: int = 0
    max_fails: int = 3
    fail_timeout: float = 30.0
    buffer_bytes: int = 64 * 1024
    timeout: float = 300.0


class StreamProxyManager:
    def __init__(self, proxies: tuple[StreamProxyConfig, ...]) -> None:
        self.proxies = proxies
        self.servers: list[asyncio.AbstractServer] = []
        self.udp_transports: list[asyncio.DatagramTransport] = []
        self.next_upstream: dict[str, int] = {}
        self.active_connections: dict[str, int] = {}
        self.upstream_active: dict[tuple[str, str, int], int] = {}
        self.upstream_failures: dict[tuple[str, str, int], int] = {}
        self.upstream_down_until: dict[tuple[str, str, int], float] = {}

    async def start(self) -> None:
        for proxy in self.proxies:
            protocol = proxy.protocol.lower()
            if protocol in {"tcp", "smtp", "imap", "pop3"}:
                self.servers.append(
                    await asyncio.start_server(
                        lambda reader, writer, item=proxy: self._handle_tcp(item, reader, writer),
                        host=proxy.listen_host,
                        port=proxy.listen_port,
                    )
                )
            elif protocol in {"udp", "dns"}:
                loop = asyncio.get_running_loop()
                transport, _protocol = await loop.create_datagram_endpoint(
                    lambda item=proxy: UdpProxyProtocol(item),
                    local_addr=(proxy.listen_host, proxy.listen_port),
                )
                self.udp_transports.append(transport)
            else:
                raise ValueError(f"unsupported stream proxy protocol: {proxy.protocol}")

    async def close(self) -> None:
        for transport in self.udp_transports:
            transport.close()
        for server in self.servers:
            server.close()
            await server.wait_closed()

    async def _handle_tcp(
        self,
        proxy: StreamProxyConfig,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        try:
            active = self.active_connections.get(proxy.name, 0)
            if proxy.max_connections > 0 and active >= proxy.max_connections:
                client_writer.close()
                await client_writer.wait_closed()
                return
            self.active_connections[proxy.name] = active + 1
            upstream_host, upstream_port = self._select_upstream(proxy, client_writer.get_extra_info("peername"))
            upstream_key = stream_upstream_key(proxy, upstream_host, upstream_port)
            self.upstream_active[upstream_key] = self.upstream_active.get(upstream_key, 0) + 1
            try:
                upstream_reader, upstream_writer = await asyncio.wait_for(
                    asyncio.open_connection(upstream_host, upstream_port),
                    timeout=proxy.timeout,
                )
                self._record_success(upstream_key)
            except Exception:
                self._record_failure(proxy, upstream_key)
                raise
            if proxy.proxy_protocol:
                upstream_writer.write(render_proxy_protocol_v1(client_writer.get_extra_info("peername"), upstream_host, upstream_port))
                await upstream_writer.drain()
        except Exception:
            if "upstream_key" in locals():
                self.upstream_active[upstream_key] = max(0, self.upstream_active.get(upstream_key, 1) - 1)
            self.active_connections[proxy.name] = max(0, self.active_connections.get(proxy.name, 1) - 1)
            client_writer.close()
            await client_writer.wait_closed()
            return

        async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                while True:
                    data = await reader.read(proxy.buffer_bytes)
                    if not data:
                        break
                    writer.write(data)
                    await writer.drain()
            finally:
                writer.close()

        await asyncio.gather(
            pipe(client_reader, upstream_writer),
            pipe(upstream_reader, client_writer),
            return_exceptions=True,
        )
        try:
            await client_writer.wait_closed()
        finally:
            self.upstream_active[upstream_key] = max(0, self.upstream_active.get(upstream_key, 1) - 1)
            self.active_connections[proxy.name] = max(0, self.active_connections.get(proxy.name, 1) - 1)

    def _select_upstream(self, proxy: StreamProxyConfig, peer: object) -> tuple[str, int]:
        upstreams = self._available_upstreams(proxy)
        if proxy.load_balance == "first_available":
            return upstreams[0]
        if proxy.load_balance == "least_connections":
            return min(upstreams, key=lambda item: self.upstream_active.get(stream_upstream_key(proxy, *item), 0))
        if proxy.load_balance == "ip_hash":
            key = str(peer)
            index = sum(key.encode("utf-8")) % len(upstreams)
            return upstreams[index]
        index = self.next_upstream.get(proxy.name, 0) % len(upstreams)
        self.next_upstream[proxy.name] = index + 1
        return upstreams[index]

    def _available_upstreams(self, proxy: StreamProxyConfig) -> tuple[tuple[str, int], ...]:
        import time

        upstreams = proxy.upstreams or ((proxy.upstream_host, proxy.upstream_port),)
        now = time.time()
        available = tuple(
            upstream for upstream in upstreams
            if self.upstream_down_until.get(stream_upstream_key(proxy, *upstream), 0.0) <= now
        )
        return available or upstreams

    def _record_success(self, upstream_key: tuple[str, str, int]) -> None:
        self.upstream_failures.pop(upstream_key, None)
        self.upstream_down_until.pop(upstream_key, None)

    def _record_failure(self, proxy: StreamProxyConfig, upstream_key: tuple[str, str, int]) -> None:
        import time

        failures = self.upstream_failures.get(upstream_key, 0) + 1
        self.upstream_failures[upstream_key] = failures
        if proxy.max_fails > 0 and failures >= proxy.max_fails:
            self.upstream_down_until[upstream_key] = time.time() + proxy.fail_timeout


class UdpProxyProtocol(asyncio.DatagramProtocol):
    def __init__(self, proxy: StreamProxyConfig) -> None:
        self.proxy = proxy
        self.transport: asyncio.DatagramTransport | None = None
        self.next_upstream = 0

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        asyncio.create_task(self._forward(data, addr))

    async def _forward(self, data: bytes, addr: tuple[str, int]) -> None:
        loop = asyncio.get_running_loop()
        done = loop.create_future()
        parent = self
        upstream_host, upstream_port = select_udp_upstream(self.proxy, addr, self.next_upstream)
        self.next_upstream += 1

        class ResponseProtocol(asyncio.DatagramProtocol):
            def connection_made(self, transport: asyncio.BaseTransport) -> None:
                self.transport = transport  # type: ignore[attr-defined]
                self.transport.sendto(data)  # type: ignore[attr-defined]

            def datagram_received(self, response: bytes, _upstream_addr: tuple[str, int]) -> None:
                if parent.transport is not None:
                    parent.transport.sendto(response, addr)
                if not done.done():
                    done.set_result(None)
                self.transport.close()  # type: ignore[attr-defined]

            def error_received(self, exc: Exception) -> None:
                if not done.done():
                    done.set_exception(exc)

        transport, _protocol = await loop.create_datagram_endpoint(
            ResponseProtocol,
            remote_addr=(upstream_host, upstream_port),
        )
        try:
            await asyncio.wait_for(done, timeout=self.proxy.timeout)
        except Exception:
            transport.close()


def select_udp_upstream(proxy: StreamProxyConfig, addr: tuple[str, int], next_upstream: int = 0) -> tuple[str, int]:
    upstreams = proxy.upstreams or ((proxy.upstream_host, proxy.upstream_port),)
    if proxy.load_balance == "ip_hash":
        index = sum(str(addr).encode("utf-8")) % len(upstreams)
        return upstreams[index]
    if proxy.load_balance == "first_available":
        return upstreams[0]
    return upstreams[next_upstream % len(upstreams)]


def stream_upstream_key(proxy: StreamProxyConfig, host: str, port: int) -> tuple[str, str, int]:
    return (proxy.name, host, port)


def render_proxy_protocol_v1(peer: object, upstream_host: str, upstream_port: int) -> bytes:
    if not isinstance(peer, tuple) or len(peer) < 2:
        return b"PROXY UNKNOWN\r\n"
    source_host, source_port = peer[0], peer[1]
    family = "TCP6" if ":" in str(source_host) else "TCP4"
    return f"PROXY {family} {source_host} {upstream_host} {source_port} {upstream_port}\r\n".encode("ascii")
