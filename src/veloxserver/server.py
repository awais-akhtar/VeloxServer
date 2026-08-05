from __future__ import annotations

import asyncio
import contextlib
import email.utils
import gzip
import base64
import hashlib
import importlib
import importlib.util
import json
import mimetypes
import os
import random
import re
import shlex
import signal
import ssl
import subprocess
import traceback
import urllib.parse
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from pathlib import Path
from time import time
from urllib.parse import unquote, urlsplit

from . import __version__
from .ai import (
    AIModelManager,
    AIServiceError,
    chat_completion_payload,
    error_payload,
    health_payload,
    models_payload,
    parse_chat_messages,
    render_chat_page,
    sse_chat_payload,
    text_completion_payload,
)
from .auth import claims_match, verify_hs256_jwt, verify_rs256_jwt
from .native import NativeCoreStatus, load_native_core
from .plugins import PluginManager
from .repair import AIErrorRepairer, ErrorRepairEvent, ErrorRepairSettings, redact_headers
from .shared import SharedZones
from .stream import StreamProxyConfig, StreamProxyManager

HEADER_END = b"\r\n\r\n"
SERVER_NAME = f"veloxserver/{__version__}"
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
PROXY_METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "PURGE"}
STATUS_TEXT = {
    101: "Switching Protocols",
    200: "OK",
    204: "No Content",
    304: "Not Modified",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    413: "Payload Too Large",
    414: "URI Too Long",
    429: "Too Many Requests",
    431: "Request Header Fields Too Large",
    500: "Internal Server Error",
    501: "Not Implemented",
    502: "Bad Gateway",
    503: "Service Unavailable",
    504: "Gateway Timeout",
}


@dataclass(frozen=True)
class RouteConfig:
    path: str = "/"
    kind: str = "static"
    hosts: tuple[str, ...] = field(default_factory=tuple)
    root: Path | None = None
    upstream: str | None = None
    upstreams: tuple[str, ...] = field(default_factory=tuple)
    upstream_weights: tuple[int, ...] = field(default_factory=tuple)
    strip_prefix: bool = False
    index: str = "index.html"
    directory_listing: bool = False
    precompressed: bool = True
    load_balance: str = "round_robin"
    retries: int = 1
    circuit_failures: int = 3
    circuit_cooldown: float = 30.0
    active_health_path: str = "/healthz"
    active_health_interval: float = 0.0
    active_health_timeout: float = 2.0
    proxy_cache: bool = False
    proxy_cache_ttl: float = 0.0
    proxy_cache_max_entries: int = 1024
    proxy_cache_max_bytes: int = 1024 * 1024
    proxy_cache_path: Path | None = None
    proxy_cache_max_disk_bytes: int = 256 * 1024 * 1024
    proxy_cache_key: str = "$protocol $method $host $uri"
    proxy_cache_methods: tuple[str, ...] = ("GET", "HEAD")
    proxy_cache_lock: bool = False
    proxy_cache_lock_timeout: float = 5.0
    proxy_cache_stale_while_revalidate: float = 0.0
    proxy_cache_use_stale_on_error: bool = False
    proxy_cache_purge: bool = False
    proxy_fallback_path: Path | None = None
    basic_auth: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    auth_realm: str = "veloxserver"
    jwt_hs256_secret: str | None = None
    jwt_issuer: str | None = None
    jwt_audience: str | None = None
    jwt_required_claims: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    jwt_jwks_file: Path | None = None
    jwt_jwks_url: str | None = None
    jwt_jwks_cache_ttl: float = 300.0
    external_auth_url: str | None = None
    external_auth_timeout: float = 2.0
    auth_request: str | None = None
    auth_request_timeout: float = 2.0
    ai_model_path: Path | None = None
    ai_backend: str = "auto"
    ai_model_name: str = "veloxserver-ai"
    ai_system_prompt: str = "You are a helpful assistant."
    ai_max_tokens: int = 512
    ai_temperature: float = 0.7
    ai_context_window: int = 4096
    ai_chat_enabled: bool = True
    ai_api_enabled: bool = True

    def normalized_path(self) -> str:
        prefix = self.path if self.path.startswith("/") else f"/{self.path}"
        if prefix != "/" and not prefix.endswith("/"):
            prefix = f"{prefix}/"
        return prefix

    def proxy_upstreams(self) -> tuple[str, ...]:
        if self.upstreams:
            return self.upstreams
        if self.upstream:
            return (self.upstream,)
        return ()


@dataclass(frozen=True)
class ServerConfig:
    config_path: Path | None = None
    root: Path | None = None
    host: str = "127.0.0.1"
    port: int = 8080
    max_header_bytes: int = 16 * 1024
    max_body_bytes: int = 10 * 1024 * 1024
    chunk_size: int = 256 * 1024
    proxy_buffer_bytes: int = 64 * 1024
    file_io_backend: str = "auto"
    sendfile: bool = True
    aio_threads: int = 0
    directio_min_bytes: int = 0
    io_uring: bool = False
    workers: int = 1
    reuse_port: bool = False
    upgrade_command: str | None = None
    upgrade_grace_seconds: float = 2.0
    upgrade_ready_timeout: float = 10.0
    upgrade_state_path: Path | None = None
    open_file_cache_entries: int = 0
    open_file_cache_ttl: float = 30.0
    open_file_cache_max_bytes: int = 1024 * 1024
    open_file_cache_errors: bool = False
    open_file_cache_min_uses: int = 1
    open_file_cache_inactive: float = 60.0
    open_file_cache_metadata: bool = True
    shared_zone_path: Path | None = None
    plugin_paths: tuple[Path, ...] = field(default_factory=tuple)
    access_log: bool = False
    log_format: str = "plain"
    access_log_path: Path | None = None
    error_log_path: Path | None = None
    log_rotate_bytes: int = 0
    gzip: bool = False
    gzip_min_bytes: int = 1024
    security_headers: bool = True
    health_path: str = "/healthz"
    metrics_path: str = "/metrics"
    rewrite_rules: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    advanced_rewrite_rules: tuple[AdvancedRewriteRule, ...] = field(default_factory=tuple)
    waf_block_path_patterns: tuple[str, ...] = field(default_factory=tuple)
    error_pages: tuple[tuple[int, Path], ...] = field(default_factory=tuple)
    proxy_timeout: float = 30.0
    rate_limit_per_minute: int = 0
    rate_limit_burst: int = 0
    connection_limit: int = 0
    connection_limit_per_client: int = 0
    tls_certfile: Path | None = None
    tls_keyfile: Path | None = None
    tls_ciphers: str | None = None
    tls_ciphersuites: str | None = None
    tls_min_version: str = "TLSv1.2"
    tls_session_tickets: bool = True
    tls_client_verify: str = "off"
    tls_client_ca_file: Path | None = None
    tls_ecdh_curve: str | None = None
    tls_keylog_file: Path | None = None
    tls_alpn_protocols: tuple[str, ...] = field(default_factory=tuple)
    tls_ocsp_required: bool = False
    tls_ocsp_response_file: Path | None = None
    tls_sni: tuple[tuple[str, Path, Path], ...] = field(default_factory=tuple)
    tls_reload_interval: float = 5.0
    graceful_shutdown_timeout: float = 10.0
    http2: bool = False
    http3: bool = False
    http3_port: int | None = None
    admin_enabled: bool = False
    admin_path: str = "/__veloxserver"
    admin_reload_path: str = "/reload"
    admin_status_path: str = "/status"
    native_core: str = "python"
    native_core_path: Path | None = None
    ai_error_repair_enabled: bool = False
    ai_error_repair_project_path: Path | None = None
    ai_error_repair_log_path: Path | None = None
    ai_error_repair_suggestions_path: Path | None = None
    ai_error_repair_apply: bool = False
    ai_error_repair_model: str = "gpt-4.1-mini"
    ai_error_repair_api_key_env: str = "OPENAI_API_KEY"
    ai_error_repair_base_url: str = "https://api.openai.com/v1"
    ai_error_repair_timeout: float = 30.0
    ai_error_repair_min_status: int = 500
    ai_error_repair_statuses: tuple[int, ...] = field(default_factory=tuple)
    ai_error_repair_context_files: tuple[Path, ...] = field(default_factory=tuple)
    ai_error_repair_max_file_bytes: int = 32 * 1024
    ai_error_repair_max_context_bytes: int = 96 * 1024
    ai_error_repair_cooldown_seconds: float = 60.0
    ai_error_repair_max_output_tokens: int = 1600
    stream_proxies: tuple[StreamProxyConfig, ...] = field(default_factory=tuple)
    routes: tuple[RouteConfig, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class AdvancedRewriteRule:
    pattern: str
    replacement: str
    methods: tuple[str, ...] = field(default_factory=tuple)
    hosts: tuple[str, ...] = field(default_factory=tuple)
    header: tuple[str, str] | None = None
    query: str | None = None
    stop: bool = True


@dataclass(frozen=True)
class Request:
    method: str
    target: str
    version: str
    headers: dict[str, str]
    body: bytes = b""

    @property
    def wants_keep_alive(self) -> bool:
        connection = self.headers.get("connection", "").lower()
        if self.version == "HTTP/1.1":
            return connection != "close"
        return connection == "keep-alive"


@dataclass(frozen=True)
class ResponseOutcome:
    status: int
    keep_alive: bool
    bytes_sent: int = 0


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: list[tuple[str, str]]
    body: bytes = b""


@dataclass(frozen=True)
class Upstream:
    host: str
    port: int
    base_path: str
    authority: str
    unix_socket: Path | None = None


@dataclass(frozen=True)
class ProxyCacheRecord:
    status: int
    head: bytes
    body: bytes
    stale: bool = False


@dataclass(frozen=True)
class OpenFileCacheInfo:
    exists: bool
    is_file: bool = False
    is_dir: bool = False
    stat: object | None = None
    error_status: int | None = None


@dataclass
class UpstreamState:
    upstream: Upstream
    weight: int = 1
    failures: int = 0
    open_until: float = 0.0
    active_connections: int = 0

    def is_available(self) -> bool:
        return time() >= self.open_until

    def record_success(self) -> None:
        self.failures = 0
        self.open_until = 0.0

    def record_failure(self, threshold: int, cooldown: float) -> None:
        self.failures += 1
        if threshold > 0 and self.failures >= threshold:
            self.open_until = time() + cooldown


@dataclass
class RouteRuntime:
    route: RouteConfig
    upstreams: list[UpstreamState]
    next_index: int = 0
    cache: OrderedDict[str, tuple[float, int, bytes, bytes]] = field(default_factory=OrderedDict)
    cache_locks: dict[str, asyncio.Lock] = field(default_factory=dict)

    @classmethod
    def from_route(cls, route: RouteConfig) -> RouteRuntime:
        weights = route.upstream_weights
        states = []
        for index, value in enumerate(route.proxy_upstreams()):
            weight = weights[index] if index < len(weights) else parse_upstream_weight(value)
            states.append(UpstreamState(parse_upstream(strip_upstream_weight(value)), max(1, weight)))
        return cls(route=route, upstreams=states)

    def select_attempts(self, client_key: str = "", target: str = "") -> list[UpstreamState]:
        attempts = max(1, self.route.retries + 1)
        available = [state for state in self.upstreams if state.is_available()]
        if not available:
            return []
        if self.route.load_balance == "first_available":
            return available[:attempts]
        if self.route.load_balance == "least_connections":
            return sorted(available, key=lambda state: state.active_connections)[:attempts]
        if self.route.load_balance in {"ip_hash", "hash"}:
            key = client_key if self.route.load_balance == "ip_hash" else target
            start = stable_index(key, available)
            return rotate(available, start)[:attempts]

        selected: list[UpstreamState] = []
        weighted = [state for state in available for _ in range(state.weight)]
        total = len(weighted)
        start = self.next_index % total
        self.next_index = (self.next_index + 1) % total
        for state in rotate(weighted, start):
            if state not in selected:
                selected.append(state)
            if len(selected) >= attempts:
                break
        return selected

    def cache_get(self, key: str, stale_window: float = 0.0) -> ProxyCacheRecord | None:
        item = self.cache.get(key)
        if item is None:
            return None
        expires_at, status, head, body = item
        if expires_at < time():
            if stale_window <= 0 or expires_at + stale_window < time():
                self.cache.pop(key, None)
                return None
            self.cache.move_to_end(key)
            return ProxyCacheRecord(status, head, body, stale=True)
        self.cache.move_to_end(key)
        return ProxyCacheRecord(status, head, body, stale=False)

    def cache_put(self, key: str, status: int, head: bytes, body: bytes) -> None:
        if self.route.proxy_cache_ttl <= 0 or len(body) > self.route.proxy_cache_max_bytes:
            return
        self.cache[key] = (time() + self.route.proxy_cache_ttl, status, head, body)
        self.cache.move_to_end(key)
        while len(self.cache) > self.route.proxy_cache_max_entries:
            self.cache.popitem(last=False)

    def cache_purge(self, key: str) -> bool:
        return self.cache.pop(key, None) is not None

    def cache_lock(self, key: str) -> asyncio.Lock:
        lock = self.cache_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self.cache_locks[key] = lock
        return lock


@dataclass
class Metrics:
    started_at: float = field(default_factory=time)
    requests_total: int = 0
    responses_total: dict[int, int] = field(default_factory=dict)
    bytes_sent_total: int = 0
    active_connections: int = 0
    rate_limited_total: int = 0
    proxy_retries_total: int = 0
    upstream_failures_total: int = 0
    circuit_open_total: int = 0
    active_health_failures_total: int = 0
    proxy_cache_hits_total: int = 0
    connection_limited_total: int = 0
    ai_error_repairs_total: int = 0
    ai_error_repair_failures_total: int = 0

    def record_response(self, status: int, bytes_sent: int) -> None:
        self.requests_total += 1
        self.responses_total[status] = self.responses_total.get(status, 0) + 1
        self.bytes_sent_total += bytes_sent

    def render_prometheus(self) -> bytes:
        lines = [
            "# HELP veloxserver_uptime_seconds Server uptime in seconds.",
            "# TYPE veloxserver_uptime_seconds gauge",
            f"veloxserver_uptime_seconds {time() - self.started_at:.3f}",
            "# HELP veloxserver_requests_total Total HTTP requests.",
            "# TYPE veloxserver_requests_total counter",
            f"veloxserver_requests_total {self.requests_total}",
            "# HELP veloxserver_responses_total HTTP responses by status.",
            "# TYPE veloxserver_responses_total counter",
        ]
        for status, count in sorted(self.responses_total.items()):
            lines.append(f'veloxserver_responses_total{{status="{status}"}} {count}')
        lines.extend(
            [
                "# HELP veloxserver_bytes_sent_total Total response bytes sent.",
                "# TYPE veloxserver_bytes_sent_total counter",
                f"veloxserver_bytes_sent_total {self.bytes_sent_total}",
                "# HELP veloxserver_active_connections Active client connections.",
                "# TYPE veloxserver_active_connections gauge",
                f"veloxserver_active_connections {self.active_connections}",
                "# HELP veloxserver_rate_limited_total Rate limited requests.",
                "# TYPE veloxserver_rate_limited_total counter",
                f"veloxserver_rate_limited_total {self.rate_limited_total}",
                "# HELP veloxserver_proxy_retries_total Proxy retries.",
                "# TYPE veloxserver_proxy_retries_total counter",
                f"veloxserver_proxy_retries_total {self.proxy_retries_total}",
                "# HELP veloxserver_upstream_failures_total Passive upstream failures.",
                "# TYPE veloxserver_upstream_failures_total counter",
                f"veloxserver_upstream_failures_total {self.upstream_failures_total}",
                "# HELP veloxserver_circuit_open_total Circuit open events.",
                "# TYPE veloxserver_circuit_open_total counter",
                f"veloxserver_circuit_open_total {self.circuit_open_total}",
                "# HELP veloxserver_active_health_failures_total Active health check failures.",
                "# TYPE veloxserver_active_health_failures_total counter",
                f"veloxserver_active_health_failures_total {self.active_health_failures_total}",
                "# HELP veloxserver_proxy_cache_hits_total Proxy cache hits.",
                "# TYPE veloxserver_proxy_cache_hits_total counter",
                f"veloxserver_proxy_cache_hits_total {self.proxy_cache_hits_total}",
                "# HELP veloxserver_connection_limited_total Connection limit rejections.",
                "# TYPE veloxserver_connection_limited_total counter",
                f"veloxserver_connection_limited_total {self.connection_limited_total}",
                "# HELP veloxserver_ai_error_repairs_total AI error repair jobs queued.",
                "# TYPE veloxserver_ai_error_repairs_total counter",
                f"veloxserver_ai_error_repairs_total {self.ai_error_repairs_total}",
                "# HELP veloxserver_ai_error_repair_failures_total AI error repair failures.",
                "# TYPE veloxserver_ai_error_repair_failures_total counter",
                f"veloxserver_ai_error_repair_failures_total {self.ai_error_repair_failures_total}",
                "",
            ]
        )
        return "\n".join(lines).encode("ascii")


@dataclass
class RateLimitBucket:
    tokens: float
    updated_at: float


class RateLimiter:
    def __init__(self, per_minute: int, burst: int) -> None:
        self.per_minute = per_minute
        self.burst = burst or per_minute
        self.buckets: dict[str, RateLimitBucket] = {}

    def allow(self, key: str) -> bool:
        if self.per_minute <= 0:
            return True
        now = time()
        rate_per_second = self.per_minute / 60
        bucket = self.buckets.get(key)
        if bucket is None:
            self.buckets[key] = RateLimitBucket(tokens=max(0, self.burst - 1), updated_at=now)
            return True

        elapsed = max(0.0, now - bucket.updated_at)
        bucket.tokens = min(self.burst, bucket.tokens + elapsed * rate_per_second)
        bucket.updated_at = now
        if bucket.tokens >= 1:
            bucket.tokens -= 1
            return True
        return False


class ConnectionLimiter:
    def __init__(self, total_limit: int, per_client_limit: int) -> None:
        self.total_limit = total_limit
        self.per_client_limit = per_client_limit
        self.total = 0
        self.by_client: dict[str, int] = {}

    def acquire(self, client: str) -> bool:
        if self.total_limit > 0 and self.total >= self.total_limit:
            return False
        current = self.by_client.get(client, 0)
        if self.per_client_limit > 0 and current >= self.per_client_limit:
            return False
        self.total += 1
        self.by_client[client] = current + 1
        return True

    def release(self, client: str) -> None:
        if self.total > 0:
            self.total -= 1
        current = self.by_client.get(client, 0)
        if current <= 1:
            self.by_client.pop(client, None)
        else:
            self.by_client[client] = current - 1


class RotatingTextLog:
    def __init__(self, path: Path | None, rotate_bytes: int) -> None:
        self.path = path
        self.rotate_bytes = rotate_bytes

    def write(self, line: str) -> None:
        if self.path is None:
            print(line)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.rotate_bytes > 0 and self.path.exists() and self.path.stat().st_size >= self.rotate_bytes:
            rotated = self.path.with_suffix(f"{self.path.suffix}.1")
            with contextlib.suppress(FileNotFoundError):
                rotated.unlink()
            self.path.replace(rotated)
        with self.path.open("a", encoding="utf-8") as file:
            file.write(line)
            file.write("\n")


class NullTransport:
    def get_write_buffer_size(self) -> int:
        return 0


class NullStreamWriter:
    def __init__(self) -> None:
        self.transport = NullTransport()

    def write(self, _data: bytes) -> None:
        return

    async def drain(self) -> None:
        return


class DiskProxyCache:
    def __init__(self, root: Path, max_bytes: int) -> None:
        self.root = root
        self.max_bytes = max_bytes
        self.root.mkdir(parents=True, exist_ok=True)

    def _paths(self, key: str) -> tuple[Path, Path, Path]:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        prefix = self.root / digest[:2]
        return prefix / f"{digest}.json", prefix / f"{digest}.head", prefix / f"{digest}.body"

    def get(self, key: str, stale_window: float = 0.0) -> ProxyCacheRecord | None:
        meta_path, head_path, body_path = self._paths(key)
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            expires_at = float(meta["expires_at"])
            now = time()
            if expires_at < now and (stale_window <= 0 or expires_at + stale_window < now):
                self.purge(key)
                return None
            return ProxyCacheRecord(
                int(meta["status"]),
                head_path.read_bytes(),
                body_path.read_bytes(),
                stale=expires_at < now,
            )
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return None

    def put(self, key: str, status: int, head: bytes, body: bytes, ttl: float, max_body_bytes: int) -> None:
        if ttl <= 0 or len(body) > max_body_bytes:
            return
        meta_path, head_path, body_path = self._paths(key)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        head_path.write_bytes(head)
        body_path.write_bytes(body)
        meta_path.write_text(
            json.dumps(
                {"key": key, "status": status, "expires_at": time() + ttl, "body_bytes": len(body)},
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        self.enforce_size()

    def purge(self, key: str) -> bool:
        removed = False
        for path in self._paths(key):
            if path.exists():
                with contextlib.suppress(OSError):
                    path.unlink()
                    removed = True
        return removed

    def enforce_size(self) -> None:
        if self.max_bytes <= 0:
            return
        files = [path for path in self.root.rglob("*") if path.is_file()]
        total = sum(path.stat().st_size for path in files)
        if total <= self.max_bytes:
            return
        for path in sorted(files, key=lambda item: item.stat().st_mtime):
            with contextlib.suppress(OSError):
                total -= path.stat().st_size
                path.unlink()
            if total <= self.max_bytes:
                break


class OpenFileCache:
    def __init__(
        self,
        entries: int,
        ttl: float,
        max_bytes: int,
        cache_errors: bool = False,
        min_uses: int = 1,
        inactive: float = 60.0,
        metadata: bool = True,
    ) -> None:
        self.entries = entries
        self.ttl = ttl
        self.max_bytes = max_bytes
        self.cache_errors = cache_errors
        self.min_uses = max(1, min_uses)
        self.inactive = inactive
        self.metadata = metadata
        self.items: OrderedDict[Path, tuple[float, float, int, int, bool, bool, int | None, bytes | None]] = OrderedDict()
        self.uses: dict[Path, int] = {}

    def info(self, path: Path) -> OpenFileCacheInfo:
        if self.entries <= 0 or not self.metadata:
            return self._info_uncached(path)
        now = time()
        item = self.items.get(path)
        if item is not None:
            valid_until, inactive_until, mtime_ns, size, is_file, is_dir, error_status, body = item
            if valid_until >= now and inactive_until >= now:
                self.items[path] = (valid_until, now + self.inactive, mtime_ns, size, is_file, is_dir, error_status, body)
                self.items.move_to_end(path)
                if error_status is not None:
                    return OpenFileCacheInfo(False, error_status=error_status)
                return OpenFileCacheInfo(True, is_file, is_dir, CachedStat(mtime_ns, size))
            self.items.pop(path, None)
        info = self._info_uncached(path)
        self._remember_info(path, info)
        return info

    def _info_uncached(self, path: Path) -> OpenFileCacheInfo:
        try:
            stat = path.stat()
        except FileNotFoundError:
            return OpenFileCacheInfo(False, error_status=404)
        except PermissionError:
            return OpenFileCacheInfo(False, error_status=403)
        return OpenFileCacheInfo(True, path.is_file(), path.is_dir(), stat)

    def _remember_info(self, path: Path, info: OpenFileCacheInfo, body: bytes | None = None) -> None:
        if self.entries <= 0 or not self.metadata:
            return
        if info.error_status is not None and not self.cache_errors:
            return
        uses = self.uses.get(path, 0) + 1
        self.uses[path] = uses
        if uses < self.min_uses:
            return
        now = time()
        stat = info.stat
        mtime_ns = int(getattr(stat, "st_mtime_ns", 0))
        size = int(getattr(stat, "st_size", 0))
        self.items[path] = (
            now + self.ttl,
            now + self.inactive,
            mtime_ns,
            size,
            info.is_file,
            info.is_dir,
            info.error_status,
            body,
        )
        self.items.move_to_end(path)
        while len(self.items) > self.entries:
            self.items.popitem(last=False)

    def read(self, path: Path) -> bytes:
        if self.entries <= 0:
            return path.read_bytes()
        info = self.info(path)
        if not info.exists:
            if info.error_status == 403:
                raise PermissionError(path)
            raise FileNotFoundError(path)
        stat = info.stat or path.stat()
        now = time()
        item = self.items.get(path)
        if item is not None:
            valid_until, inactive_until, mtime_ns, size, is_file, is_dir, error_status, body = item
            if (
                body is not None
                and valid_until >= now
                and inactive_until >= now
                and mtime_ns == stat.st_mtime_ns
                and size == stat.st_size
            ):
                self.items[path] = (valid_until, now + self.inactive, mtime_ns, size, is_file, is_dir, error_status, body)
                self.items.move_to_end(path)
                return body
            self.items.pop(path, None)
        body = path.read_bytes()
        if len(body) <= self.max_bytes:
            self._remember_info(path, OpenFileCacheInfo(True, path.is_file(), path.is_dir(), stat), body)
        return body


@dataclass(frozen=True)
class CachedStat:
    st_mtime_ns: int
    st_size: int

    @property
    def st_mtime(self) -> float:
        return self.st_mtime_ns / 1_000_000_000


class HttpError(Exception):
    def __init__(self, status: int, message: str | None = None) -> None:
        super().__init__(message or STATUS_TEXT.get(status, "Error"))
        self.status = status
        self.message = message or STATUS_TEXT.get(status, "Error")


class UpstreamUnavailable(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class VeloxServer:
    def __init__(self, config: ServerConfig) -> None:
        validate_runtime_config(config)
        self.config = config
        self.routes = normalize_routes(config)
        self.health_path = normalize_health_path(config.health_path)
        self.metrics_path = normalize_health_path(config.metrics_path)
        self.metrics = Metrics()
        self.rate_limiter = RateLimiter(config.rate_limit_per_minute, config.rate_limit_burst)
        self.connection_limiter = ConnectionLimiter(config.connection_limit, config.connection_limit_per_client)
        self.access_logger = RotatingTextLog(config.access_log_path, config.log_rotate_bytes)
        self.error_logger = RotatingTextLog(config.error_log_path, config.log_rotate_bytes)
        self.file_cache = OpenFileCache(
            config.open_file_cache_entries,
            config.open_file_cache_ttl,
            config.open_file_cache_max_bytes,
            config.open_file_cache_errors,
            config.open_file_cache_min_uses,
            config.open_file_cache_inactive,
            config.open_file_cache_metadata,
        )
        self.shared_zones = SharedZones(
            config.shared_zone_path,
            config.rate_limit_per_minute,
            config.rate_limit_burst,
            config.connection_limit,
            config.connection_limit_per_client,
        )
        self.ai_models = AIModelManager()
        self.ai_error_repairer = AIErrorRepairer(
            ErrorRepairSettings(
                enabled=config.ai_error_repair_enabled,
                project_path=config.ai_error_repair_project_path or config.root or (config.config_path.parent if config.config_path else None),
                log_path=config.ai_error_repair_log_path,
                suggestions_path=config.ai_error_repair_suggestions_path,
                apply_patches=config.ai_error_repair_apply,
                model=config.ai_error_repair_model,
                api_key_env=config.ai_error_repair_api_key_env,
                base_url=config.ai_error_repair_base_url,
                timeout=config.ai_error_repair_timeout,
                min_status=config.ai_error_repair_min_status,
                include_statuses=config.ai_error_repair_statuses,
                context_files=config.ai_error_repair_context_files,
                max_file_bytes=config.ai_error_repair_max_file_bytes,
                max_context_bytes=config.ai_error_repair_max_context_bytes,
                cooldown_seconds=config.ai_error_repair_cooldown_seconds,
                max_output_tokens=config.ai_error_repair_max_output_tokens,
            )
        )
        self._ai_repair_tasks: set[asyncio.Task[object]] = set()
        self.plugin_manager = PluginManager(config.plugin_paths)
        self.native_core_status: NativeCoreStatus = load_native_core(config.native_core, config.native_core_path)
        self.native_core = self.native_core_status.core
        self.stream_proxy_manager = StreamProxyManager(config.stream_proxies)
        self.rewrite_rules = tuple((re.compile(pattern), replacement) for pattern, replacement in config.rewrite_rules)
        self.advanced_rewrite_rules = config.advanced_rewrite_rules
        self.waf_block_path_patterns = tuple(re.compile(pattern) for pattern in config.waf_block_path_patterns)
        self.error_pages = {status: path for status, path in config.error_pages}
        self.route_runtimes = {
            route.path: RouteRuntime.from_route(route)
            for route in self.routes
            if route.kind == "proxy"
        }
        self.jwks_cache: dict[str, tuple[float, dict[str, object]]] = {}
        self.proxy_disk_caches = {
            route.path: DiskProxyCache(route.proxy_cache_path, route.proxy_cache_max_disk_bytes)
            for route in self.routes
            if route.kind == "proxy" and route.proxy_cache_path is not None
        }
        self.ssl_context = create_ssl_context(config)
        self._tls_stamp = tls_stamp(config)
        self.generation = int(os.environ.get("VELOXSERVER_GENERATION", "0") or "0")
        self._shutdown_event: asyncio.Event | None = None

    async def serve_forever(self) -> None:
        self._validate_static_roots()
        self._shutdown_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        self._install_signal_handlers(loop)

        server = await asyncio.start_server(
            self._handle_client,
            host=self.config.host,
            port=self.config.port,
            limit=self.config.max_header_bytes,
            ssl=self.ssl_context,
            reuse_port=self.config.reuse_port,
        )
        await self.stream_proxy_manager.start()
        http3_server = await self._start_http3_server()
        tls_task = self._start_tls_reloader()
        health_task = self._start_active_health_checker()
        sockets = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
        scheme = "https" if self.ssl_context else "http"
        print(f"{SERVER_NAME} serving {scheme} on {sockets}")

        try:
            async with server:
                await self._shutdown_event.wait()
                server.close()
                await asyncio.wait_for(server.wait_closed(), timeout=self.config.graceful_shutdown_timeout)
        finally:
            if tls_task is not None:
                tls_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await tls_task
            if health_task is not None:
                health_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await health_task
            if http3_server is not None:
                http3_server.close()
            await self.stream_proxy_manager.close()

    def request_shutdown(self) -> None:
        if self._shutdown_event is not None and not self._shutdown_event.is_set():
            self._shutdown_event.set()

    def _install_signal_handlers(self, loop: asyncio.AbstractEventLoop) -> None:
        for sig_name in ("SIGTERM", "SIGINT"):
            sig = getattr(signal, sig_name, None)
            if sig is None:
                continue
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.add_signal_handler(sig, self.request_shutdown)

        sighup = getattr(signal, "SIGHUP", None)
        if sighup is not None:
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.add_signal_handler(sighup, self.reload_runtime_config)
        sigusr2 = getattr(signal, "SIGUSR2", None)
        if sigusr2 is not None:
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.add_signal_handler(sigusr2, lambda: asyncio.create_task(self.request_binary_upgrade()))

    def _start_tls_reloader(self) -> asyncio.Task[None] | None:
        if self.ssl_context is None or self.config.tls_reload_interval <= 0:
            return None
        return asyncio.create_task(self._tls_reload_loop())

    def _start_active_health_checker(self) -> asyncio.Task[None] | None:
        if not any(runtime.route.active_health_interval > 0 for runtime in self.route_runtimes.values()):
            return None
        return asyncio.create_task(self._active_health_loop())

    async def _start_http3_server(self) -> object | None:
        if not self.config.http3:
            return None
        from .http3 import start_http3_server

        return await start_http3_server(self)

    async def request_binary_upgrade(self) -> bool:
        if not self.config.upgrade_command:
            print(f"{SERVER_NAME} upgrade requested but upgrade_command is not configured")
            return False
        next_generation = self.generation + 1
        env = os.environ.copy()
        env["VELOXSERVER_UPGRADE_FROM_PID"] = str(os.getpid())
        env["VELOXSERVER_GENERATION"] = str(next_generation)
        try:
            process = subprocess.Popen(shlex.split(self.config.upgrade_command), close_fds=False, env=env)
        except OSError as exc:
            print(f"{SERVER_NAME} upgrade failed: {exc}")
            return False
        self._write_upgrade_state(process.pid, next_generation, "started")
        ready = await self._wait_for_upgrade_generation(next_generation)
        if not ready:
            self._write_upgrade_state(process.pid, next_generation, "failed")
            print(f"{SERVER_NAME} upgrade did not become ready before timeout")
            return False
        self._write_upgrade_state(process.pid, next_generation, "ready")
        await asyncio.sleep(self.config.upgrade_grace_seconds)
        self.request_shutdown()
        return True

    def _write_upgrade_state(self, pid: int, generation: int, state: str) -> None:
        if self.config.upgrade_state_path is None:
            return
        payload = {
            "old_pid": os.getpid(),
            "new_pid": pid,
            "generation": generation,
            "state": state,
            "time": time(),
        }
        try:
            self.config.upgrade_state_path.parent.mkdir(parents=True, exist_ok=True)
            self.config.upgrade_state_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        except OSError as exc:
            print(f"{SERVER_NAME} upgrade state write failed: {exc}")

    async def _wait_for_upgrade_generation(self, generation: int) -> bool:
        deadline = time() + self.config.upgrade_ready_timeout
        while time() < deadline:
            if await self._probe_generation(generation):
                return True
            await asyncio.sleep(0.1)
        return False

    async def _probe_generation(self, generation: int) -> bool:
        ssl_context = ssl._create_unverified_context() if self.ssl_context is not None else None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.config.host, self.config.port, ssl=ssl_context),
                timeout=1.0,
            )
            writer.write(
                (
                    f"GET {self.health_path} HTTP/1.1\r\n"
                    f"Host: {self.config.host}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode("ascii")
            )
            await writer.drain()
            head = await asyncio.wait_for(reader.readuntil(HEADER_END), timeout=1.0)
            writer.close()
            with contextlib.suppress(ConnectionError, RuntimeError):
                await writer.wait_closed()
        except Exception:
            return False
        return f"x-veloxserver-generation: {generation}".encode("ascii") in head.lower()

    async def _tls_reload_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.tls_reload_interval)
            self.reload_tls_context()

    async def _active_health_loop(self) -> None:
        while True:
            intervals = [
                runtime.route.active_health_interval
                for runtime in self.route_runtimes.values()
                if runtime.route.active_health_interval > 0
            ]
            await asyncio.sleep(min(intervals) if intervals else 30.0)
            await self.check_upstreams_once()

    async def check_upstreams_once(self) -> None:
        for runtime in self.route_runtimes.values():
            route = runtime.route
            if route.active_health_interval <= 0:
                continue
            for state in runtime.upstreams:
                try:
                    reader, writer = await connect_upstream(state.upstream, route.active_health_timeout)
                    target = route.active_health_path if route.active_health_path.startswith("/") else f"/{route.active_health_path}"
                    writer.write(
                        (
                            f"GET {target} HTTP/1.1\r\n"
                            f"Host: {state.upstream.authority}\r\n"
                            "Connection: close\r\n\r\n"
                        ).encode("ascii")
                    )
                    await writer.drain()
                    head = await asyncio.wait_for(reader.readuntil(HEADER_END), timeout=route.active_health_timeout)
                    status = int(head.split(b" ", 2)[1])
                    writer.close()
                    with contextlib.suppress(ConnectionError, RuntimeError):
                        await writer.wait_closed()
                    if status < 500:
                        state.record_success()
                    else:
                        self._record_upstream_failure(route, state)
                        self.metrics.active_health_failures_total += 1
                except Exception:
                    self._record_upstream_failure(route, state)
                    self.metrics.active_health_failures_total += 1

    def reload_tls_context(self) -> bool:
        if self.ssl_context is None:
            return False
        stamp = tls_stamp(self.config)
        if stamp is None or stamp == self._tls_stamp:
            return False
        self.ssl_context.load_cert_chain(
            certfile=str(self.config.tls_certfile),
            keyfile=str(self.config.tls_keyfile),
        )
        self._tls_stamp = stamp
        print(f"{SERVER_NAME} reloaded TLS certificate")
        return True

    def reload_runtime_config(self) -> bool:
        if self.config.config_path is None:
            return self.reload_tls_context()

        try:
            from .config import load_config

            new_config = load_config(self.config.config_path)
            validate_runtime_config(new_config)
            if (new_config.host, new_config.port) != (self.config.host, self.config.port):
                raise ValueError("host or port changes require a full restart")
            old_tls = self.config.tls_certfile is not None or self.config.tls_keyfile is not None
            new_tls = new_config.tls_certfile is not None or new_config.tls_keyfile is not None
            if old_tls != new_tls:
                raise ValueError("enabling or disabling TLS requires a full restart")
            new_routes = normalize_routes(new_config)
            for route in new_routes:
                if route.kind == "static" and route.root is not None:
                    root = route.root.resolve()
                    if not root.exists() or not root.is_dir():
                        raise ValueError(f"route root does not exist or is not a directory: {root}")
        except Exception as exc:
            print(f"{SERVER_NAME} config reload failed: {exc}")
            return False

        self.config = new_config
        self.routes = new_routes
        self.health_path = normalize_health_path(new_config.health_path)
        self.metrics_path = normalize_health_path(new_config.metrics_path)
        self.rate_limiter = RateLimiter(new_config.rate_limit_per_minute, new_config.rate_limit_burst)
        self.connection_limiter = ConnectionLimiter(new_config.connection_limit, new_config.connection_limit_per_client)
        self.access_logger = RotatingTextLog(new_config.access_log_path, new_config.log_rotate_bytes)
        self.error_logger = RotatingTextLog(new_config.error_log_path, new_config.log_rotate_bytes)
        self.ai_models = AIModelManager()
        self.ai_error_repairer = AIErrorRepairer(
            ErrorRepairSettings(
                enabled=new_config.ai_error_repair_enabled,
                project_path=new_config.ai_error_repair_project_path or new_config.root or (new_config.config_path.parent if new_config.config_path else None),
                log_path=new_config.ai_error_repair_log_path,
                suggestions_path=new_config.ai_error_repair_suggestions_path,
                apply_patches=new_config.ai_error_repair_apply,
                model=new_config.ai_error_repair_model,
                api_key_env=new_config.ai_error_repair_api_key_env,
                base_url=new_config.ai_error_repair_base_url,
                timeout=new_config.ai_error_repair_timeout,
                min_status=new_config.ai_error_repair_min_status,
                include_statuses=new_config.ai_error_repair_statuses,
                context_files=new_config.ai_error_repair_context_files,
                max_file_bytes=new_config.ai_error_repair_max_file_bytes,
                max_context_bytes=new_config.ai_error_repair_max_context_bytes,
                cooldown_seconds=new_config.ai_error_repair_cooldown_seconds,
                max_output_tokens=new_config.ai_error_repair_max_output_tokens,
            )
        )
        self.file_cache = OpenFileCache(
            new_config.open_file_cache_entries,
            new_config.open_file_cache_ttl,
            new_config.open_file_cache_max_bytes,
            new_config.open_file_cache_errors,
            new_config.open_file_cache_min_uses,
            new_config.open_file_cache_inactive,
            new_config.open_file_cache_metadata,
        )
        self.rewrite_rules = tuple((re.compile(pattern), replacement) for pattern, replacement in new_config.rewrite_rules)
        self.advanced_rewrite_rules = new_config.advanced_rewrite_rules
        self.waf_block_path_patterns = tuple(re.compile(pattern) for pattern in new_config.waf_block_path_patterns)
        self.error_pages = {status: path for status, path in new_config.error_pages}
        self.route_runtimes = {
            route.path: RouteRuntime.from_route(route)
            for route in self.routes
            if route.kind == "proxy"
        }
        self.jwks_cache.clear()
        self.proxy_disk_caches = {
            route.path: DiskProxyCache(route.proxy_cache_path, route.proxy_cache_max_disk_bytes)
            for route in self.routes
            if route.kind == "proxy" and route.proxy_cache_path is not None
        }

        if self.ssl_context is not None:
            self.reload_tls_context()
        print(f"{SERVER_NAME} reloaded config from {new_config.config_path}")
        return True

    def _validate_static_roots(self) -> None:
        for route in self.routes:
            if route.kind == "static" and route.root is not None:
                root = route.root.resolve()
                if not root.exists() or not root.is_dir():
                    raise SystemExit(f"route root does not exist or is not a directory: {root}")

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername")
        client_key = remote_addr(peer)
        local_connection_acquired = self.connection_limiter.acquire(client_key)
        shared_connection_acquired = local_connection_acquired and self.shared_zones.acquire_connection(client_key)
        if not local_connection_acquired or not shared_connection_acquired:
            self.metrics.connection_limited_total += 1
            bytes_sent = await self._send_error(writer, 503, "Connection limit exceeded", keep_alive=False)
            self.metrics.record_response(503, bytes_sent)
            if local_connection_acquired:
                self.connection_limiter.release(client_key)
            writer.close()
            with contextlib.suppress(ConnectionError, RuntimeError):
                await writer.wait_closed()
            return
        self.metrics.active_connections += 1
        selected_protocol = selected_alpn_protocol(writer)
        if self.config.http2 and selected_protocol == "h2":
            try:
                await self._handle_http2_client(reader, writer, peer)
            finally:
                self.metrics.active_connections -= 1
                self.connection_limiter.release(client_key)
                writer.close()
                with contextlib.suppress(ConnectionError, RuntimeError):
                    await writer.wait_closed()
            return

        keep_alive = True
        try:
            while keep_alive:
                started = time()
                bytes_sent = 0
                status = 0
                method = "-"
                target = "-"
                request: Request | None = None
                try:
                    raw = await self._read_headers(reader)
                    if raw == b"":
                        break
                    request = self._parse_request(raw)
                    request = replace(request, body=await self._read_body(reader, request))
                    method = request.method
                    target = request.target
                    keep_alive = request.wants_keep_alive
                    outcome = await self._serve_request(reader, writer, request, keep_alive, peer)
                    keep_alive = outcome.keep_alive
                    status = outcome.status
                    bytes_sent = outcome.bytes_sent
                except HttpError as exc:
                    keep_alive = False
                    status = exc.status
                    bytes_sent = await self._send_error(
                        writer,
                        exc.status,
                        exc.message,
                        keep_alive=False,
                        request=request,
                        peer=peer,
                        exception=exc,
                    )
                except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
                    break
                except Exception as exc:
                    keep_alive = False
                    status = 500
                    bytes_sent = await self._send_error(
                        writer,
                        500,
                        STATUS_TEXT[500],
                        keep_alive=False,
                        request=request,
                        peer=peer,
                        exception=exc,
                        traceback_text=traceback.format_exc(),
                    )

                if status:
                    self.metrics.record_response(status, bytes_sent)
                if self.config.access_log:
                    elapsed_ms = (time() - started) * 1000
                    self._log_access(peer, method, target, status, bytes_sent, elapsed_ms)
        finally:
            self.metrics.active_connections -= 1
            if local_connection_acquired:
                self.connection_limiter.release(client_key)
            if shared_connection_acquired:
                self.shared_zones.release_connection(client_key)
            writer.close()
            with contextlib.suppress(ConnectionError, RuntimeError):
                await writer.wait_closed()

    async def _handle_http2_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        peer: object,
    ) -> None:
        h2_connection_cls, h2_config_cls, events = load_h2()
        h2_config = h2_config_cls(client_side=False, header_encoding="utf-8")
        connection = h2_connection_cls(config=h2_config)
        connection.initiate_connection()
        writer.write(connection.data_to_send())
        await writer.drain()

        streams: dict[int, dict[str, object]] = {}
        send_lock = asyncio.Lock()
        window_waiters: dict[int, asyncio.Event] = {}
        tasks: set[asyncio.Task[None]] = set()

        async def flush() -> None:
            data = connection.data_to_send()
            if data:
                writer.write(data)
                await writer.drain()

        while True:
            data = await reader.read(self.config.chunk_size)
            if not data:
                break
            for event in connection.receive_data(data):
                if isinstance(event, events.RequestReceived):
                    streams[event.stream_id] = {
                        "headers": event.headers,
                        "body": bytearray(),
                        "started": time(),
                    }
                elif isinstance(event, events.DataReceived):
                    state = streams.setdefault(
                        event.stream_id,
                        {"headers": [], "body": bytearray(), "started": time()},
                    )
                    body = state["body"]
                    if isinstance(body, bytearray):
                        body.extend(event.data)
                    connection.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                    if isinstance(body, bytearray) and len(body) > self.config.max_body_bytes:
                        task = asyncio.create_task(
                            self._send_http2_response(
                                connection,
                                writer,
                                send_lock,
                                window_waiters,
                                event.stream_id,
                                Request("GET", "/", "HTTP/2", {}, b""),
                                peer,
                                forced_response=self._build_error_http_response(413, "Payload Too Large"),
                            )
                        )
                        tasks.add(task)
                        task.add_done_callback(tasks.discard)
                elif isinstance(event, events.StreamEnded):
                    state = streams.pop(event.stream_id, None)
                    if state is None:
                        continue
                    request = h2_request_from_state(state)
                    task = asyncio.create_task(
                        self._send_http2_response(
                            connection,
                            writer,
                            send_lock,
                            window_waiters,
                            event.stream_id,
                            request,
                            peer,
                        )
                    )
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
                elif isinstance(event, events.WindowUpdated):
                    if event.stream_id == 0:
                        waiters = list(window_waiters.values())
                        window_waiters.clear()
                        for waiter in waiters:
                            waiter.set()
                    else:
                        waiter = window_waiters.pop(event.stream_id, None)
                        if waiter is not None:
                            waiter.set()
                elif isinstance(event, (events.StreamReset, events.ConnectionTerminated)):
                    streams.pop(getattr(event, "stream_id", -1), None)

            async with send_lock:
                await flush()

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _send_http2_response(
        self,
        connection: object,
        writer: asyncio.StreamWriter,
        send_lock: asyncio.Lock,
        window_waiters: dict[int, asyncio.Event],
        stream_id: int,
        request: Request,
        peer: object,
        forced_response: HttpResponse | None = None,
    ) -> None:
        started = time()
        try:
            response = forced_response or await self._build_http2_response(request, peer)
        except Exception:
            response = self._build_error_http_response(500, STATUS_TEXT[500])

        async with send_lock:
            headers = h2_response_headers(response.status, response.headers)
            body = b"" if request.method == "HEAD" else response.body
            connection.send_headers(stream_id, headers, end_stream=not body)
            writer.write(connection.data_to_send())
            await writer.drain()
            await self._send_http2_body(connection, writer, window_waiters, stream_id, body)

        self.metrics.record_response(response.status, len(response.body))
        self._queue_ai_error_repair(response.status, STATUS_TEXT.get(response.status, "Error"), request, peer, None, "HTTP/2")
        if self.config.access_log:
            elapsed_ms = (time() - started) * 1000
            self._log_access(peer, request.method, request.target, response.status, len(response.body), elapsed_ms)

    async def _send_http2_body(
        self,
        connection: object,
        writer: asyncio.StreamWriter,
        window_waiters: dict[int, asyncio.Event],
        stream_id: int,
        body: bytes,
    ) -> None:
        remaining = memoryview(body)
        while remaining:
            window = min(
                connection.local_flow_control_window(stream_id),
                connection.max_outbound_frame_size,
            )
            if window <= 0:
                waiter = asyncio.Event()
                window_waiters[stream_id] = waiter
                await waiter.wait()
                continue
            chunk = remaining[:window]
            remaining = remaining[window:]
            connection.send_data(stream_id, chunk.tobytes(), end_stream=not remaining)
            writer.write(connection.data_to_send())
            await writer.drain()

    async def _build_http2_response(self, request: Request, peer: object) -> HttpResponse:
        request = self._apply_rewrites(request)
        path = target_path(request.target)
        plugin_result = self.plugin_manager.check_request(request)
        if not plugin_result.allowed:
            return self._build_error_http_response(plugin_result.status, plugin_result.message)
        waf_result = self.plugin_manager.check_waf(request)
        if not waf_result.allowed:
            return self._build_error_http_response(waf_result.status, waf_result.message)
        for pattern in self.waf_block_path_patterns:
            if pattern.search(path):
                return self._build_error_http_response(403, "Forbidden")

        if path == self.health_path:
            return self._build_health_http_response(request)
        if path == self.metrics_path:
            return self._build_metrics_http_response(request)
        if self._is_admin_path(path):
            return await self._build_admin_http_response(request)

        client_key = remote_addr(peer)
        if not self.rate_limiter.allow(client_key) or not self.shared_zones.allow_request(client_key):
            self.metrics.rate_limited_total += 1
            return self._build_error_http_response(
                429,
                "Too Many Requests",
                extra_headers=[("Retry-After", "60")],
            )

        route = select_route(self.routes, request.target, request_host(request))
        if route is None:
            return self._build_error_http_response(404, "Not Found")
        if not await self._authorize_request(request, route, peer):
            return self._build_error_http_response(
                401,
                "Unauthorized",
                extra_headers=[("WWW-Authenticate", f'Basic realm="{route.auth_realm}"')],
            )
        if route.kind == "static":
            return self._build_static_http_response(request, route)
        if route.kind == "proxy":
            return await self._build_proxy_http_response(request, route, peer)
        if route.kind == "ai":
            return await self._build_ai_http_response(request, route)
        return self._build_error_http_response(500, "Invalid route")

    async def _build_ai_http_response(self, request: Request, route: RouteConfig) -> HttpResponse:
        try:
            return await self._dispatch_ai_request(request, route)
        except AIServiceError as exc:
            body = error_payload(exc.message)
            return HttpResponse(
                exc.status,
                self._with_security_headers(
                    [
                        ("Content-Length", str(len(body))),
                        ("Content-Type", "application/json"),
                        ("Cache-Control", "no-store"),
                    ]
                ),
                body,
            )
        except json.JSONDecodeError as exc:
            body = error_payload(f"invalid JSON: {exc.msg}")
            return HttpResponse(
                400,
                self._with_security_headers(
                    [
                        ("Content-Length", str(len(body))),
                        ("Content-Type", "application/json"),
                        ("Cache-Control", "no-store"),
                    ]
                ),
                body,
            )

    async def _dispatch_ai_request(self, request: Request, route: RouteConfig) -> HttpResponse:
        local_target = route_local_target(route, request.target)
        local_path = target_path(local_target)
        if request.method in {"GET", "HEAD"} and local_path in {"/", "/chat"}:
            if not route.ai_chat_enabled:
                raise AIServiceError(404, "AI chat UI is disabled")
            body = render_chat_page(route)
            return HttpResponse(
                200,
                self._with_security_headers(
                    [
                        ("Content-Length", str(len(body))),
                        ("Content-Type", "text/html; charset=utf-8"),
                        ("Cache-Control", "no-store"),
                    ]
                ),
                body,
            )
        if request.method in {"GET", "HEAD"} and local_path in {"/health", "/v1/health"}:
            body = health_payload(route)
            return json_http_response(200, self._with_security_headers, body)
        if request.method in {"GET", "HEAD"} and local_path == "/v1/models":
            if not route.ai_api_enabled:
                raise AIServiceError(404, "AI API is disabled")
            body = models_payload(route)
            return json_http_response(200, self._with_security_headers, body)
        if request.method != "POST":
            return self._build_error_http_response(405, "Method Not Allowed", extra_headers=[("Allow", "GET, HEAD, POST")])
        if not route.ai_api_enabled:
            raise AIServiceError(404, "AI API is disabled")

        payload = json.loads(request.body.decode("utf-8") if request.body else "{}")
        if not isinstance(payload, dict):
            raise AIServiceError(400, "AI request body must be a JSON object")
        max_tokens = int(payload.get("max_tokens", route.ai_max_tokens))
        temperature = float(payload.get("temperature", route.ai_temperature))
        messages = parse_chat_messages(payload)
        completion = await self.ai_models.complete_chat(
            route,
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )

        if local_path in {"/v1/chat/completions", "/chat"}:
            if bool(payload.get("stream", False)):
                body = sse_chat_payload(route, completion)
                return HttpResponse(
                    200,
                    self._with_security_headers(
                        [
                            ("Content-Length", str(len(body))),
                            ("Content-Type", "text/event-stream; charset=utf-8"),
                            ("Cache-Control", "no-store"),
                        ]
                    ),
                    body,
                )
            if local_path == "/chat":
                body = json.dumps({"reply": completion.text}, separators=(",", ":")).encode("utf-8")
            else:
                body = chat_completion_payload(route, completion)
            return json_http_response(200, self._with_security_headers, body)
        if local_path == "/v1/completions":
            body = text_completion_payload(route, completion)
            return json_http_response(200, self._with_security_headers, body)
        raise AIServiceError(404, "AI endpoint not found")

    def _build_health_http_response(self, request: Request) -> HttpResponse:
        if request.method not in {"GET", "HEAD"}:
            return self._build_error_http_response(405, "Method Not Allowed", extra_headers=[("Allow", "GET, HEAD")])
        body = b'{"status":"ok"}\n'
        return HttpResponse(
            200,
            self._with_security_headers(
                [
                    ("Content-Length", str(len(body))),
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-store"),
                    ("X-VeloxServer-Generation", str(self.generation)),
                ]
            ),
            body,
        )

    def _build_metrics_http_response(self, request: Request) -> HttpResponse:
        if request.method not in {"GET", "HEAD"}:
            return self._build_error_http_response(405, "Method Not Allowed", extra_headers=[("Allow", "GET, HEAD")])
        body = self.metrics.render_prometheus()
        return HttpResponse(
            200,
            self._with_security_headers(
                [
                    ("Content-Length", str(len(body))),
                    ("Content-Type", "text/plain; version=0.0.4"),
                    ("Cache-Control", "no-store"),
                ]
            ),
            body,
        )

    def _normalize_admin_path(self, path: str) -> str:
        if not path.startswith("/"):
            path = f"/{path}"
        if path != "/":
            path = path.rstrip("/")
        return path

    def _admin_route_path(self, suffix: str) -> str:
        admin_prefix = self._normalize_admin_path(self.config.admin_path)
        suffix_path = self._normalize_admin_path(suffix)
        if admin_prefix == "/":
            return suffix_path
        if suffix_path == "/":
            return admin_prefix
        return f"{admin_prefix}{suffix_path}"

    def _is_admin_path(self, path: str) -> bool:
        if not self.config.admin_enabled:
            return False
        return path in {
            self._normalize_admin_path(self.config.admin_path),
            self._admin_route_path(self.config.admin_reload_path),
            self._admin_route_path(self.config.admin_status_path),
        }

    async def _build_admin_http_response(self, request: Request) -> HttpResponse:
        path = target_path(request.target)
        if path == self._admin_route_path(self.config.admin_reload_path):
            if request.method not in {"POST", "PUT", "PATCH"}:
                return self._build_error_http_response(405, "Method Not Allowed", extra_headers=[("Allow", "POST, PUT, PATCH")])
            if self.config.config_path is None:
                return self._build_error_http_response(500, "Reload not available")
            try:
                if not self.reload_runtime_config():
                    raise RuntimeError("reload_runtime_config failed")
            except Exception as exc:
                return self._build_error_http_response(500, f"Reload failed: {exc}")
            body = json.dumps({"status": "reloaded", "generation": self.generation}).encode("utf-8")
            return json_http_response(200, self._with_security_headers, body)
        if path == self._admin_route_path(self.config.admin_status_path):
            body = json.dumps(
                {
                    "status": "ready",
                    "generation": self.generation,
                    "active_connections": self.metrics.active_connections,
                    "requests_total": self.metrics.requests_total,
                    "proxy_retries_total": self.metrics.proxy_retries_total,
                }
            ).encode("utf-8")
            return json_http_response(200, self._with_security_headers, body)
        body = json.dumps(
            {
                "status": "ok",
                "generation": self.generation,
                "routes": [route.path for route in self.routes],
                "admin_enabled": self.config.admin_enabled,
            }
        ).encode("utf-8")
        return json_http_response(200, self._with_security_headers, body)

    def _build_static_http_response(self, request: Request, route: RouteConfig) -> HttpResponse:
        if request.method not in {"GET", "HEAD"}:
            return self._build_error_http_response(405, "Method Not Allowed", extra_headers=[("Allow", "GET, HEAD")])
        if route.root is None:
            return self._build_error_http_response(500, "Invalid static route")

        path = resolve_static_target(route.root.resolve(), route, request.target)
        path_info = self.file_cache.info(path)
        if path_info.is_dir:
            index_path = path / route.index
            index_info = self.file_cache.info(index_path)
            if index_info.is_file:
                path = index_path
                path_info = index_info
            elif route.directory_listing:
                return self._build_directory_http_response(request, route, path)
            else:
                return self._build_error_http_response(403, "Forbidden")

        if not path_info.exists or not path_info.is_file:
            if path_info.error_status == 403:
                return self._build_error_http_response(403, "Forbidden")
            return self._build_error_http_response(404, "Not Found")

        precompressed = select_precompressed(path, request) if route.precompressed else None
        if precompressed is not None:
            encoded_path, encoding = precompressed
            body = self.file_cache.read(encoded_path)
            stat = encoded_path.stat()
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            return HttpResponse(
                200,
                self._with_security_headers(
                    [
                        ("Content-Length", str(len(body))),
                        ("Content-Type", content_type),
                        ("Content-Encoding", encoding),
                        ("Vary", "Accept-Encoding"),
                        ("ETag", make_etag(stat)),
                        ("Last-Modified", email.utils.formatdate(stat.st_mtime, usegmt=True)),
                    ]
                ),
                body,
            )

        stat = path_info.stat or path.stat()
        etag = make_etag(stat)
        last_modified = email.utils.formatdate(stat.st_mtime, usegmt=True)
        if request.headers.get("if-none-match") == etag or is_not_modified(
            request.headers.get("if-modified-since"), stat.st_mtime
        ):
            return HttpResponse(
                304,
                self._with_security_headers([("ETag", etag), ("Last-Modified", last_modified)]),
                b"",
            )

        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        body = self.file_cache.read(path)
        if should_gzip(request, content_type, stat.st_size, self.config):
            body = gzip.compress(body)
            headers = [
                ("Content-Length", str(len(body))),
                ("Content-Type", content_type),
                ("Content-Encoding", "gzip"),
                ("Vary", "Accept-Encoding"),
                ("ETag", f'{etag[:-1]}-gzip"'),
                ("Last-Modified", last_modified),
            ]
        else:
            headers = [
                ("Content-Length", str(len(body))),
                ("Content-Type", content_type),
                ("ETag", etag),
                ("Last-Modified", last_modified),
            ]
        return HttpResponse(200, self._with_security_headers(headers), body)

    def _build_directory_http_response(
        self,
        request: Request,
        route: RouteConfig,
        path: Path,
    ) -> HttpResponse:
        if route.root is None:
            return self._build_error_http_response(500, "Invalid static route")
        root = route.root.resolve()
        relative = "/" if path == root else f"/{path.relative_to(root).as_posix().strip('/')}/"
        items = []
        for child in sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
            name = f"{child.name}/" if child.is_dir() else child.name
            items.append(f'<li><a href="{name}">{name}</a></li>')
        body = (
            "<!doctype html><meta charset=\"utf-8\">"
            f"<title>Index of {relative}</title>"
            f"<h1>Index of {relative}</h1><ul>{''.join(items)}</ul>"
        ).encode("utf-8")
        return HttpResponse(
            200,
            self._with_security_headers(
                [
                    ("Content-Length", str(len(body))),
                    ("Content-Type", "text/html; charset=utf-8"),
                ]
            ),
            body,
        )

    async def _build_proxy_http_response(
        self,
        request: Request,
        route: RouteConfig,
        peer: object,
    ) -> HttpResponse:
        if request.method not in PROXY_METHODS:
            return self._build_error_http_response(
                405,
                "Method Not Allowed",
                extra_headers=[("Allow", ", ".join(sorted(PROXY_METHODS)))],
            )
        runtime = self.route_runtimes.get(route.path)
        if runtime is None:
            return self._build_error_http_response(500, "Invalid proxy route")

        cache_key = self._build_proxy_cache_key(route, request, peer, "h2")
        if request.method == "PURGE":
            if not route.proxy_cache_purge:
                return self._build_error_http_response(405, "Method Not Allowed")
            purge_key = self._build_proxy_cache_key(route, replace(request, method="GET"), peer, "h2")
            removed = self._proxy_cache_purge(runtime, route, purge_key)
            body = b"purged\n" if removed else b"not found\n"
            return HttpResponse(
                200 if removed else 404,
                [("Content-Length", str(len(body))), ("Content-Type", "text/plain; charset=utf-8")],
                body,
            )
        if route.proxy_cache and request.method in route.proxy_cache_methods:
            cached = self._proxy_cache_get(runtime, route, cache_key, route.proxy_cache_stale_while_revalidate)
            if cached is not None:
                self.metrics.proxy_cache_hits_total += 1
                if cached.stale:
                    asyncio.create_task(self._refresh_proxy_cache(request, route, runtime, peer, cache_key))
                return HttpResponse(cached.status, decode_cached_h2_headers(cached.head), cached.body)

        attempts = runtime.select_attempts(remote_addr(peer), request.target)
        if not attempts:
            self.metrics.circuit_open_total += 1
            if route.proxy_fallback_path is not None and route.proxy_fallback_path.exists() and route.proxy_fallback_path.is_file():
                body = route.proxy_fallback_path.read_bytes()
                content_type = mimetypes.guess_type(route.proxy_fallback_path.name)[0] or "application/octet-stream"
                return HttpResponse(
                    200,
                    self._with_security_headers(
                        [
                            ("Content-Length", str(len(body))),
                            ("Content-Type", content_type),
                        ]
                    ),
                    body,
                )
            return self._build_error_http_response(503, "No healthy upstream")

        last_status = 502
        last_message = "Bad Gateway"
        for index, state in enumerate(attempts):
            try:
                response = await self._fetch_upstream_http_response(request, route, runtime, state, peer, cache_key)
                if response.status >= 500:
                    self._record_upstream_failure(route, state)
                else:
                    state.record_success()
                return response
            except UpstreamUnavailable as exc:
                last_status = exc.status
                last_message = exc.message
                self._record_upstream_failure(route, state)
                if index < len(attempts) - 1:
                    self.metrics.proxy_retries_total += 1
        if route.proxy_fallback_path is not None and route.proxy_fallback_path.exists() and route.proxy_fallback_path.is_file():
            body = route.proxy_fallback_path.read_bytes()
            content_type = mimetypes.guess_type(route.proxy_fallback_path.name)[0] or "application/octet-stream"
            return HttpResponse(
                200,
                self._with_security_headers(
                    [
                        ("Content-Length", str(len(body))),
                        ("Content-Type", content_type),
                    ]
                ),
                body,
            )
        return self._build_error_http_response(last_status, last_message)

    async def _fetch_upstream_http_response(
        self,
        request: Request,
        route: RouteConfig,
        runtime: RouteRuntime,
        state: UpstreamState,
        peer: object,
        cache_key: str,
    ) -> HttpResponse:
        upstream = state.upstream
        upstream_target = build_upstream_target(route, request.target, upstream.base_path)
        timeout = self.config.proxy_timeout
        state.active_connections += 1
        try:
            upstream_reader, upstream_writer = await connect_upstream(upstream, timeout)
            upstream_writer.write(render_upstream_request(request, upstream, upstream_target, peer, "https"))
            await upstream_writer.drain()
            response_head = await asyncio.wait_for(upstream_reader.readuntil(HEADER_END), timeout=timeout)
            status, headers = parse_upstream_response_headers(response_head)
            body = await read_upstream_response_body(
                upstream_reader,
                headers,
                timeout,
                self.config.proxy_buffer_bytes,
                decode_chunked=True,
            )
            upstream_writer.close()
            with contextlib.suppress(ConnectionError, RuntimeError):
                await upstream_writer.wait_closed()
        except asyncio.TimeoutError as exc:
            raise UpstreamUnavailable(504, "Gateway Timeout") from exc
        except (OSError, asyncio.IncompleteReadError, HttpError) as exc:
            raise UpstreamUnavailable(502, "Bad Gateway") from exc
        finally:
            state.active_connections = max(0, state.active_connections - 1)

        filtered_headers = filter_h2_upstream_headers(headers)
        response = HttpResponse(status, filtered_headers, body)
        if route.proxy_cache and request.method in route.proxy_cache_methods and status in {200, 203, 204, 301, 302, 404}:
            self._proxy_cache_put(runtime, route, cache_key, status, encode_cached_h2_headers(filtered_headers), response.body)
        return response

    def _build_error_http_response(
        self,
        status: int,
        message: str,
        extra_headers: list[tuple[str, str]] | None = None,
    ) -> HttpResponse:
        body = f"{status} {message}\n".encode("utf-8")
        content_type = "text/plain; charset=utf-8"
        error_page = self.error_pages.get(status)
        if error_page is not None and error_page.exists() and error_page.is_file():
            body = error_page.read_bytes()
            content_type = mimetypes.guess_type(error_page.name)[0] or "text/html; charset=utf-8"
        headers = [
            ("Content-Length", str(len(body))),
            ("Content-Type", content_type),
        ]
        if extra_headers:
            headers.extend(extra_headers)
        return HttpResponse(status, self._with_security_headers(headers), body)

    def _log_access(
        self,
        peer: object,
        method: str,
        target: str,
        status: int,
        bytes_sent: int,
        elapsed_ms: float,
    ) -> None:
        if self.config.log_format == "json":
            self.access_logger.write(
                json.dumps(
                    {
                        "ts": email.utils.formatdate(usegmt=True),
                        "remote_addr": remote_addr(peer),
                        "method": method,
                        "target": target,
                        "status": status,
                        "bytes_sent": bytes_sent,
                        "duration_ms": round(elapsed_ms, 3),
                    },
                    separators=(",", ":"),
                )
            )
            return
        self.access_logger.write(f"{peer} {method} {target} {status} {bytes_sent} {elapsed_ms:.2f}ms")

    async def _read_headers(self, reader: asyncio.StreamReader) -> bytes:
        try:
            raw = await reader.readuntil(HEADER_END)
        except asyncio.LimitOverrunError as exc:
            await reader.readexactly(exc.consumed)
            raise HttpError(431)
        except asyncio.IncompleteReadError as exc:
            if not exc.partial:
                return b""
            raise

        if len(raw) > self.config.max_header_bytes:
            raise HttpError(431)
        return raw

    def _parse_request(self, raw: bytes) -> Request:
        if self.native_core is not None and self.native_core.supports_request_parser:
            parsed = self.native_core.parse_request_head(raw)
            if parsed is not None and "error" not in parsed:
                headers = parsed.get("headers", {})
                if isinstance(headers, dict):
                    return Request(
                        str(parsed.get("method", "")),
                        str(parsed.get("target", "")),
                        str(parsed.get("version", "")),
                        {str(name).lower(): str(value) for name, value in headers.items()},
                    )
        return parse_request(raw)

    async def _read_body(self, reader: asyncio.StreamReader, request: Request) -> bytes:
        transfer_encoding = request.headers.get("transfer-encoding", "").lower()
        if transfer_encoding == "chunked":
            return await self._read_chunked_body(reader)

        content_length = request.headers.get("content-length")
        if not content_length:
            return b""

        try:
            length = int(content_length)
        except ValueError as exc:
            raise HttpError(400, "Bad Request") from exc

        if length < 0:
            raise HttpError(400, "Bad Request")
        if length > self.config.max_body_bytes:
            raise HttpError(413, "Payload Too Large")
        return await reader.readexactly(length)

    async def _read_chunked_body(self, reader: asyncio.StreamReader) -> bytes:
        body = bytearray()
        while True:
            size_line = await reader.readline()
            if not size_line:
                raise HttpError(400, "Bad Request")
            size_text = size_line.split(b";", 1)[0].strip()
            try:
                size = int(size_text, 16)
            except ValueError as exc:
                raise HttpError(400, "Bad Request") from exc
            if size == 0:
                while True:
                    trailer_line = await reader.readline()
                    if trailer_line in {b"", b"\r\n"}:
                        break
                break
            if len(body) + size > self.config.max_body_bytes:
                raise HttpError(413, "Payload Too Large")
            body.extend(await reader.readexactly(size))
            if await reader.readexactly(2) != b"\r\n":
                raise HttpError(400, "Bad Request")
        return bytes(body)

    async def _serve_request(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        request: Request,
        keep_alive: bool,
        peer: object,
    ) -> ResponseOutcome:
        request = self._apply_rewrites(request)
        path = target_path(request.target)
        plugin_result = self.plugin_manager.check_request(request)
        if not plugin_result.allowed:
            bytes_sent = await self._send_error(writer, plugin_result.status, plugin_result.message, keep_alive=False, request=request, peer=peer)
            return ResponseOutcome(plugin_result.status, False, bytes_sent)
        waf_result = self.plugin_manager.check_waf(request)
        if not waf_result.allowed:
            bytes_sent = await self._send_error(writer, waf_result.status, waf_result.message, keep_alive=False, request=request, peer=peer)
            return ResponseOutcome(waf_result.status, False, bytes_sent)
        for pattern in self.waf_block_path_patterns:
            if pattern.search(path):
                bytes_sent = await self._send_error(writer, 403, "Forbidden", keep_alive=False, request=request, peer=peer)
                return ResponseOutcome(403, False, bytes_sent)

        if path == self.health_path:
            return await self._serve_health(writer, request, keep_alive)
        if path == self.metrics_path:
            return await self._serve_metrics(writer, request, keep_alive)
        if self._is_admin_path(path):
            return await self._serve_admin(writer, request, keep_alive)

        client_key = remote_addr(peer)
        if not self.rate_limiter.allow(client_key) or not self.shared_zones.allow_request(client_key):
            self.metrics.rate_limited_total += 1
            bytes_sent = await self._send_error(
                writer,
                429,
                "Too Many Requests",
                keep_alive=False,
                extra_headers=[("Retry-After", "60")],
                request=request,
                peer=peer,
            )
            return ResponseOutcome(429, False, bytes_sent)

        route = select_route(self.routes, request.target, request_host(request))
        if route is None:
            bytes_sent = await self._send_error(writer, 404, "Not Found", keep_alive=keep_alive, request=request, peer=peer)
            return ResponseOutcome(404, keep_alive, bytes_sent)

        if not await self._authorize_request(request, route, peer):
            bytes_sent = await self._send_error(
                writer,
                401,
                "Unauthorized",
                keep_alive=False,
                extra_headers=[("WWW-Authenticate", f'Basic realm="{route.auth_realm}"')],
                request=request,
                peer=peer,
                route=route,
            )
            return ResponseOutcome(401, False, bytes_sent)

        if route.kind == "static":
            return await self._serve_static(writer, request, route, keep_alive)
        if route.kind == "proxy":
            return await self._serve_proxy(reader, writer, request, route, peer)
        if route.kind == "ai":
            return await self._serve_ai(writer, request, route, keep_alive)

        bytes_sent = await self._send_error(writer, 500, "Invalid route", keep_alive=False, request=request, peer=peer, route=route)
        return ResponseOutcome(500, False, bytes_sent)

    async def _authorize_request(self, request: Request, route: RouteConfig, peer: object) -> bool:
        if not is_authorized(request, route):
            return False
        if route.jwt_jwks_file is not None or route.jwt_jwks_url is not None:
            if not await self._check_jwks_auth(request, route):
                return False
        plugin_result = self.plugin_manager.check_auth(request)
        if not plugin_result.allowed:
            return False
        if route.external_auth_url is not None:
            if not await self._check_external_auth(request, route.external_auth_url, route.external_auth_timeout, peer):
                return False
        if route.auth_request is not None:
            if not await self._check_auth_subrequest(request, route.auth_request, route.auth_request_timeout, peer):
                return False
        return True

    async def _check_jwks_auth(self, request: Request, route: RouteConfig) -> bool:
        authorization = request.headers.get("authorization", "")
        if not authorization.lower().startswith("bearer "):
            return False
        jwks = await self._load_jwks(route)
        if jwks is None:
            return False
        token = authorization.split(None, 1)[1]
        claims = verify_rs256_jwt(token, jwks)
        if not claims.valid:
            return False
        return claims_match(claims.payload, route.jwt_issuer, route.jwt_audience, route.jwt_required_claims)

    async def _load_jwks(self, route: RouteConfig) -> dict[str, object] | None:
        if route.jwt_jwks_file is not None:
            try:
                return json.loads(route.jwt_jwks_file.read_text(encoding="utf-8"))
            except Exception:
                return None
        if route.jwt_jwks_url is None:
            return None
        cached = self.jwks_cache.get(route.jwt_jwks_url)
        now = time()
        if cached is not None and cached[0] > now:
            return cached[1]
        try:
            jwks = await asyncio.to_thread(fetch_json_url, route.jwt_jwks_url, 5.0)
        except Exception:
            return None
        self.jwks_cache[route.jwt_jwks_url] = (now + max(1.0, route.jwt_jwks_cache_ttl), jwks)
        return jwks

    async def _check_external_auth(self, request: Request, url: str, timeout: float, peer: object) -> bool:
        parsed = urlsplit(url)
        if parsed.scheme != "http" or parsed.hostname is None:
            return False
        target = parsed.path or "/"
        if parsed.query:
            target = f"{target}?{parsed.query}"
        port = parsed.port or 80
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(parsed.hostname, port), timeout=timeout)
            headers = [
                f"GET {target} HTTP/1.1",
                f"Host: {parsed.hostname if port == 80 else f'{parsed.hostname}:{port}'}",
                f"X-Original-Method: {request.method}",
                f"X-Original-URI: {request.target}",
                f"X-Forwarded-For: {remote_addr(peer)}",
                "Connection: close",
            ]
            authorization = request.headers.get("authorization")
            if authorization:
                headers.append(f"Authorization: {authorization}")
            writer.write(("\r\n".join(headers) + "\r\n\r\n").encode("iso-8859-1"))
            await writer.drain()
            head = await asyncio.wait_for(reader.readuntil(HEADER_END), timeout=timeout)
            writer.close()
            with contextlib.suppress(ConnectionError, RuntimeError):
                await writer.wait_closed()
            status = parse_upstream_response_status(head)
            return 200 <= status < 300
        except Exception:
            return False

    async def _check_auth_subrequest(self, request: Request, target: str, timeout: float, peer: object) -> bool:
        if target.startswith("http://"):
            return await self._check_external_auth(request, target, timeout, peer)
        if target == request.target:
            return False
        subrequest = Request(
            "GET",
            target,
            request.version,
            {
                **request.headers,
                "x-original-method": request.method,
                "x-original-uri": request.target,
            },
        )
        route = select_route(self.routes, subrequest.target, request_host(subrequest))
        if route is None:
            return False
        try:
            if route.kind == "static":
                response = self._build_static_http_response(subrequest, route)
            elif route.kind == "proxy":
                runtime = self.route_runtimes.get(route.path)
                if runtime is None:
                    return False
                cache_key = self._build_proxy_cache_key(route, subrequest, peer, "auth")
                attempts = runtime.select_attempts(remote_addr(peer), subrequest.target)
                if not attempts:
                    return False
                response = await asyncio.wait_for(
                    self._build_proxy_http_response(subrequest, route, peer),
                    timeout=timeout,
                )
            else:
                return False
        except Exception:
            return False
        return 200 <= response.status < 300

    def _apply_rewrites(self, request: Request) -> Request:
        if not self.rewrite_rules and not self.advanced_rewrite_rules:
            return request
        target = request.target
        for pattern, replacement in self.rewrite_rules:
            rewritten = pattern.sub(replacement, target, count=1)
            if rewritten != target:
                target = rewritten
                break
        rewritten_request = replace(request, target=target)
        for rule in self.advanced_rewrite_rules:
            if not advanced_rewrite_matches(rule, rewritten_request):
                continue
            rewritten = apply_advanced_rewrite(rule, rewritten_request)
            if rewritten != rewritten_request.target:
                rewritten_request = replace(rewritten_request, target=rewritten)
            if rule.stop:
                break
        return rewritten_request

    async def _serve_health(
        self,
        writer: asyncio.StreamWriter,
        request: Request,
        keep_alive: bool,
    ) -> ResponseOutcome:
        if request.method not in {"GET", "HEAD"}:
            bytes_sent = await self._send_error(
                writer,
                405,
                "Method Not Allowed",
                keep_alive=keep_alive,
                extra_headers=[("Allow", "GET, HEAD")],
            )
            return ResponseOutcome(405, keep_alive, bytes_sent)

        body = b'{"status":"ok"}\n'
        headers = [
            ("Content-Length", str(len(body))),
            ("Content-Type", "application/json"),
            ("Cache-Control", "no-store"),
            ("Connection", "keep-alive" if keep_alive else "close"),
            ("X-VeloxServer-Generation", str(self.generation)),
        ]
        writer.write(render_headers(200, self._with_security_headers(headers)))
        if request.method != "HEAD":
            writer.write(body)
        await writer.drain()
        return ResponseOutcome(200, keep_alive, len(body))

    async def _serve_metrics(
        self,
        writer: asyncio.StreamWriter,
        request: Request,
        keep_alive: bool,
    ) -> ResponseOutcome:
        if request.method not in {"GET", "HEAD"}:
            bytes_sent = await self._send_error(
                writer,
                405,
                "Method Not Allowed",
                keep_alive=keep_alive,
                extra_headers=[("Allow", "GET, HEAD")],
            )
            return ResponseOutcome(405, keep_alive, bytes_sent)

        body = self.metrics.render_prometheus()
        headers = [
            ("Content-Length", str(len(body))),
            ("Content-Type", "text/plain; version=0.0.4"),
            ("Cache-Control", "no-store"),
            ("Connection", "keep-alive" if keep_alive else "close"),
        ]
        writer.write(render_headers(200, self._with_security_headers(headers)))
        if request.method != "HEAD":
            writer.write(body)
        await writer.drain()
        return ResponseOutcome(200, keep_alive, len(body))

    async def _serve_admin(self, writer: asyncio.StreamWriter, request: Request, keep_alive: bool) -> ResponseOutcome:
        path = target_path(request.target)
        if path == self._admin_route_path(self.config.admin_reload_path):
            return await self._serve_admin_reload(writer, request, keep_alive)
        if path == self._admin_route_path(self.config.admin_status_path):
            return await self._serve_admin_status(writer, request, keep_alive)
        if request.method not in {"GET", "HEAD"}:
            bytes_sent = await self._send_error(writer, 405, "Method Not Allowed", keep_alive=False, extra_headers=[("Allow", "GET, HEAD")])
            return ResponseOutcome(405, False, bytes_sent)
        body = json.dumps(
            {
                "status": "ok",
                "generation": self.generation,
                "routes": [route.path for route in self.routes],
                "admin_enabled": self.config.admin_enabled,
            }
        ).encode("utf-8")
        headers = self._with_security_headers(
            [
                ("Content-Length", str(len(body))),
                ("Content-Type", "application/json"),
                ("Connection", "keep-alive" if keep_alive else "close"),
            ]
        )
        writer.write(render_headers(200, headers))
        if request.method != "HEAD":
            writer.write(body)
        await writer.drain()
        return ResponseOutcome(200, keep_alive, len(body))

    async def _serve_admin_status(self, writer: asyncio.StreamWriter, request: Request, keep_alive: bool) -> ResponseOutcome:
        if request.method not in {"GET", "HEAD"}:
            bytes_sent = await self._send_error(writer, 405, "Method Not Allowed", keep_alive=False, extra_headers=[("Allow", "GET, HEAD")])
            return ResponseOutcome(405, False, bytes_sent)
        body = json.dumps(
            {
                "status": "ready",
                "generation": self.generation,
                "active_connections": self.metrics.active_connections,
                "requests_total": self.metrics.requests_total,
                "proxy_retries_total": self.metrics.proxy_retries_total,
            }
        ).encode("utf-8")
        headers = self._with_security_headers(
            [
                ("Content-Length", str(len(body))),
                ("Content-Type", "application/json"),
                ("Connection", "keep-alive" if keep_alive else "close"),
            ]
        )
        writer.write(render_headers(200, headers))
        if request.method != "HEAD":
            writer.write(body)
        await writer.drain()
        return ResponseOutcome(200, keep_alive, len(body))

    async def _serve_admin_reload(self, writer: asyncio.StreamWriter, request: Request, keep_alive: bool) -> ResponseOutcome:
        if request.method not in {"POST", "PUT", "PATCH"}:
            bytes_sent = await self._send_error(writer, 405, "Method Not Allowed", keep_alive=False, extra_headers=[("Allow", "POST, PUT, PATCH")])
            return ResponseOutcome(405, False, bytes_sent)
        if self.config.config_path is None:
            bytes_sent = await self._send_error(writer, 500, "Reload not available", keep_alive=False)
            return ResponseOutcome(500, False, bytes_sent)
        try:
            if not self.reload_runtime_config():
                raise RuntimeError("reload_runtime_config failed")
        except Exception as exc:
            bytes_sent = await self._send_error(writer, 500, f"Reload failed: {exc}", keep_alive=False)
            return ResponseOutcome(500, False, bytes_sent)
        body = json.dumps({"status": "reloaded", "generation": self.generation}).encode("utf-8")
        headers = self._with_security_headers(
            [
                ("Content-Length", str(len(body))),
                ("Content-Type", "application/json"),
                ("Connection", "keep-alive" if keep_alive else "close"),
            ]
        )
        writer.write(render_headers(200, headers))
        writer.write(body)
        await writer.drain()
        return ResponseOutcome(200, keep_alive, len(body))

    async def _serve_static(
        self,
        writer: asyncio.StreamWriter,
        request: Request,
        route: RouteConfig,
        keep_alive: bool,
    ) -> ResponseOutcome:
        if request.method not in {"GET", "HEAD"}:
            bytes_sent = await self._send_error(
                writer,
                405,
                "Method Not Allowed",
                keep_alive=keep_alive,
                extra_headers=[("Allow", "GET, HEAD")],
            )
            return ResponseOutcome(405, keep_alive, bytes_sent)

        if route.root is None:
            raise HttpError(500, "Invalid static route")

        native_outcome = await self._serve_static_native(writer, request, route, keep_alive)
        if native_outcome is not None:
            return native_outcome

        path = resolve_static_target(route.root.resolve(), route, request.target)
        path_info = self.file_cache.info(path)
        if path_info.is_dir:
            index_path = path / route.index
            index_info = self.file_cache.info(index_path)
            if index_info.is_file:
                path = index_path
                path_info = index_info
            elif route.directory_listing:
                return await self._serve_directory_listing(writer, request, route, path, keep_alive)
            else:
                bytes_sent = await self._send_error(writer, 403, "Forbidden", keep_alive=keep_alive)
                return ResponseOutcome(403, keep_alive, bytes_sent)

        if not path_info.exists or not path_info.is_file:
            if path_info.error_status == 403:
                bytes_sent = await self._send_error(writer, 403, "Forbidden", keep_alive=keep_alive)
                return ResponseOutcome(403, keep_alive, bytes_sent)
            bytes_sent = await self._send_error(writer, 404, "Not Found", keep_alive=keep_alive)
            return ResponseOutcome(404, keep_alive, bytes_sent)

        precompressed = select_precompressed(path, request) if route.precompressed else None
        if precompressed is not None:
            encoded_path, encoding = precompressed
            stat = encoded_path.stat()
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            headers = [
                ("Content-Length", str(stat.st_size)),
                ("Content-Type", content_type),
                ("Content-Encoding", encoding),
                ("Vary", "Accept-Encoding"),
                ("ETag", make_etag(stat)),
                ("Last-Modified", email.utils.formatdate(stat.st_mtime, usegmt=True)),
                ("Connection", "keep-alive" if keep_alive else "close"),
            ]
            writer.write(render_headers(200, self._with_security_headers(headers)))
            await writer.drain()
            bytes_sent = 0
            if request.method != "HEAD":
                bytes_sent = await self._send_file(writer, encoded_path)
            return ResponseOutcome(200, keep_alive, bytes_sent)

        stat = path_info.stat or path.stat()
        etag = make_etag(stat)
        last_modified = email.utils.formatdate(stat.st_mtime, usegmt=True)

        if request.headers.get("if-none-match") == etag or is_not_modified(
            request.headers.get("if-modified-since"), stat.st_mtime
        ):
            headers = [
                ("ETag", etag),
                ("Last-Modified", last_modified),
                ("Connection", "keep-alive" if keep_alive else "close"),
            ]
            writer.write(render_headers(304, self._with_security_headers(headers)))
            await writer.drain()
            return ResponseOutcome(304, keep_alive, 0)

        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        use_gzip = should_gzip(request, content_type, stat.st_size, self.config)
        if use_gzip:
            body = gzip.compress(self.file_cache.read(path))
            headers = [
                ("Content-Length", str(len(body))),
                ("Content-Type", content_type),
                ("Content-Encoding", "gzip"),
                ("Vary", "Accept-Encoding"),
                ("ETag", f'{etag[:-1]}-gzip"'),
                ("Last-Modified", last_modified),
                ("Connection", "keep-alive" if keep_alive else "close"),
            ]
            writer.write(render_headers(200, self._with_security_headers(headers)))
            if request.method != "HEAD":
                writer.write(body)
            await writer.drain()
            return ResponseOutcome(200, keep_alive, len(body))

        headers = [
            ("Content-Length", str(stat.st_size)),
            ("Content-Type", content_type),
            ("ETag", etag),
            ("Last-Modified", last_modified),
            ("Connection", "keep-alive" if keep_alive else "close"),
        ]
        writer.write(render_headers(200, self._with_security_headers(headers)))
        await writer.drain()

        bytes_sent = 0
        if request.method != "HEAD":
            bytes_sent = await self._send_file(writer, path)
        return ResponseOutcome(200, keep_alive, bytes_sent)

    async def _serve_static_native(
        self,
        writer: asyncio.StreamWriter,
        request: Request,
        route: RouteConfig,
        keep_alive: bool,
    ) -> ResponseOutcome | None:
        if self.native_core is None or route.root is None:
            return None
        if not self.native_core.supports_static_response:
            return None
        if self.config.gzip or route.precompressed or route.directory_listing or self.error_pages:
            return None
        if request.headers.get("if-none-match") or request.headers.get("if-modified-since"):
            return None
        native_target = route_local_target(route, request.target)
        result = self.native_core.build_static_response(
            route.root.resolve(),
            native_target,
            request.method,
            route.index,
            keep_alive,
            self.config.security_headers,
        )
        if result is None:
            return None
        writer.write(result.response)
        await writer.drain()
        return ResponseOutcome(result.status, keep_alive, result.body_len)

    async def _serve_directory_listing(
        self,
        writer: asyncio.StreamWriter,
        request: Request,
        route: RouteConfig,
        path: Path,
        keep_alive: bool,
    ) -> ResponseOutcome:
        root = route.root.resolve() if route.root is not None else path
        relative = "/" if path == root else f"/{path.relative_to(root).as_posix().strip('/')}/"
        items = []
        for child in sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
            name = f"{child.name}/" if child.is_dir() else child.name
            items.append(f'<li><a href="{name}">{name}</a></li>')
        body = (
            "<!doctype html><meta charset=\"utf-8\">"
            f"<title>Index of {relative}</title>"
            f"<h1>Index of {relative}</h1><ul>{''.join(items)}</ul>"
        ).encode("utf-8")
        headers = [
            ("Content-Length", str(len(body))),
            ("Content-Type", "text/html; charset=utf-8"),
            ("Connection", "keep-alive" if keep_alive else "close"),
        ]
        writer.write(render_headers(200, self._with_security_headers(headers)))
        if request.method != "HEAD":
            writer.write(body)
        await writer.drain()
        return ResponseOutcome(200, keep_alive, len(body))

    async def _serve_ai(
        self,
        writer: asyncio.StreamWriter,
        request: Request,
        route: RouteConfig,
        keep_alive: bool,
    ) -> ResponseOutcome:
        response = await self._build_ai_http_response(request, route)
        headers = [
            *response.headers,
            ("Connection", "keep-alive" if keep_alive else "close"),
        ]
        writer.write(render_headers(response.status, headers))
        bytes_sent = 0
        if request.method != "HEAD":
            writer.write(response.body)
            bytes_sent = len(response.body)
        await writer.drain()
        return ResponseOutcome(response.status, keep_alive, bytes_sent)

    def _proxy_cache_get(
        self,
        runtime: RouteRuntime,
        route: RouteConfig,
        key: str,
        stale_window: float = 0.0,
    ) -> ProxyCacheRecord | None:
        cached = runtime.cache_get(key, stale_window)
        if cached is not None:
            return cached
        disk_cache = self.proxy_disk_caches.get(route.path)
        if disk_cache is not None:
            return disk_cache.get(key, stale_window)
        return None

    def _proxy_cache_put(
        self,
        runtime: RouteRuntime,
        route: RouteConfig,
        key: str,
        status: int,
        head: bytes,
        body: bytes,
    ) -> None:
        runtime.cache_put(key, status, head, body)
        disk_cache = self.proxy_disk_caches.get(route.path)
        if disk_cache is not None:
            disk_cache.put(key, status, head, body, route.proxy_cache_ttl, route.proxy_cache_max_bytes)

    def _proxy_cache_purge(self, runtime: RouteRuntime, route: RouteConfig, key: str) -> bool:
        removed = runtime.cache_purge(key)
        disk_cache = self.proxy_disk_caches.get(route.path)
        if disk_cache is not None:
            removed = disk_cache.purge(key) or removed
        return removed

    async def _serve_cached_h1(
        self,
        writer: asyncio.StreamWriter,
        request: Request,
        record: ProxyCacheRecord,
    ) -> ResponseOutcome:
        writer.write(record.head)
        if request.method != "HEAD":
            writer.write(record.body)
        await writer.drain()
        self.metrics.proxy_cache_hits_total += 1
        return ResponseOutcome(record.status, False, len(record.body))

    async def _serve_proxy(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        request: Request,
        route: RouteConfig,
        peer: object,
    ) -> ResponseOutcome:
        if request.method not in PROXY_METHODS:
            bytes_sent = await self._send_error(
                writer,
                405,
                "Method Not Allowed",
                keep_alive=False,
                extra_headers=[("Allow", ", ".join(sorted(PROXY_METHODS)))],
            )
            return ResponseOutcome(405, False, bytes_sent)

        runtime = self.route_runtimes.get(route.path)
        if runtime is None:
            raise HttpError(500, "Invalid proxy route")

        scheme = "https" if self.ssl_context else "http"
        cache_key = self._build_proxy_cache_key(route, request, peer, scheme)
        if request.method == "PURGE":
            if not route.proxy_cache_purge:
                bytes_sent = await self._send_error(writer, 405, "Method Not Allowed", keep_alive=False)
                return ResponseOutcome(405, False, bytes_sent)
            purge_key = self._build_proxy_cache_key(route, replace(request, method="GET"), peer, scheme)
            removed = self._proxy_cache_purge(runtime, route, purge_key)
            body = b"purged\n" if removed else b"not found\n"
            status = 200 if removed else 404
            writer.write(
                render_headers(
                    status,
                    [
                        ("Content-Length", str(len(body))),
                        ("Content-Type", "text/plain; charset=utf-8"),
                        ("Connection", "close"),
                    ],
                )
            )
            writer.write(body)
            await writer.drain()
            return ResponseOutcome(status, False, len(body))

        if is_upgrade_request(request):
            attempts = runtime.select_attempts(remote_addr(peer), request.target)
            if not attempts:
                self.metrics.circuit_open_total += 1
                bytes_sent = await self._send_error(writer, 503, "No healthy upstream", keep_alive=False, request=request, peer=peer, route=route)
                return ResponseOutcome(503, False, bytes_sent)

            last_status = 502
            last_message = "Bad Gateway"
            for index, state in enumerate(attempts):
                try:
                    outcome = await self._proxy_upgrade(reader, writer, request, route, state, peer)
                    if outcome.status >= 500:
                        self._record_upstream_failure(route, state)
                    else:
                        state.record_success()
                    return outcome
                except UpstreamUnavailable as exc:
                    last_status = exc.status
                    last_message = exc.message
                    self._record_upstream_failure(route, state)
                    if index < len(attempts) - 1:
                        self.metrics.proxy_retries_total += 1

            bytes_sent = await self._send_error(writer, last_status, last_message, keep_alive=False, request=request, peer=peer, route=route)
            return ResponseOutcome(last_status, False, bytes_sent)

        stale_record = None
        if route.proxy_cache and request.method in route.proxy_cache_methods:
            stale_window = max(route.proxy_cache_stale_while_revalidate, route.proxy_cache_ttl if route.proxy_cache_use_stale_on_error else 0.0)
            cached = self._proxy_cache_get(runtime, route, cache_key, stale_window)
            if cached is not None:
                if not cached.stale or route.proxy_cache_stale_while_revalidate > 0:
                    if cached.stale:
                        asyncio.create_task(self._refresh_proxy_cache(request, route, runtime, peer, cache_key))
                    return await self._serve_cached_h1(writer, request, cached)
                stale_record = cached

        attempts = runtime.select_attempts(remote_addr(peer), request.target)
        if not attempts:
            self.metrics.circuit_open_total += 1
            if route.proxy_fallback_path is not None and route.proxy_fallback_path.exists() and route.proxy_fallback_path.is_file():
                body = route.proxy_fallback_path.read_bytes()
                content_type = mimetypes.guess_type(route.proxy_fallback_path.name)[0] or "application/octet-stream"
                writer.write(
                    render_headers(
                        200,
                        self._with_security_headers(
                            [
                                ("Content-Length", str(len(body))),
                                ("Content-Type", content_type),
                                ("Connection", "close"),
                            ]
                        ),
                    )
                )
                if request.method != "HEAD":
                    writer.write(body)
                await writer.drain()
                return ResponseOutcome(200, False, len(body))
            bytes_sent = await self._send_error(writer, 503, "No healthy upstream", keep_alive=False, request=request, peer=peer, route=route)
            return ResponseOutcome(503, False, bytes_sent)

        last_status = 502
        last_message = "Bad Gateway"
        try:
            if route.proxy_cache and request.method in route.proxy_cache_methods and route.proxy_cache_lock:
                lock = runtime.cache_lock(cache_key)
                async with asyncio.timeout(route.proxy_cache_lock_timeout):
                    async with lock:
                        cached = self._proxy_cache_get(runtime, route, cache_key)
                        if cached is not None and not cached.stale:
                            return await self._serve_cached_h1(writer, request, cached)
                        return await self._proxy_attempts(writer, request, route, runtime, attempts, peer, cache_key)
            return await self._proxy_attempts(writer, request, route, runtime, attempts, peer, cache_key)
        except TimeoutError:
            last_status = 504
            last_message = "Gateway Timeout"
        except UpstreamUnavailable as exc:
            last_status = exc.status
            last_message = exc.message

        if stale_record is not None and route.proxy_cache_use_stale_on_error:
            return await self._serve_cached_h1(writer, request, stale_record)
        if route.proxy_fallback_path is not None and route.proxy_fallback_path.exists() and route.proxy_fallback_path.is_file():
            body = route.proxy_fallback_path.read_bytes()
            content_type = mimetypes.guess_type(route.proxy_fallback_path.name)[0] or "application/octet-stream"
            writer.write(
                render_headers(
                    200,
                    self._with_security_headers(
                        [
                            ("Content-Length", str(len(body))),
                            ("Content-Type", content_type),
                            ("Connection", "close"),
                        ]
                    ),
                )
            )
            if request.method != "HEAD":
                writer.write(body)
            await writer.drain()
            return ResponseOutcome(200, False, len(body))
        bytes_sent = await self._send_error(writer, last_status, last_message, keep_alive=False, request=request, peer=peer, route=route)
        return ResponseOutcome(last_status, False, bytes_sent)

    async def _proxy_attempts(
        self,
        writer: asyncio.StreamWriter,
        request: Request,
        route: RouteConfig,
        runtime: RouteRuntime,
        attempts: list[UpstreamState],
        peer: object,
        cache_key: str,
    ) -> ResponseOutcome:
        last_status = 502
        last_message = "Bad Gateway"
        for index, state in enumerate(attempts):
            try:
                outcome = await self._proxy_to_upstream(writer, request, route, runtime, state, peer, cache_key)
                if outcome.status >= 500:
                    self._record_upstream_failure(route, state)
                else:
                    state.record_success()
                return outcome
            except UpstreamUnavailable as exc:
                last_status = exc.status
                last_message = exc.message
                self._record_upstream_failure(route, state)
                if index < len(attempts) - 1:
                    self.metrics.proxy_retries_total += 1
        raise UpstreamUnavailable(last_status, last_message)

    async def _refresh_proxy_cache(
        self,
        request: Request,
        route: RouteConfig,
        runtime: RouteRuntime,
        peer: object,
        cache_key: str,
    ) -> None:
        attempts = runtime.select_attempts(remote_addr(peer), request.target)
        if not attempts:
            return
        state = attempts[0]
        sink = NullStreamWriter()
        with contextlib.suppress(Exception):
            await self._proxy_to_upstream(sink, request, route, runtime, state, peer, cache_key)

    def _record_upstream_failure(self, route: RouteConfig, state: UpstreamState) -> None:
        was_available = state.is_available()
        state.record_failure(route.circuit_failures, route.circuit_cooldown)
        self.metrics.upstream_failures_total += 1
        if was_available and not state.is_available():
            self.metrics.circuit_open_total += 1

    def _build_proxy_cache_key(self, route: RouteConfig, request: Request, peer: object, scheme: str) -> str:
        if self.native_core is not None and self.native_core.supports_cache_key:
            key = self.native_core.build_cache_key(
                route.proxy_cache_key,
                request.method,
                scheme,
                request_host(request),
                request.target,
                remote_addr(peer),
            )
            if key is not None:
                return key
        return build_proxy_cache_key(route, request, peer, scheme)

    async def _proxy_upgrade(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        request: Request,
        route: RouteConfig,
        state: UpstreamState,
        peer: object,
    ) -> ResponseOutcome:
        upstream = state.upstream
        upstream_target = build_upstream_target(route, request.target, upstream.base_path)
        timeout = self.config.proxy_timeout
        state.active_connections += 1
        bytes_sent = 0

        try:
            upstream_reader, upstream_writer = await connect_upstream(upstream, timeout)
            scheme = "https" if self.ssl_context else "http"
            upstream_writer.write(render_upstream_upgrade_request(request, upstream, upstream_target, peer, scheme))
            await upstream_writer.drain()
            response_head = await asyncio.wait_for(upstream_reader.readuntil(HEADER_END), timeout=timeout)
            status = parse_upstream_response_status(response_head)
            writer.write(response_head)
            await writer.drain()

            if status != 101:
                while True:
                    chunk = await asyncio.wait_for(upstream_reader.read(self.config.proxy_buffer_bytes), timeout=timeout)
                    if not chunk:
                        break
                    bytes_sent += len(chunk)
                    writer.write(chunk)
                    if writer.transport.get_write_buffer_size() >= self.config.proxy_buffer_bytes:
                        await writer.drain()
                await writer.drain()
                return ResponseOutcome(status, False, bytes_sent)

            upstream_to_client = asyncio.create_task(
                pipe_stream(upstream_reader, writer, self.config.proxy_buffer_bytes)
            )
            client_to_upstream = asyncio.create_task(
                pipe_stream(reader, upstream_writer, self.config.proxy_buffer_bytes)
            )
            done, pending = await asyncio.wait(
                {upstream_to_client, client_to_upstream},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            results = await asyncio.gather(*done, *pending, return_exceptions=True)
            bytes_sent = sum(result for result in results if isinstance(result, int))
            return ResponseOutcome(status, False, bytes_sent)
        except asyncio.TimeoutError as exc:
            raise UpstreamUnavailable(504, "Gateway Timeout") from exc
        except (OSError, asyncio.IncompleteReadError, HttpError) as exc:
            raise UpstreamUnavailable(502, "Bad Gateway") from exc
        finally:
            state.active_connections = max(0, state.active_connections - 1)
            with contextlib.suppress(UnboundLocalError):
                upstream_writer.close()
                with contextlib.suppress(ConnectionError, RuntimeError):
                    await upstream_writer.wait_closed()

    async def _proxy_to_upstream(
        self,
        writer: asyncio.StreamWriter,
        request: Request,
        route: RouteConfig,
        runtime: RouteRuntime,
        state: UpstreamState,
        peer: object,
        cache_key: str,
    ) -> ResponseOutcome:
        upstream = state.upstream
        upstream_target = build_upstream_target(route, request.target, upstream.base_path)
        timeout = self.config.proxy_timeout
        state.active_connections += 1

        try:
            upstream_reader, upstream_writer = await connect_upstream(upstream, timeout)
            scheme = "https" if self.ssl_context else "http"
            upstream_writer.write(render_upstream_request(request, upstream, upstream_target, peer, scheme))
            await upstream_writer.drain()
            response_head = await asyncio.wait_for(
                upstream_reader.readuntil(HEADER_END),
                timeout=timeout,
            )
            status, filtered_head = filter_upstream_response(response_head)
            _parsed_status, upstream_headers = parse_upstream_response_headers(response_head)
        except asyncio.TimeoutError as exc:
            state.active_connections = max(0, state.active_connections - 1)
            raise UpstreamUnavailable(504, "Gateway Timeout") from exc
        except (OSError, asyncio.IncompleteReadError, HttpError) as exc:
            state.active_connections = max(0, state.active_connections - 1)
            raise UpstreamUnavailable(502, "Bad Gateway") from exc

        should_cache = route.proxy_cache and request.method in route.proxy_cache_methods and status in {200, 203, 204, 301, 302, 404}
        if should_cache:
            try:
                cached_body = await read_upstream_response_body(
                    upstream_reader,
                    upstream_headers,
                    timeout,
                    self.config.proxy_buffer_bytes,
                )
                if len(cached_body) > route.proxy_cache_max_bytes:
                    should_cache = False
                writer.write(filtered_head)
                if request.method != "HEAD":
                    writer.write(cached_body)
                await writer.drain()
                if should_cache:
                    self._proxy_cache_put(runtime, route, cache_key, status, filtered_head, cached_body)
                return ResponseOutcome(status, False, len(cached_body))
            except asyncio.TimeoutError as exc:
                raise UpstreamUnavailable(504, "Gateway Timeout") from exc
            except (OSError, asyncio.IncompleteReadError, HttpError) as exc:
                raise UpstreamUnavailable(502, "Bad Gateway") from exc
            finally:
                upstream_writer.close()
                state.active_connections = max(0, state.active_connections - 1)
                with contextlib.suppress(ConnectionError, RuntimeError):
                    await upstream_writer.wait_closed()

        writer.write(filtered_head)
        bytes_sent = 0
        try:
            bytes_sent = await stream_upstream_response_body(
                upstream_reader,
                writer,
                upstream_headers,
                timeout,
                self.config.proxy_buffer_bytes,
            )
        except (asyncio.TimeoutError, OSError, asyncio.IncompleteReadError, HttpError):
            with contextlib.suppress(ConnectionError, RuntimeError):
                await writer.drain()
        finally:
            upstream_writer.close()
            state.active_connections = max(0, state.active_connections - 1)
            with contextlib.suppress(ConnectionError, RuntimeError):
                await upstream_writer.wait_closed()
        return ResponseOutcome(status, False, bytes_sent)

    async def _send_file(self, writer: asyncio.StreamWriter, path: Path) -> int:
        if self.config.file_io_backend == "read":
            return await self._send_file_read(writer, path)
        if self.config.file_io_backend == "threaded" or self.config.aio_threads > 0:
            return await self._send_file_threaded(writer, path)
        loop = asyncio.get_running_loop()
        sent = path.stat().st_size
        if not self.config.sendfile or self.config.file_io_backend not in {"auto", "sendfile"}:
            return await self._send_file_read(writer, path)
        with path.open("rb") as file:
            try:
                await loop.sendfile(writer.transport, file)
                return sent
            except (AttributeError, NotImplementedError, RuntimeError):
                return await self._send_file_read(writer, path)
        await writer.drain()
        return sent

    async def _send_file_read(self, writer: asyncio.StreamWriter, path: Path) -> int:
        sent = path.stat().st_size
        buffering = 0 if self.config.directio_min_bytes > 0 and sent >= self.config.directio_min_bytes else -1
        with path.open("rb", buffering=buffering) as file:
            while True:
                chunk = file.read(self.config.chunk_size)
                if not chunk:
                    break
                writer.write(chunk)
                if writer.transport.get_write_buffer_size() >= self.config.chunk_size:
                    await writer.drain()
        await writer.drain()
        return sent

    async def _send_file_threaded(self, writer: asyncio.StreamWriter, path: Path) -> int:
        sent = path.stat().st_size
        buffering = 0 if self.config.directio_min_bytes > 0 and sent >= self.config.directio_min_bytes else -1
        with path.open("rb", buffering=buffering) as file:
            while True:
                chunk = await asyncio.to_thread(file.read, self.config.chunk_size)
                if not chunk:
                    break
                writer.write(chunk)
                if writer.transport.get_write_buffer_size() >= self.config.chunk_size:
                    await writer.drain()
        await writer.drain()
        return sent

    async def _send_error(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        message: str,
        keep_alive: bool,
        extra_headers: list[tuple[str, str]] | None = None,
        request: Request | None = None,
        peer: object = None,
        route: RouteConfig | None = None,
        exception: BaseException | None = None,
        traceback_text: str | None = None,
    ) -> int:
        body = f"{status} {message}\n".encode("utf-8")
        content_type = "text/plain; charset=utf-8"
        error_page = self.error_pages.get(status)
        if error_page is not None and error_page.exists() and error_page.is_file():
            body = error_page.read_bytes()
            content_type = mimetypes.guess_type(error_page.name)[0] or "text/html; charset=utf-8"
        headers = [
            ("Content-Length", str(len(body))),
            ("Content-Type", content_type),
            ("Connection", "keep-alive" if keep_alive else "close"),
        ]
        if extra_headers:
            headers.extend(extra_headers)
        writer.write(render_headers(status, self._with_security_headers(headers)))
        writer.write(body)
        await writer.drain()
        if status >= 500 and self.config.error_log_path is not None:
            self.error_logger.write(
                json.dumps(
                    {
                        "ts": email.utils.formatdate(usegmt=True),
                        "status": status,
                        "message": message,
                    },
                    separators=(",", ":"),
                )
            )
        self._queue_ai_error_repair(status, message, request, peer, route, "HTTP/1.1", exception, traceback_text)
        return len(body)

    def _queue_ai_error_repair(
        self,
        status: int,
        message: str,
        request: Request | None,
        peer: object,
        route: RouteConfig | None,
        protocol: str,
        exception: BaseException | None = None,
        traceback_text: str | None = None,
    ) -> None:
        if not self.ai_error_repairer.should_handle(status):
            return
        headers = redact_headers(request.headers) if request is not None else {}
        event = ErrorRepairEvent(
            status=status,
            message=message,
            method=request.method if request is not None else "-",
            target=request.target if request is not None else "-",
            protocol=protocol,
            peer=remote_addr(peer),
            headers=headers,
            route_kind=route.kind if route is not None else "-",
            exception=f"{type(exception).__name__}: {exception}" if exception is not None else None,
            traceback=traceback_text,
            config_path=self.config.config_path,
        )
        task = asyncio.create_task(self.ai_error_repairer.handle(event))
        self.metrics.ai_error_repairs_total += 1
        self._ai_repair_tasks.add(task)

        def done(completed: asyncio.Task[object]) -> None:
            self._ai_repair_tasks.discard(completed)
            if completed.cancelled():
                return
            with contextlib.suppress(Exception):
                completed.result()

        task.add_done_callback(done)

    def _with_security_headers(self, headers: list[tuple[str, str]]) -> list[tuple[str, str]]:
        if not self.config.security_headers:
            return headers
        return [
            *headers,
            ("X-Content-Type-Options", "nosniff"),
            ("X-Frame-Options", "DENY"),
            ("Referrer-Policy", "no-referrer"),
        ]


def validate_runtime_config(config: ServerConfig) -> None:
    if config.http2:
        ensure_h2_available()
    if config.http3:
        ensure_aioquic_available()
        if config.tls_certfile is None or config.tls_keyfile is None:
            raise ValueError("HTTP/3 requires tls_certfile and tls_keyfile")
    if config.native_core != "python":
        status = load_native_core(config.native_core, config.native_core_path)
        if not status.available:
            raise RuntimeError(status.message)
    if config.log_format not in {"plain", "json"}:
        raise ValueError("log_format must be 'plain' or 'json'")
    if config.tls_client_verify not in {"off", "optional", "required"}:
        raise ValueError("tls_client_verify must be 'off', 'optional', or 'required'")
    if config.tls_client_verify != "off" and config.tls_client_ca_file is None:
        raise ValueError("tls_client_ca_file is required when tls_client_verify is enabled")
    if config.tls_ciphersuites and not hasattr(ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER), "set_ciphersuites"):
        raise RuntimeError("tls_ciphersuites is not supported by this Python/OpenSSL build")
    if config.file_io_backend not in {"auto", "sendfile", "threaded", "read"}:
        raise ValueError("file_io_backend must be 'auto', 'sendfile', 'threaded', or 'read'")
    if config.io_uring and os.name == "nt":
        raise ValueError("io_uring tuning is only meaningful on Linux")
    for route in config.routes:
        if route.load_balance not in {"round_robin", "first_available", "least_connections", "ip_hash", "hash"}:
            raise ValueError(f"unsupported load_balance: {route.load_balance}")


def normalize_routes(config: ServerConfig) -> tuple[RouteConfig, ...]:
    routes = config.routes
    if not routes:
        routes = (RouteConfig(path="/", kind="static", root=config.root or Path.cwd()),)

    normalized: list[RouteConfig] = []
    for route in routes:
        kind = route.kind.lower()
        if kind not in {"static", "proxy", "ai"}:
            raise ValueError(f"unsupported route kind: {route.kind}")
        path = route.normalized_path()
        if kind == "static" and route.root is None:
            raise ValueError(f"static route {path} requires root")
        if kind == "proxy" and not route.proxy_upstreams():
            raise ValueError(f"proxy route {path} requires upstream or upstreams")
        if kind == "ai" and route.ai_backend.lower() not in {"auto", "echo", "transformers", "llama_cpp"}:
            raise ValueError(f"unsupported AI backend: {route.ai_backend}")
        hosts = tuple(host.lower() for host in route.hosts)
        normalized.append(replace(route, kind=kind, path=path, hosts=hosts, retries=max(0, route.retries)))

    return tuple(sorted(normalized, key=lambda item: len(item.path), reverse=True))


def normalize_health_path(path: str) -> str:
    if not path.startswith("/"):
        path = f"/{path}"
    return path


def parse_request(raw: bytes) -> Request:
    try:
        head = raw[: -len(HEADER_END)].decode("iso-8859-1")
    except UnicodeDecodeError as exc:
        raise HttpError(400, "Bad Request") from exc

    lines = head.split("\r\n")
    if not lines or not lines[0]:
        raise HttpError(400, "Bad Request")

    parts = lines[0].split()
    if len(parts) != 3:
        raise HttpError(400, "Bad Request")

    method, target, version = parts
    if version not in {"HTTP/1.0", "HTTP/1.1"}:
        raise HttpError(400, "Bad Request")
    if len(target) > 8192:
        raise HttpError(414)

    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        if ":" not in line:
            raise HttpError(400, "Bad Request")
        name, value = line.split(":", 1)
        headers[name.strip().lower()] = value.strip()

    return Request(method=method.upper(), target=target, version=version, headers=headers)


def select_route(routes: tuple[RouteConfig, ...], target: str, host: str = "") -> RouteConfig | None:
    path = target_path(target)
    for route in routes:
        if route.hosts and host.lower() not in route.hosts:
            continue
        prefix = route.path
        if prefix == "/" or path == prefix.rstrip("/") or path.startswith(prefix):
            return route
    return None


def target_path(target: str) -> str:
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc:
        raise HttpError(400, "Bad Request")
    return parsed.path or "/"


def advanced_rewrite_matches(rule: AdvancedRewriteRule, request: Request) -> bool:
    if rule.methods and request.method.upper() not in rule.methods:
        return False
    host = request_host(request)
    if rule.hosts and not any(re.fullmatch(pattern, host) for pattern in rule.hosts):
        return False
    if rule.header is not None:
        name, pattern = rule.header
        if re.search(pattern, request.headers.get(name.lower(), "")) is None:
            return False
    if rule.query is not None and re.search(rule.query, urlsplit(request.target).query) is None:
        return False
    return re.search(rule.pattern, target_path(request.target)) is not None


def apply_advanced_rewrite(rule: AdvancedRewriteRule, request: Request) -> str:
    parsed = urlsplit(request.target)
    path = parsed.path or "/"
    match = re.search(rule.pattern, path)
    if match is None:
        return request.target
    replacement = expand_rewrite_template(rule.replacement, match, request)
    if "?" in replacement:
        return replacement
    if parsed.query:
        return f"{replacement}?{parsed.query}"
    return replacement


def expand_rewrite_template(template: str, match: re.Match[str], request: Request) -> str:
    result = template
    for index, value in enumerate(match.groups(), start=1):
        result = result.replace(f"${index}", value or "")
    for name, value in match.groupdict().items():
        result = result.replace(f"${{{name}}}", value or "")
        result = result.replace(f"${name}", value or "")
    parsed = urlsplit(request.target)
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    replacements = {
        "$method": request.method,
        "$host": request_host(request),
        "$uri": request.target,
        "$path": parsed.path or "/",
        "$query": parsed.query,
    }
    for name, value in request.headers.items():
        replacements[f"$header_{name.replace('-', '_')}"] = value
    for name, values in query.items():
        replacements[f"$arg_{name}"] = values[0] if values else ""
    for token, value in sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True):
        result = result.replace(token, value)
    return result


def resolve_static_target(root: Path, route: RouteConfig, target: str) -> Path:
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc:
        raise HttpError(400, "Bad Request")

    raw_path = parsed.path or "/"
    if "%00" in raw_path:
        raise HttpError(400, "Bad Request")

    prefix = route.path
    if prefix != "/":
        if raw_path == prefix.rstrip("/"):
            raw_path = "/"
        elif raw_path.startswith(prefix):
            raw_path = f"/{raw_path[len(prefix):]}"

    decoded = unquote(raw_path)
    if "\x00" in decoded:
        raise HttpError(400, "Bad Request")

    parts: list[str] = []
    for part in decoded.replace("\\", "/").split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            raise HttpError(403, "Forbidden")
        parts.append(part)

    candidate = root.joinpath(*parts).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise HttpError(403, "Forbidden") from exc
    return candidate


def route_local_target(route: RouteConfig, target: str) -> str:
    parsed = urlsplit(target)
    raw_path = parsed.path or "/"
    prefix = route.path
    if prefix != "/":
        if raw_path == prefix.rstrip("/"):
            raw_path = "/"
        elif raw_path.startswith(prefix):
            raw_path = f"/{raw_path[len(prefix):]}"
    if parsed.query:
        return f"{raw_path}?{parsed.query}"
    return raw_path


def resolve_target(root: Path, target: str) -> Path:
    return resolve_static_target(root, RouteConfig(path="/", kind="static", root=root), target)


def parse_upstream(value: str) -> Upstream:
    value = strip_upstream_weight(value)
    if value.startswith("http://unix:"):
        socket_path = Path(value[len("http://unix:") :])
        return Upstream("localhost", 80, "/", "localhost", socket_path)
    if value.startswith("unix:"):
        socket_path = Path(value[len("unix:") :])
        return Upstream("localhost", 80, "/", "localhost", socket_path)
    if value.startswith("http+unix://"):
        socket_path = Path(urllib.parse.unquote(value[len("http+unix://") :]))
        return Upstream("localhost", 80, "/", "localhost", socket_path)
    parsed = urlsplit(value)
    if parsed.scheme != "http":
        raise HttpError(500, "Only http:// and unix socket upstreams are supported")
    if parsed.hostname is None:
        raise HttpError(500, "Invalid upstream")
    port = parsed.port or 80
    authority = parsed.hostname if port == 80 else f"{parsed.hostname}:{port}"
    base_path = parsed.path or "/"
    return Upstream(parsed.hostname, port, base_path, authority)


async def connect_upstream(upstream: Upstream, timeout: float) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    if upstream.unix_socket is not None:
        opener = getattr(asyncio, "open_unix_connection", None)
        if opener is None:
            raise OSError("Unix socket upstreams are not supported on this platform")
        return await asyncio.wait_for(opener(str(upstream.unix_socket)), timeout=timeout)
    return await asyncio.wait_for(asyncio.open_connection(upstream.host, upstream.port), timeout=timeout)


def strip_upstream_weight(value: str) -> str:
    return value.split()[0]


def parse_upstream_weight(value: str) -> int:
    for part in value.split()[1:]:
        if part.startswith("weight="):
            with contextlib.suppress(ValueError):
                return max(1, int(part.split("=", 1)[1]))
    return 1


def build_upstream_target(route: RouteConfig, target: str, upstream_base_path: str) -> str:
    parsed = urlsplit(target)
    path = parsed.path or "/"

    if route.strip_prefix and route.path != "/":
        if path == route.path.rstrip("/"):
            path = "/"
        elif path.startswith(route.path):
            path = f"/{path[len(route.path):]}"

    base = upstream_base_path.rstrip("/")
    if base:
        path = f"{base}{path if path.startswith('/') else f'/{path}'}"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return path


def build_proxy_cache_key(route: RouteConfig, request: Request, peer: object, scheme: str) -> str:
    parsed = urlsplit(request.target)
    replacements = {
        "$protocol": scheme,
        "$method": request.method,
        "$scheme": scheme,
        "$host": request_host(request),
        "$uri": request.target,
        "$path": parsed.path or "/",
        "$query": parsed.query,
        "$remote_addr": remote_addr(peer),
    }
    key = route.proxy_cache_key
    for token, value in sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True):
        key = key.replace(token, value)
    return key


def json_http_response(status: int, header_filter: object, body: bytes) -> HttpResponse:
    headers = [
        ("Content-Length", str(len(body))),
        ("Content-Type", "application/json"),
        ("Cache-Control", "no-store"),
    ]
    return HttpResponse(status, header_filter(headers), body)  # type: ignore[operator]


def fetch_json_url(url: str, timeout: float) -> dict[str, object]:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("JWKS URL must use http or https")
    request = urllib.request.Request(url, headers={"User-Agent": SERVER_NAME})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        status = getattr(response, "status", 200)
        if status < 200 or status >= 300:
            raise ValueError(f"JWKS fetch failed with status {status}")
        return json.loads(response.read(1024 * 1024).decode("utf-8"))


def render_upstream_request(
    request: Request,
    upstream: Upstream,
    upstream_target: str,
    peer: object,
    scheme: str,
) -> bytes:
    lines = [
        f"{request.method} {upstream_target} HTTP/1.1",
        f"Host: {upstream.authority}",
        "Connection: close",
    ]

    for name, value in request.headers.items():
        lower_name = name.lower()
        if lower_name in HOP_BY_HOP_HEADERS or lower_name in {"host", "content-length"}:
            continue
        lines.append(f"{title_header(name)}: {value}")

    client_ip = remote_addr(peer)
    if client_ip:
        forwarded_for = request.headers.get("x-forwarded-for")
        value = f"{forwarded_for}, {client_ip}" if forwarded_for else client_ip
        lines.append(f"X-Forwarded-For: {value}")
    lines.append(f"X-Forwarded-Proto: {scheme}")
    lines.append(f"Content-Length: {len(request.body)}")

    return ("\r\n".join(lines) + "\r\n\r\n").encode("iso-8859-1") + request.body


def render_upstream_upgrade_request(
    request: Request,
    upstream: Upstream,
    upstream_target: str,
    peer: object,
    scheme: str,
) -> bytes:
    lines = [
        f"{request.method} {upstream_target} HTTP/1.1",
        f"Host: {upstream.authority}",
        "Connection: Upgrade",
        f"Upgrade: {request.headers.get('upgrade', '')}",
    ]

    for name, value in request.headers.items():
        lower_name = name.lower()
        if lower_name in {
            "host",
            "content-length",
            "connection",
            "upgrade",
            "keep-alive",
            "proxy-authenticate",
            "proxy-authorization",
            "te",
            "trailer",
            "transfer-encoding",
        }:
            continue
        lines.append(f"{title_header(name)}: {value}")

    client_ip = remote_addr(peer)
    if client_ip:
        forwarded_for = request.headers.get("x-forwarded-for")
        value = f"{forwarded_for}, {client_ip}" if forwarded_for else client_ip
        lines.append(f"X-Forwarded-For: {value}")
    lines.append(f"X-Forwarded-Proto: {scheme}")
    if request.body:
        lines.append(f"Content-Length: {len(request.body)}")

    return ("\r\n".join(lines) + "\r\n\r\n").encode("iso-8859-1") + request.body


def parse_upstream_response_status(response_head: bytes) -> int:
    text = response_head[: -len(HEADER_END)].decode("iso-8859-1", errors="replace")
    first_line = text.split("\r\n", 1)[0]
    if not first_line.startswith("HTTP/"):
        raise HttpError(502, "Bad Gateway")
    status_parts = first_line.split(maxsplit=2)
    try:
        return int(status_parts[1])
    except (IndexError, ValueError) as exc:
        raise HttpError(502, "Bad Gateway") from exc


def filter_upstream_response(response_head: bytes) -> tuple[int, bytes]:
    text = response_head[: -len(HEADER_END)].decode("iso-8859-1", errors="replace")
    lines = text.split("\r\n")
    if not lines or not lines[0].startswith("HTTP/"):
        raise HttpError(502, "Bad Gateway")

    status_parts = lines[0].split(maxsplit=2)
    try:
        status = int(status_parts[1])
    except (IndexError, ValueError) as exc:
        raise HttpError(502, "Bad Gateway") from exc

    filtered = [lines[0]]
    has_date = False
    has_server = False
    for line in lines[1:]:
        if not line or ":" not in line:
            continue
        name, _value = line.split(":", 1)
        lower_name = name.strip().lower()
        if lower_name in HOP_BY_HOP_HEADERS - {"transfer-encoding", "trailer"}:
            continue
        if lower_name == "date":
            has_date = True
        if lower_name == "server":
            has_server = True
        filtered.append(line)

    if not has_server:
        filtered.append(f"Server: {SERVER_NAME}")
    if not has_date:
        filtered.append(f"Date: {email.utils.formatdate(usegmt=True)}")
    filtered.append("Connection: close")
    return status, ("\r\n".join(filtered) + "\r\n\r\n").encode("iso-8859-1")


def header_map(headers: list[tuple[str, str]]) -> dict[str, str]:
    return {name.lower(): value for name, value in headers}


def is_chunked_response(headers: list[tuple[str, str]]) -> bool:
    transfer_encoding = header_map(headers).get("transfer-encoding", "")
    tokens = {token.strip().lower() for token in transfer_encoding.split(",")}
    return "chunked" in tokens


def upstream_content_length(headers: list[tuple[str, str]]) -> int | None:
    value = header_map(headers).get("content-length")
    if value is None:
        return None
    try:
        length = int(value)
    except ValueError as exc:
        raise HttpError(502, "Bad Gateway") from exc
    if length < 0:
        raise HttpError(502, "Bad Gateway")
    return length


async def read_upstream_response_body(
    reader: asyncio.StreamReader,
    headers: list[tuple[str, str]],
    timeout: float,
    buffer_bytes: int,
    *,
    decode_chunked: bool = False,
) -> bytes:
    if is_chunked_response(headers):
        return await read_chunked_upstream_body(reader, timeout, decode_chunked)
    length = upstream_content_length(headers)
    if length is not None:
        return await read_exact_upstream_body(reader, length, timeout, buffer_bytes)
    return await read_until_close_upstream_body(reader, timeout, buffer_bytes)


async def stream_upstream_response_body(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    headers: list[tuple[str, str]],
    timeout: float,
    buffer_bytes: int,
) -> int:
    if is_chunked_response(headers):
        return await stream_chunked_upstream_body(reader, writer, timeout)
    length = upstream_content_length(headers)
    if length is not None:
        return await stream_exact_upstream_body(reader, writer, length, timeout, buffer_bytes)
    return await pipe_stream_with_timeout(reader, writer, timeout, buffer_bytes)


async def read_exact_upstream_body(
    reader: asyncio.StreamReader,
    length: int,
    timeout: float,
    buffer_bytes: int,
) -> bytes:
    body = bytearray()
    remaining = length
    while remaining > 0:
        chunk = await asyncio.wait_for(reader.readexactly(min(buffer_bytes, remaining)), timeout=timeout)
        body.extend(chunk)
        remaining -= len(chunk)
    return bytes(body)


async def stream_exact_upstream_body(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    length: int,
    timeout: float,
    buffer_bytes: int,
) -> int:
    remaining = length
    sent = 0
    while remaining > 0:
        chunk = await asyncio.wait_for(reader.readexactly(min(buffer_bytes, remaining)), timeout=timeout)
        sent += len(chunk)
        remaining -= len(chunk)
        writer.write(chunk)
        if writer.transport.get_write_buffer_size() >= buffer_bytes:
            await writer.drain()
    await writer.drain()
    return sent


async def read_until_close_upstream_body(
    reader: asyncio.StreamReader,
    timeout: float,
    buffer_bytes: int,
) -> bytes:
    body = bytearray()
    while True:
        chunk = await asyncio.wait_for(reader.read(buffer_bytes), timeout=timeout)
        if not chunk:
            break
        body.extend(chunk)
    return bytes(body)


async def pipe_stream_with_timeout(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    timeout: float,
    buffer_bytes: int,
) -> int:
    bytes_sent = 0
    while True:
        chunk = await asyncio.wait_for(reader.read(buffer_bytes), timeout=timeout)
        if not chunk:
            break
        bytes_sent += len(chunk)
        writer.write(chunk)
        if writer.transport.get_write_buffer_size() >= buffer_bytes:
            await writer.drain()
    await writer.drain()
    return bytes_sent


async def read_chunked_upstream_body(
    reader: asyncio.StreamReader,
    timeout: float,
    decode: bool,
) -> bytes:
    body = bytearray()
    while True:
        line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        if not line:
            raise HttpError(502, "Bad Gateway")
        size = parse_chunk_size(line)
        if not decode:
            body.extend(line)
        if size:
            data = await asyncio.wait_for(reader.readexactly(size), timeout=timeout)
            ending = await asyncio.wait_for(reader.readexactly(2), timeout=timeout)
            if ending != b"\r\n":
                raise HttpError(502, "Bad Gateway")
            body.extend(data)
            if not decode:
                body.extend(ending)
            continue
        await consume_or_copy_trailers(reader, timeout, body if not decode else None)
        break
    return bytes(body)


async def stream_chunked_upstream_body(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    timeout: float,
) -> int:
    sent = 0
    while True:
        line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        if not line:
            raise HttpError(502, "Bad Gateway")
        size = parse_chunk_size(line)
        writer.write(line)
        sent += len(line)
        if size:
            data = await asyncio.wait_for(reader.readexactly(size + 2), timeout=timeout)
            writer.write(data)
            sent += len(data)
            await writer.drain()
            continue
        sent += await consume_or_copy_trailers(reader, timeout, writer=writer)
        await writer.drain()
        return sent


async def consume_or_copy_trailers(
    reader: asyncio.StreamReader,
    timeout: float,
    body: bytearray | None = None,
    writer: asyncio.StreamWriter | None = None,
) -> int:
    copied = 0
    while True:
        line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        if not line:
            raise HttpError(502, "Bad Gateway")
        if body is not None:
            body.extend(line)
        if writer is not None:
            writer.write(line)
        copied += len(line)
        if line in {b"\r\n", b"\n"}:
            return copied


def parse_chunk_size(line: bytes) -> int:
    try:
        text = line.decode("ascii", errors="strict").strip()
    except UnicodeError as exc:
        raise HttpError(502, "Bad Gateway") from exc
    size_text = text.split(";", 1)[0]
    try:
        return int(size_text, 16)
    except ValueError as exc:
        raise HttpError(502, "Bad Gateway") from exc


def is_upgrade_request(request: Request) -> bool:
    connection_tokens = {
        token.strip().lower()
        for token in request.headers.get("connection", "").split(",")
    }
    return "upgrade" in connection_tokens and bool(request.headers.get("upgrade"))


async def pipe_stream(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    buffer_bytes: int,
) -> int:
    bytes_sent = 0
    while True:
        chunk = await reader.read(buffer_bytes)
        if not chunk:
            break
        bytes_sent += len(chunk)
        writer.write(chunk)
        if writer.transport.get_write_buffer_size() >= buffer_bytes:
            await writer.drain()
    await writer.drain()
    return bytes_sent


def should_gzip(
    request: Request,
    content_type: str,
    size: int,
    config: ServerConfig,
) -> bool:
    if not config.gzip or size < config.gzip_min_bytes:
        return False
    if "gzip" not in request.headers.get("accept-encoding", "").lower():
        return False
    return content_type.startswith("text/") or content_type in {
        "application/javascript",
        "application/json",
        "application/xml",
        "image/svg+xml",
    }


def select_precompressed(path: Path, request: Request) -> tuple[Path, str] | None:
    encodings = [item.strip() for item in request.headers.get("accept-encoding", "").lower().split(",")]
    if "br" in encodings:
        brotli_path = path.with_name(f"{path.name}.br")
        if brotli_path.exists() and brotli_path.is_file():
            return brotli_path, "br"
    if "gzip" in encodings:
        gzip_path = path.with_name(f"{path.name}.gz")
        if gzip_path.exists() and gzip_path.is_file():
            return gzip_path, "gzip"
    return None


def request_host(request: Request) -> str:
    return request.headers.get("host", "").split(":", 1)[0].lower()


def is_authorized(request: Request, route: RouteConfig) -> bool:
    if route.jwt_hs256_secret is not None:
        authorization = request.headers.get("authorization", "")
        if not authorization.lower().startswith("bearer "):
            return False
        token = authorization.split(None, 1)[1]
        claims = verify_hs256_jwt(token, route.jwt_hs256_secret)
        if not claims.valid:
            return False
        if not claims_match(claims.payload, route.jwt_issuer, route.jwt_audience, route.jwt_required_claims):
            return False
    if not route.basic_auth:
        return True
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        return False
    try:
        raw = base64.b64decode(header.split(None, 1)[1]).decode("utf-8")
    except Exception:
        return False
    if ":" not in raw:
        return False
    username, password = raw.split(":", 1)
    return any(username == allowed_user and password == allowed_password for allowed_user, allowed_password in route.basic_auth)


def create_ssl_context(config: ServerConfig) -> ssl.SSLContext | None:
    if config.tls_certfile is None and config.tls_keyfile is None:
        return None
    if config.tls_certfile is None or config.tls_keyfile is None:
        raise ValueError("tls_certfile and tls_keyfile must be set together")

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    if hasattr(ssl, "TLSVersion"):
        versions = {
            "TLSv1.2": ssl.TLSVersion.TLSv1_2,
            "TLSv1.3": ssl.TLSVersion.TLSv1_3,
        }
        context.minimum_version = versions.get(config.tls_min_version, ssl.TLSVersion.TLSv1_2)
    if config.tls_ciphers:
        context.set_ciphers(config.tls_ciphers)
    if config.tls_ciphersuites and hasattr(context, "set_ciphersuites"):
        context.set_ciphersuites(config.tls_ciphersuites)
    if config.tls_ecdh_curve:
        context.set_ecdh_curve(config.tls_ecdh_curve)
    if config.tls_keylog_file is not None and hasattr(context, "keylog_filename"):
        context.keylog_filename = str(config.tls_keylog_file)
    if config.tls_client_ca_file is not None:
        context.load_verify_locations(cafile=str(config.tls_client_ca_file))
    if config.tls_client_verify == "required":
        context.verify_mode = ssl.CERT_REQUIRED
    elif config.tls_client_verify == "optional":
        context.verify_mode = ssl.CERT_OPTIONAL
    if not config.tls_session_tickets and hasattr(ssl, "OP_NO_TICKET"):
        context.options |= ssl.OP_NO_TICKET
    if config.tls_ocsp_response_file is not None:
        if hasattr(context, "ocsp_response"):
            context.ocsp_response = config.tls_ocsp_response_file.read_bytes()
        elif config.tls_ocsp_required:
            raise RuntimeError("OCSP stapling is required but this Python/OpenSSL build does not expose ocsp_response")
    alpn = list(config.tls_alpn_protocols) if config.tls_alpn_protocols else (["h2", "http/1.1"] if config.http2 else ["http/1.1"])
    context.set_alpn_protocols(alpn)
    context.load_cert_chain(certfile=str(config.tls_certfile), keyfile=str(config.tls_keyfile))
    if config.tls_sni:
        contexts = {}
        for hostname, certfile, keyfile in config.tls_sni:
            sni_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            sni_context.minimum_version = context.minimum_version
            if config.tls_ciphers:
                sni_context.set_ciphers(config.tls_ciphers)
            if config.tls_ciphersuites and hasattr(sni_context, "set_ciphersuites"):
                sni_context.set_ciphersuites(config.tls_ciphersuites)
            if config.tls_ecdh_curve:
                sni_context.set_ecdh_curve(config.tls_ecdh_curve)
            if config.tls_keylog_file is not None and hasattr(sni_context, "keylog_filename"):
                sni_context.keylog_filename = str(config.tls_keylog_file)
            if config.tls_client_ca_file is not None:
                sni_context.load_verify_locations(cafile=str(config.tls_client_ca_file))
            if config.tls_client_verify == "required":
                sni_context.verify_mode = ssl.CERT_REQUIRED
            elif config.tls_client_verify == "optional":
                sni_context.verify_mode = ssl.CERT_OPTIONAL
            if not config.tls_session_tickets and hasattr(ssl, "OP_NO_TICKET"):
                sni_context.options |= ssl.OP_NO_TICKET
            if config.tls_ocsp_response_file is not None and hasattr(sni_context, "ocsp_response"):
                sni_context.ocsp_response = config.tls_ocsp_response_file.read_bytes()
            sni_context.set_alpn_protocols(alpn)
            sni_context.load_cert_chain(certfile=str(certfile), keyfile=str(keyfile))
            contexts[hostname.lower()] = sni_context

        def sni_callback(sock: ssl.SSLSocket, server_name: str | None, _context: ssl.SSLContext) -> None:
            if server_name and server_name.lower() in contexts:
                sock.context = contexts[server_name.lower()]

        context.set_servername_callback(sni_callback)
    return context


def tls_stamp(config: ServerConfig) -> tuple[tuple[int, int], tuple[int, int]] | None:
    if config.tls_certfile is None or config.tls_keyfile is None:
        return None
    cert = config.tls_certfile.stat()
    key = config.tls_keyfile.stat()
    return ((cert.st_mtime_ns, cert.st_size), (key.st_mtime_ns, key.st_size))


def stable_index(key: str, items: list[UpstreamState]) -> int:
    if not items:
        return 0
    digest = hashlib.sha256(key.encode("utf-8", errors="ignore")).digest()
    return int.from_bytes(digest[:8], "big") % len(items)


def rotate(items: list[UpstreamState], start: int) -> list[UpstreamState]:
    if not items:
        return []
    start = start % len(items)
    return items[start:] + items[:start]


def is_h2_available() -> bool:
    return importlib.util.find_spec("h2") is not None


def is_aioquic_available() -> bool:
    return importlib.util.find_spec("aioquic") is not None


def ensure_h2_available() -> None:
    if not is_h2_available():
        raise RuntimeError("HTTP/2 support requires the 'h2' package. Install veloxserver dependencies first.")


def ensure_aioquic_available() -> None:
    if not is_aioquic_available():
        raise RuntimeError("HTTP/3 support requires the 'aioquic' package. Install veloxserver dependencies first.")


def load_h2() -> tuple[object, object, object]:
    ensure_h2_available()
    connection_module = importlib.import_module("h2.connection")
    config_module = importlib.import_module("h2.config")
    events_module = importlib.import_module("h2.events")
    return connection_module.H2Connection, config_module.H2Configuration, events_module


def selected_alpn_protocol(writer: asyncio.StreamWriter) -> str | None:
    ssl_object = writer.get_extra_info("ssl_object")
    if ssl_object is None:
        return None
    return ssl_object.selected_alpn_protocol()


def h2_request_from_state(state: dict[str, object]) -> Request:
    raw_headers = state.get("headers", [])
    headers: dict[str, str] = {}
    pseudo: dict[str, str] = {}
    for name, value in raw_headers:
        name = str(name).lower()
        value = str(value)
        if name.startswith(":"):
            pseudo[name] = value
        else:
            headers[name] = value
    if ":authority" in pseudo and "host" not in headers:
        headers["host"] = pseudo[":authority"]
    method = pseudo.get(":method", "GET").upper()
    target = pseudo.get(":path", "/")
    body_obj = state.get("body", bytearray())
    body = bytes(body_obj) if isinstance(body_obj, bytearray) else b""
    return Request(method=method, target=target, version="HTTP/2", headers=headers, body=body)


def h2_response_headers(status: int, headers: list[tuple[str, str]]) -> list[tuple[str, str]]:
    result = [(":status", str(status))]
    seen = set()
    base_headers = [
        ("server", SERVER_NAME),
        ("date", email.utils.formatdate(usegmt=True)),
        *headers,
    ]
    for name, value in base_headers:
        lower = name.lower()
        if lower in HOP_BY_HOP_HEADERS or lower in {"connection", "upgrade", "keep-alive"}:
            continue
        if lower.startswith(":"):
            continue
        if lower in seen and lower in {"server", "date", "content-length", "content-type"}:
            continue
        seen.add(lower)
        result.append((lower, str(value)))
    return result


def parse_upstream_response_headers(response_head: bytes) -> tuple[int, list[tuple[str, str]]]:
    text = response_head[: -len(HEADER_END)].decode("iso-8859-1", errors="replace")
    lines = text.split("\r\n")
    if not lines or not lines[0].startswith("HTTP/"):
        raise HttpError(502, "Bad Gateway")
    parts = lines[0].split(maxsplit=2)
    try:
        status = int(parts[1])
    except (IndexError, ValueError) as exc:
        raise HttpError(502, "Bad Gateway") from exc
    headers = []
    for line in lines[1:]:
        if not line or ":" not in line:
            continue
        name, value = line.split(":", 1)
        headers.append((name.strip(), value.strip()))
    return status, headers


def filter_h2_upstream_headers(headers: list[tuple[str, str]]) -> list[tuple[str, str]]:
    filtered = []
    has_server = False
    has_date = False
    for name, value in headers:
        lower = name.lower()
        if lower in HOP_BY_HOP_HEADERS:
            continue
        if lower == "server":
            has_server = True
        if lower == "date":
            has_date = True
        filtered.append((lower, value))
    if not has_server:
        filtered.append(("server", SERVER_NAME))
    if not has_date:
        filtered.append(("date", email.utils.formatdate(usegmt=True)))
    return filtered


def encode_cached_h2_headers(headers: list[tuple[str, str]]) -> bytes:
    return json.dumps(headers, separators=(",", ":")).encode("utf-8")


def decode_cached_h2_headers(data: bytes) -> list[tuple[str, str]]:
    return [(str(name), str(value)) for name, value in json.loads(data.decode("utf-8"))]


def make_etag(stat_result: os.stat_result) -> str:
    return f'W/"{stat_result.st_mtime_ns:x}-{stat_result.st_size:x}"'


def is_not_modified(header_value: str | None, mtime: float) -> bool:
    if not header_value:
        return False
    try:
        parsed = email.utils.parsedate_to_datetime(header_value)
    except (TypeError, ValueError):
        return False
    return parsed.timestamp() >= int(mtime)


def title_header(name: str) -> str:
    return "-".join(part.capitalize() for part in name.split("-"))


def remote_addr(peer: object) -> str:
    if isinstance(peer, tuple) and peer:
        return str(peer[0])
    return ""


def render_headers(status: int, headers: list[tuple[str, str]]) -> bytes:
    reason = STATUS_TEXT.get(status, "OK")
    base = [
        f"HTTP/1.1 {status} {reason}",
        f"Server: {SERVER_NAME}",
        f"Date: {email.utils.formatdate(usegmt=True)}",
    ]
    base.extend(f"{name}: {value}" for name, value in headers)
    return ("\r\n".join(base) + "\r\n\r\n").encode("iso-8859-1")
