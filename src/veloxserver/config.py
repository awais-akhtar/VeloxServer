from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from .server import AdvancedRewriteRule, RouteConfig, ServerConfig
from .stream import StreamProxyConfig


def load_config(path: Path) -> ServerConfig:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    base_dir = path.resolve().parent
    server = data.get("server", {})
    if not isinstance(server, dict):
        raise ValueError("[server] must be a table")
    admin = data.get("admin", {})
    if not isinstance(admin, dict):
        raise ValueError("[admin] must be a table")
    ai_error_repair = data.get("ai_error_repair", {})
    if not isinstance(ai_error_repair, dict):
        raise ValueError("[ai_error_repair] must be a table")

    routes = tuple(load_routes(data.get("routes", []), base_dir))
    root = optional_path(server.get("root"), base_dir)

    return ServerConfig(
        admin_enabled=bool(admin.get("enabled", False)),
        admin_path=str(admin.get("path", "/__veloxserver")),
        admin_reload_path=str(admin.get("reload_path", "/reload")),
        admin_status_path=str(admin.get("status_path", "/status")),
        config_path=path.resolve(),
        root=root,
        host=str(server.get("host", "127.0.0.1")),
        port=int(server.get("port", 8080)),
        max_header_bytes=int(server.get("max_header_bytes", 16 * 1024)),
        max_body_bytes=int(server.get("max_body_bytes", 10 * 1024 * 1024)),
        chunk_size=int(server.get("chunk_size", 256 * 1024)),
        proxy_buffer_bytes=int(server.get("proxy_buffer_bytes", 64 * 1024)),
        file_io_backend=str(server.get("file_io_backend", "auto")),
        sendfile=bool(server.get("sendfile", True)),
        aio_threads=int(server.get("aio_threads", 0)),
        directio_min_bytes=int(server.get("directio_min_bytes", 0)),
        io_uring=bool(server.get("io_uring", False)),
        workers=int(server.get("workers", 1)),
        reuse_port=bool(server.get("reuse_port", False)),
        upgrade_command=optional_string(server.get("upgrade_command")),
        upgrade_grace_seconds=float(server.get("upgrade_grace_seconds", 2.0)),
        upgrade_ready_timeout=float(server.get("upgrade_ready_timeout", 10.0)),
        upgrade_state_path=optional_path(server.get("upgrade_state_path"), base_dir),
        open_file_cache_entries=int(server.get("open_file_cache_entries", 0)),
        open_file_cache_ttl=float(server.get("open_file_cache_ttl", 30.0)),
        open_file_cache_max_bytes=int(server.get("open_file_cache_max_bytes", 1024 * 1024)),
        open_file_cache_errors=bool(server.get("open_file_cache_errors", False)),
        open_file_cache_min_uses=int(server.get("open_file_cache_min_uses", 1)),
        open_file_cache_inactive=float(server.get("open_file_cache_inactive", 60.0)),
        open_file_cache_metadata=bool(server.get("open_file_cache_metadata", True)),
        shared_zone_path=optional_path(server.get("shared_zone_path"), base_dir),
        plugin_paths=tuple(
            path for path in (optional_path(value, base_dir) for value in optional_strings(server.get("plugin_paths"))) if path is not None
        ),
        access_log=bool(server.get("access_log", False)),
        log_format=str(server.get("log_format", "plain")),
        access_log_path=optional_path(server.get("access_log_path"), base_dir),
        error_log_path=optional_path(server.get("error_log_path"), base_dir),
        log_rotate_bytes=int(server.get("log_rotate_bytes", 0)),
        gzip=bool(server.get("gzip", False)),
        gzip_min_bytes=int(server.get("gzip_min_bytes", 1024)),
        security_headers=bool(server.get("security_headers", True)),
        health_path=str(server.get("health_path", "/healthz")),
        metrics_path=str(server.get("metrics_path", "/metrics")),
        rewrite_rules=tuple(load_pairs(server.get("rewrite_rules", []), "pattern", "replacement")),
        advanced_rewrite_rules=tuple(load_advanced_rewrites(server.get("advanced_rewrite_rules", []))),
        waf_block_path_patterns=tuple(optional_strings(server.get("waf_block_path_patterns"))),
        error_pages=tuple(load_error_pages(server.get("error_pages", {}), base_dir)),
        proxy_timeout=float(server.get("proxy_timeout", 30.0)),
        rate_limit_per_minute=int(server.get("rate_limit_per_minute", 0)),
        rate_limit_burst=int(server.get("rate_limit_burst", 0)),
        connection_limit=int(server.get("connection_limit", 0)),
        connection_limit_per_client=int(server.get("connection_limit_per_client", 0)),
        tls_certfile=optional_path(server.get("tls_certfile"), base_dir),
        tls_keyfile=optional_path(server.get("tls_keyfile"), base_dir),
        tls_ciphers=optional_string(server.get("tls_ciphers")),
        tls_ciphersuites=optional_string(server.get("tls_ciphersuites")),
        tls_min_version=str(server.get("tls_min_version", "TLSv1.2")),
        tls_session_tickets=bool(server.get("tls_session_tickets", True)),
        tls_client_verify=str(server.get("tls_client_verify", "off")),
        tls_client_ca_file=optional_path(server.get("tls_client_ca_file"), base_dir),
        tls_ecdh_curve=optional_string(server.get("tls_ecdh_curve")),
        tls_keylog_file=optional_path(server.get("tls_keylog_file"), base_dir),
        tls_alpn_protocols=tuple(optional_strings(server.get("tls_alpn_protocols"))),
        tls_ocsp_required=bool(server.get("tls_ocsp_required", False)),
        tls_ocsp_response_file=optional_path(server.get("tls_ocsp_response_file"), base_dir),
        tls_sni=tuple(load_tls_sni(server.get("tls_sni", []), base_dir)),
        tls_reload_interval=float(server.get("tls_reload_interval", 5.0)),
        graceful_shutdown_timeout=float(server.get("graceful_shutdown_timeout", 10.0)),
        http2=bool(server.get("http2", False)),
        http3=bool(server.get("http3", False)),
        http3_port=optional_int(server.get("http3_port")),
        native_core=str(server.get("native_core", "python")),
        native_core_path=optional_path(server.get("native_core_path"), base_dir),
        ai_error_repair_enabled=bool(ai_error_repair.get("enabled", server.get("ai_error_repair_enabled", False))),
        ai_error_repair_project_path=optional_path(ai_error_repair.get("project_path", server.get("ai_error_repair_project_path")), base_dir),
        ai_error_repair_log_path=optional_path(ai_error_repair.get("log_path", server.get("ai_error_repair_log_path")), base_dir),
        ai_error_repair_suggestions_path=optional_path(ai_error_repair.get("suggestions_path", server.get("ai_error_repair_suggestions_path")), base_dir),
        ai_error_repair_apply=bool(ai_error_repair.get("apply", server.get("ai_error_repair_apply", False))),
        ai_error_repair_model=str(ai_error_repair.get("model", server.get("ai_error_repair_model", "gpt-4.1-mini"))),
        ai_error_repair_api_key_env=str(ai_error_repair.get("api_key_env", server.get("ai_error_repair_api_key_env", "OPENAI_API_KEY"))),
        ai_error_repair_base_url=str(ai_error_repair.get("base_url", server.get("ai_error_repair_base_url", "https://api.openai.com/v1"))),
        ai_error_repair_timeout=float(ai_error_repair.get("timeout", server.get("ai_error_repair_timeout", 30.0))),
        ai_error_repair_min_status=int(ai_error_repair.get("min_status", server.get("ai_error_repair_min_status", 500))),
        ai_error_repair_statuses=tuple(int(value) for value in optional_strings(ai_error_repair.get("statuses", server.get("ai_error_repair_statuses")))),
        ai_error_repair_context_files=tuple(
            path for path in (optional_path(value, base_dir) for value in optional_strings(ai_error_repair.get("context_files", server.get("ai_error_repair_context_files")))) if path is not None
        ),
        ai_error_repair_max_file_bytes=int(ai_error_repair.get("max_file_bytes", server.get("ai_error_repair_max_file_bytes", 32 * 1024))),
        ai_error_repair_max_context_bytes=int(ai_error_repair.get("max_context_bytes", server.get("ai_error_repair_max_context_bytes", 96 * 1024))),
        ai_error_repair_cooldown_seconds=float(ai_error_repair.get("cooldown_seconds", server.get("ai_error_repair_cooldown_seconds", 60.0))),
        ai_error_repair_max_output_tokens=int(ai_error_repair.get("max_output_tokens", server.get("ai_error_repair_max_output_tokens", 1600))),
        stream_proxies=tuple(load_stream_proxies(data.get("streams", []))),
        routes=routes,
    )


def load_routes(raw_routes: Any, base_dir: Path) -> list[RouteConfig]:
    if not isinstance(raw_routes, list):
        raise ValueError("[[routes]] must be an array of tables")

    routes: list[RouteConfig] = []
    for item in raw_routes:
        if not isinstance(item, dict):
            raise ValueError("each [[routes]] entry must be a table")

        kind = str(item.get("kind", item.get("type", "static")))
        routes.append(
            RouteConfig(
                path=str(item.get("path", "/")),
                kind=kind,
                hosts=tuple(optional_strings(item.get("hosts"))),
                root=optional_path(item.get("root"), base_dir),
                upstream=optional_string(item.get("upstream")),
                upstreams=tuple(optional_strings(item.get("upstreams"))),
                upstream_weights=tuple(int(value) for value in optional_strings(item.get("upstream_weights"))),
                strip_prefix=bool(item.get("strip_prefix", False)),
                index=str(item.get("index", "index.html")),
                directory_listing=bool(item.get("directory_listing", False)),
                precompressed=bool(item.get("precompressed", True)),
                load_balance=str(item.get("load_balance", "round_robin")),
                retries=int(item.get("retries", 1)),
                circuit_failures=int(item.get("circuit_failures", 3)),
                circuit_cooldown=float(item.get("circuit_cooldown", 30.0)),
                active_health_path=str(item.get("active_health_path", "/healthz")),
                active_health_interval=float(item.get("active_health_interval", 0.0)),
                active_health_timeout=float(item.get("active_health_timeout", 2.0)),
                proxy_cache=bool(item.get("proxy_cache", False)),
                proxy_cache_ttl=float(item.get("proxy_cache_ttl", 0.0)),
                proxy_cache_max_entries=int(item.get("proxy_cache_max_entries", 1024)),
                proxy_cache_max_bytes=int(item.get("proxy_cache_max_bytes", 1024 * 1024)),
                proxy_cache_path=optional_path(item.get("proxy_cache_path"), base_dir),
                proxy_cache_max_disk_bytes=int(item.get("proxy_cache_max_disk_bytes", 256 * 1024 * 1024)),
                proxy_cache_key=str(item.get("proxy_cache_key", "$protocol $method $host $uri")),
                proxy_cache_methods=tuple(value.upper() for value in optional_strings(item.get("proxy_cache_methods")) or ["GET", "HEAD"]),
                proxy_cache_lock=bool(item.get("proxy_cache_lock", False)),
                proxy_cache_lock_timeout=float(item.get("proxy_cache_lock_timeout", 5.0)),
                proxy_cache_stale_while_revalidate=float(item.get("proxy_cache_stale_while_revalidate", 0.0)),
                proxy_cache_use_stale_on_error=bool(item.get("proxy_cache_use_stale_on_error", False)),
                proxy_cache_purge=bool(item.get("proxy_cache_purge", False)),
                proxy_fallback_path=optional_path(item.get("proxy_fallback_path"), base_dir),
                basic_auth=tuple(load_basic_auth(item.get("basic_auth", {}))),
                auth_realm=str(item.get("auth_realm", "veloxserver")),
                jwt_hs256_secret=optional_string(item.get("jwt_hs256_secret")),
                jwt_issuer=optional_string(item.get("jwt_issuer", item.get("oidc_issuer"))),
                jwt_audience=optional_string(item.get("jwt_audience", item.get("oidc_audience"))),
                jwt_required_claims=tuple(load_string_pairs(item.get("jwt_required_claims", {}))),
                jwt_jwks_file=optional_path(item.get("jwt_jwks_file"), base_dir),
                jwt_jwks_url=optional_string(item.get("jwt_jwks_url", item.get("oidc_jwks_url"))),
                jwt_jwks_cache_ttl=float(item.get("jwt_jwks_cache_ttl", 300.0)),
                external_auth_url=optional_string(item.get("external_auth_url")),
                external_auth_timeout=float(item.get("external_auth_timeout", 2.0)),
                auth_request=optional_string(item.get("auth_request")),
                auth_request_timeout=float(item.get("auth_request_timeout", 2.0)),
                ai_model_path=optional_path(item.get("ai_model_path", item.get("model_path")), base_dir),
                ai_backend=str(item.get("ai_backend", "auto")),
                ai_model_name=str(item.get("ai_model_name", item.get("model", "veloxserver-ai"))),
                ai_system_prompt=str(item.get("ai_system_prompt", item.get("system_prompt", "You are a helpful assistant."))),
                ai_max_tokens=int(item.get("ai_max_tokens", item.get("max_tokens", 512))),
                ai_temperature=float(item.get("ai_temperature", item.get("temperature", 0.7))),
                ai_context_window=int(item.get("ai_context_window", item.get("context_window", 4096))),
                ai_chat_enabled=bool(item.get("ai_chat_enabled", True)),
                ai_api_enabled=bool(item.get("ai_api_enabled", True)),
            )
        )
    return routes


def optional_path(value: Any, base_dir: Path) -> Path | None:
    if value is None:
        return None
    path = Path(str(value))
    if path.is_absolute():
        return path
    return base_dir / path


def optional_string(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def optional_strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def load_pairs(value: Any, first_key: str, second_key: str) -> list[tuple[str, str]]:
    if not isinstance(value, list):
        return []
    pairs = []
    for item in value:
        if isinstance(item, dict):
            pairs.append((str(item[first_key]), str(item[second_key])))
    return pairs


def load_advanced_rewrites(value: Any) -> list[AdvancedRewriteRule]:
    if not isinstance(value, list):
        return []
    rules = []
    for item in value:
        if not isinstance(item, dict):
            continue
        header = None
        raw_header = item.get("header")
        if isinstance(raw_header, dict):
            header = (str(raw_header["name"]).lower(), str(raw_header["pattern"]))
        rules.append(
            AdvancedRewriteRule(
                pattern=str(item["pattern"]),
                replacement=str(item["replacement"]),
                methods=tuple(method.upper() for method in optional_strings(item.get("methods"))),
                hosts=tuple(optional_strings(item.get("hosts"))),
                header=header,
                query=optional_string(item.get("query")),
                stop=bool(item.get("stop", True)),
            )
        )
    return rules


def load_error_pages(value: Any, base_dir: Path) -> list[tuple[int, Path]]:
    if not isinstance(value, dict):
        return []
    return [(int(status), optional_path(path, base_dir) or base_dir) for status, path in value.items()]


def load_basic_auth(value: Any) -> list[tuple[str, str]]:
    if isinstance(value, dict):
        return [(str(username), str(password)) for username, password in value.items()]
    if isinstance(value, list):
        pairs = []
        for item in value:
            if isinstance(item, dict):
                pairs.append((str(item["username"]), str(item["password"])))
        return pairs
    return []


def load_string_pairs(value: Any) -> list[tuple[str, str]]:
    if isinstance(value, dict):
        return [(str(name), str(expected)) for name, expected in value.items()]
    if isinstance(value, list):
        pairs = []
        for item in value:
            if isinstance(item, dict):
                pairs.append((str(item["name"]), str(item["value"])))
        return pairs
    return []


def load_tls_sni(value: Any, base_dir: Path) -> list[tuple[str, Path, Path]]:
    if not isinstance(value, list):
        return []
    entries = []
    for item in value:
        if isinstance(item, dict):
            hostname = str(item["hostname"]).lower()
            certfile = optional_path(item["certfile"], base_dir)
            keyfile = optional_path(item["keyfile"], base_dir)
            if certfile is not None and keyfile is not None:
                entries.append((hostname, certfile, keyfile))
    return entries


def load_stream_proxies(value: Any) -> list[StreamProxyConfig]:
    if not isinstance(value, list):
        return []
    proxies = []
    for item in value:
        if not isinstance(item, dict):
            continue
        proxies.append(
            StreamProxyConfig(
                name=str(item.get("name", f"{item.get('protocol', 'tcp')}:{item.get('listen_port')}")),
                protocol=str(item.get("protocol", "tcp")),
                listen_host=str(item.get("listen_host", "127.0.0.1")),
                listen_port=int(item["listen_port"]),
                upstream_host=str(item["upstream_host"]),
                upstream_port=int(item["upstream_port"]),
                upstreams=tuple(load_stream_upstreams(item.get("upstreams", []))),
                load_balance=str(item.get("load_balance", "round_robin")),
                proxy_protocol=bool(item.get("proxy_protocol", False)),
                max_connections=int(item.get("max_connections", 0)),
                max_fails=int(item.get("max_fails", 3)),
                fail_timeout=float(item.get("fail_timeout", 30.0)),
                buffer_bytes=int(item.get("buffer_bytes", 64 * 1024)),
                timeout=float(item.get("timeout", 300.0)),
            )
        )
    return proxies


def load_stream_upstreams(value: Any) -> list[tuple[str, int]]:
    if not isinstance(value, list):
        return []
    upstreams = []
    for item in value:
        if isinstance(item, dict):
            upstreams.append((str(item["host"]), int(item["port"])))
        elif isinstance(item, str) and ":" in item:
            host, port = item.rsplit(":", 1)
            upstreams.append((host, int(port)))
    return upstreams
