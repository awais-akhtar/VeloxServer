from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
from dataclasses import dataclass, replace
from pathlib import Path

from . import __version__
from .config import load_config
from .deploy_ai import AIDeploymentPlanner, DeploymentSettings
from .native import load_native_core
from .server import RouteConfig, ServerConfig, VeloxServer
from .workers import run_worker_pool


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="veloxserver",
        description="VeloxServer: a clean-room HTTP gateway with static, proxy, stream, and AI model routes.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="TOML config file. CLI route flags are ignored when this is set.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Directory to serve. Defaults to the current directory.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host.")
    parser.add_argument("--port", type=int, default=8080, help="Bind port.")
    parser.add_argument(
        "--max-header-bytes",
        type=int,
        default=16 * 1024,
        help="Maximum request header size.",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=256 * 1024,
        help="Fallback file read chunk size.",
    )
    parser.add_argument(
        "--max-body-bytes",
        type=int,
        default=10 * 1024 * 1024,
        help="Maximum buffered request body size.",
    )
    parser.add_argument(
        "--proxy-buffer-bytes",
        type=int,
        default=64 * 1024,
        help="Reverse-proxy response streaming buffer size.",
    )
    parser.add_argument(
        "--file-io-backend",
        choices=["auto", "sendfile", "threaded", "read"],
        default="auto",
        help="Static file I/O backend.",
    )
    parser.add_argument(
        "--no-sendfile",
        action="store_true",
        help="Disable asyncio sendfile even when available.",
    )
    parser.add_argument(
        "--aio-threads",
        type=int,
        default=0,
        help="Use threaded file reads when greater than zero.",
    )
    parser.add_argument(
        "--directio-min-bytes",
        type=int,
        default=0,
        help="Use unbuffered Python reads for files at or above this size. 0 disables.",
    )
    parser.add_argument(
        "--io-uring",
        action="store_true",
        help="Request Linux io_uring tuning when a native backend supports it.",
    )
    parser.add_argument("--workers", type=int, default=1, help="Worker process count. Requires SO_REUSEPORT.")
    parser.add_argument(
        "--upgrade-command",
        help="Command to start a replacement server before graceful shutdown on SIGUSR2.",
    )
    parser.add_argument(
        "--upgrade-ready-timeout",
        type=float,
        default=10.0,
        help="Seconds to wait for the replacement generation to pass health checks.",
    )
    parser.add_argument(
        "--upgrade-state-path",
        type=Path,
        help="JSON file where old/new master upgrade state is written.",
    )
    parser.add_argument(
        "--open-file-cache-entries",
        type=int,
        default=0,
        help="Number of small files to cache in memory. 0 disables the cache.",
    )
    parser.add_argument(
        "--shared-zone-path",
        type=Path,
        help="SQLite file for shared worker rate/connection zones.",
    )
    parser.add_argument(
        "--plugin-path",
        action="append",
        default=[],
        type=Path,
        help="Python plugin file with optional on_request(request) hook.",
    )
    parser.add_argument(
        "--access-log",
        action="store_true",
        help="Print one access-log line per request.",
    )
    parser.add_argument(
        "--access-log-path",
        type=Path,
        help="Write access logs to this file instead of stdout.",
    )
    parser.add_argument(
        "--error-log-path",
        type=Path,
        help="Write server error logs to this file.",
    )
    parser.add_argument(
        "--log-rotate-bytes",
        type=int,
        default=0,
        help="Rotate log files after this many bytes. 0 disables rotation.",
    )
    parser.add_argument(
        "--log-format",
        choices=["plain", "json"],
        default="plain",
        help="Access log format.",
    )
    parser.add_argument(
        "--gzip",
        action="store_true",
        help="Gzip compress eligible static responses when clients support it.",
    )
    parser.add_argument(
        "--health-path",
        default="/healthz",
        help="Health endpoint path.",
    )
    parser.add_argument(
        "--metrics-path",
        default="/metrics",
        help="Prometheus metrics endpoint path.",
    )
    parser.add_argument(
        "--rate-limit-per-minute",
        type=int,
        default=0,
        help="Per-client request limit per minute. 0 disables rate limiting.",
    )
    parser.add_argument(
        "--rate-limit-burst",
        type=int,
        default=0,
        help="Per-client token bucket burst. Defaults to the per-minute limit.",
    )
    parser.add_argument(
        "--connection-limit",
        type=int,
        default=0,
        help="Maximum active client connections. 0 disables the limit.",
    )
    parser.add_argument(
        "--connection-limit-per-client",
        type=int,
        default=0,
        help="Maximum active connections per remote address. 0 disables the limit.",
    )
    parser.add_argument(
        "--waf-block-path",
        action="append",
        default=[],
        help="Regex path pattern to block with 403. Can be repeated.",
    )
    parser.add_argument(
        "--rewrite",
        action="append",
        default=[],
        metavar="PATTERN=REPLACEMENT",
        help="Regex rewrite rule. TOML is recommended for multiple rules.",
    )
    parser.add_argument(
        "--tls-certfile",
        type=Path,
        help="TLS certificate file.",
    )
    parser.add_argument(
        "--tls-keyfile",
        type=Path,
        help="TLS private key file.",
    )
    parser.add_argument(
        "--tls-reload-interval",
        type=float,
        default=5.0,
        help="Seconds between TLS certificate reload checks. 0 disables polling.",
    )
    parser.add_argument(
        "--http2",
        action="store_true",
        help="Enable HTTP/2 over TLS via ALPN. Requires the h2 package.",
    )
    parser.add_argument(
        "--http3",
        action="store_true",
        help="Enable HTTP/3 / QUIC. Requires aioquic and TLS certificate files.",
    )
    parser.add_argument(
        "--http3-port",
        type=int,
        help="UDP port for HTTP/3. Defaults to the main port.",
    )
    parser.add_argument(
        "--native-core",
        default="python",
        help="Native core selector. Use python unless a native veloxcore library is built.",
    )
    parser.add_argument(
        "--native-core-path",
        type=Path,
        help="Directory containing the native veloxcore library.",
    )
    parser.add_argument(
        "--proxy-timeout",
        type=float,
        default=30.0,
        help="Reverse-proxy upstream timeout in seconds.",
    )
    parser.add_argument(
        "--proxy",
        action="append",
        default=[],
        metavar="PREFIX=URL",
        help="Add a reverse-proxy route, for example --proxy /api/=http://127.0.0.1:9000.",
    )
    parser.add_argument(
        "--ai-error-repair",
        action="store_true",
        help="Enable OpenAI-powered error diagnosis and repair suggestions.",
    )
    parser.add_argument(
        "--ai-error-repair-project",
        type=Path,
        help="Project directory sent as bounded context to the repair assistant.",
    )
    parser.add_argument(
        "--ai-error-repair-model",
        default="gpt-4.1-mini",
        help="OpenAI model used for error repair suggestions.",
    )
    parser.add_argument(
        "--ai-error-repair-apply",
        action="store_true",
        help="Allow AI repair file changes under the configured project path.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


async def amain(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv and raw_argv[0] == "ai-deploy":
        return run_ai_deploy(raw_argv[1:])
    if raw_argv and raw_argv[0] == "validate":
        return run_validate(raw_argv[1:])
    if raw_argv and raw_argv[0] == "doctor":
        return run_doctor(raw_argv[1:])

    args = build_parser().parse_args(raw_argv)
    if args.config:
        config = load_config(args.config)
        if args.ai_error_repair or args.ai_error_repair_project is not None or args.ai_error_repair_apply:
            config = replace(
                config,
                ai_error_repair_enabled=args.ai_error_repair or config.ai_error_repair_enabled,
                ai_error_repair_project_path=args.ai_error_repair_project or config.ai_error_repair_project_path,
                ai_error_repair_model=args.ai_error_repair_model or config.ai_error_repair_model,
                ai_error_repair_apply=args.ai_error_repair_apply or config.ai_error_repair_apply,
            )
    else:
        routes = [RouteConfig(path="/", kind="static", root=args.root)]
        routes.extend(parse_proxy_routes(args.proxy))
        config = ServerConfig(
            root=args.root,
            host=args.host,
            port=args.port,
            max_header_bytes=args.max_header_bytes,
            max_body_bytes=args.max_body_bytes,
            chunk_size=args.chunk_size,
            proxy_buffer_bytes=args.proxy_buffer_bytes,
            file_io_backend=args.file_io_backend,
            sendfile=not args.no_sendfile,
            aio_threads=args.aio_threads,
            directio_min_bytes=args.directio_min_bytes,
            io_uring=args.io_uring,
            workers=args.workers,
            reuse_port=args.workers > 1,
            upgrade_command=args.upgrade_command,
            upgrade_ready_timeout=args.upgrade_ready_timeout,
            upgrade_state_path=args.upgrade_state_path,
            open_file_cache_entries=args.open_file_cache_entries,
            shared_zone_path=args.shared_zone_path,
            plugin_paths=tuple(args.plugin_path),
            access_log=args.access_log,
            log_format=args.log_format,
            access_log_path=args.access_log_path,
            error_log_path=args.error_log_path,
            log_rotate_bytes=args.log_rotate_bytes,
            gzip=args.gzip,
            health_path=args.health_path,
            metrics_path=args.metrics_path,
            rewrite_rules=tuple(parse_pairs(args.rewrite, "--rewrite")),
            waf_block_path_patterns=tuple(args.waf_block_path),
            rate_limit_per_minute=args.rate_limit_per_minute,
            rate_limit_burst=args.rate_limit_burst,
            connection_limit=args.connection_limit,
            connection_limit_per_client=args.connection_limit_per_client,
            tls_certfile=args.tls_certfile,
            tls_keyfile=args.tls_keyfile,
            tls_reload_interval=args.tls_reload_interval,
            http2=args.http2,
            http3=args.http3,
            http3_port=args.http3_port,
            native_core=args.native_core,
            native_core_path=args.native_core_path,
            ai_error_repair_enabled=args.ai_error_repair,
            ai_error_repair_project_path=args.ai_error_repair_project,
            ai_error_repair_model=args.ai_error_repair_model,
            ai_error_repair_apply=args.ai_error_repair_apply,
            proxy_timeout=args.proxy_timeout,
            routes=tuple(routes),
        )
    if config.workers > 1:
        return run_worker_pool(config)
    server = VeloxServer(config)
    await server.serve_forever()
    return 0


def parse_proxy_routes(values: list[str]) -> list[RouteConfig]:
    routes = []
    for value in values:
        if "=" not in value:
            raise SystemExit("--proxy must look like PREFIX=URL")
        prefix, upstream = value.split("=", 1)
        routes.append(
            RouteConfig(
                path=prefix,
                kind="proxy",
                upstreams=tuple(item.strip() for item in upstream.split(",") if item.strip()),
                strip_prefix=False,
            )
        )
    return routes


def parse_pairs(values: list[str], flag_name: str) -> list[tuple[str, str]]:
    pairs = []
    for value in values:
        if "=" not in value:
            raise SystemExit(f"{flag_name} must look like PATTERN=REPLACEMENT")
        left, right = value.split("=", 1)
        pairs.append((left, right))
    return pairs


@dataclass(frozen=True)
class Diagnostic:
    level: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"level": self.level, "message": self.message}


def build_validate_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="veloxserver validate",
        description="Validate a VeloxServer TOML configuration without starting the server.",
    )
    parser.add_argument("--config", required=True, type=Path, help="TOML config file to validate.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable diagnostics.")
    return parser


def run_validate(argv: list[str]) -> int:
    args = build_validate_parser().parse_args(argv)
    diagnostics = validate_config_path(args.config)
    emit_diagnostics(diagnostics, json_output=args.json)
    return diagnostics_exit_code(diagnostics)


def validate_config_path(path: Path) -> list[Diagnostic]:
    try:
        config = load_config(path)
    except Exception as exc:
        return [Diagnostic("error", f"could not load {path}: {exc}")]

    diagnostics = [Diagnostic("ok", f"loaded {path}")]
    diagnostics.extend(validate_server_config(config))
    if diagnostics_exit_code(diagnostics) == 0:
        diagnostics.append(Diagnostic("ok", "configuration checks passed"))
    return diagnostics


def validate_server_config(config: ServerConfig) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []

    if config.workers > 1 and sys.platform.startswith("win"):
        diagnostics.append(Diagnostic("error", "worker process mode requires SO_REUSEPORT and is not supported on Windows"))
    if config.http3 and (config.tls_certfile is None or config.tls_keyfile is None):
        diagnostics.append(Diagnostic("error", "HTTP/3 requires tls_certfile and tls_keyfile"))
    if config.tls_certfile is not None and config.tls_keyfile is None:
        diagnostics.append(Diagnostic("error", "tls_certfile is set but tls_keyfile is missing"))
    if config.tls_keyfile is not None and config.tls_certfile is None:
        diagnostics.append(Diagnostic("error", "tls_keyfile is set but tls_certfile is missing"))

    for label, path in (
        ("TLS certificate", config.tls_certfile),
        ("TLS private key", config.tls_keyfile),
        ("TLS client CA", config.tls_client_ca_file),
        ("OCSP response", config.tls_ocsp_response_file),
    ):
        if path is not None and not path.exists():
            diagnostics.append(Diagnostic("error", f"{label} file does not exist: {path}"))

    for plugin_path in config.plugin_paths:
        if not plugin_path.exists():
            diagnostics.append(Diagnostic("error", f"plugin file does not exist: {plugin_path}"))

    seen_routes: set[tuple[tuple[str, ...], str]] = set()
    for route in config.routes:
        route_key = (route.hosts, route.normalized_path())
        if route_key in seen_routes:
            diagnostics.append(Diagnostic("warning", f"duplicate route path for hosts {route.hosts or ('*',)}: {route.path}"))
        seen_routes.add(route_key)

        route_name = f"route {route.path} ({route.kind})"
        if route.kind == "static":
            root = route.root or config.root
            if root is None:
                diagnostics.append(Diagnostic("error", f"{route_name} has no root directory"))
            elif not root.exists():
                diagnostics.append(Diagnostic("error", f"{route_name} static root does not exist: {root}"))
            elif not root.is_dir():
                diagnostics.append(Diagnostic("error", f"{route_name} static root is not a directory: {root}"))
        elif route.kind == "proxy":
            if not route.proxy_upstreams():
                diagnostics.append(Diagnostic("error", f"{route_name} has no upstreams"))
        elif route.kind == "ai":
            if route.ai_model_path is not None and not route.ai_model_path.exists():
                diagnostics.append(Diagnostic("warning", f"{route_name} model file does not exist yet: {route.ai_model_path}"))
            if route.ai_backend in {"llama", "llama-cpp", "llama_cpp"} and route.ai_model_path is None:
                diagnostics.append(Diagnostic("error", f"{route_name} requires ai_model_path for the llama backend"))
        else:
            diagnostics.append(Diagnostic("error", f"{route_name} uses unsupported route kind"))

    if config.ai_error_repair_enabled:
        project = config.ai_error_repair_project_path or Path.cwd()
        if not project.exists():
            diagnostics.append(Diagnostic("error", f"AI error repair project path does not exist: {project}"))
        if not os.environ.get(config.ai_error_repair_api_key_env):
            diagnostics.append(Diagnostic("warning", f"{config.ai_error_repair_api_key_env} is not set; repair calls will be skipped or fail"))

    return diagnostics


def build_doctor_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="veloxserver doctor",
        description="Check the local VeloxServer runtime environment.",
    )
    parser.add_argument("--config", type=Path, help="Also validate a config file.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable diagnostics.")
    return parser


def run_doctor(argv: list[str]) -> int:
    args = build_doctor_parser().parse_args(argv)
    diagnostics = runtime_diagnostics()
    if args.config is not None:
        diagnostics.extend(validate_config_path(args.config))
    emit_diagnostics(diagnostics, json_output=args.json)
    return diagnostics_exit_code(diagnostics)


def runtime_diagnostics() -> list[Diagnostic]:
    diagnostics = [
        Diagnostic("ok" if sys.version_info >= (3, 11) else "error", f"Python {sys.version.split()[0]}"),
    ]
    for module_name, label in (
        ("h2", "HTTP/2 support"),
        ("aioquic", "HTTP/3 support"),
        ("cryptography", "TLS helpers"),
        ("openai", "AI repair SDK"),
        ("transformers", "transformers model backend"),
        ("llama_cpp", "llama.cpp model backend"),
    ):
        diagnostics.append(optional_module_diagnostic(module_name, label))

    native_status = load_native_core("rust")
    diagnostics.append(
        Diagnostic(
            "ok" if native_status.available else "warning",
            f"native core: {native_status.message}" + (f" ({native_status.path})" if native_status.path else ""),
        )
    )
    return diagnostics


def optional_module_diagnostic(module_name: str, label: str) -> Diagnostic:
    if importlib.util.find_spec(module_name) is None:
        return Diagnostic("warning", f"{label} optional module is not installed: {module_name}")
    return Diagnostic("ok", f"{label} optional module is installed: {module_name}")


def emit_diagnostics(diagnostics: list[Diagnostic], json_output: bool = False) -> None:
    if json_output:
        print(json.dumps([item.as_dict() for item in diagnostics], indent=2))
        return
    for item in diagnostics:
        print(f"{item.level.upper():7} {item.message}")


def diagnostics_exit_code(diagnostics: list[Diagnostic]) -> int:
    return 1 if any(item.level == "error" for item in diagnostics) else 0


def build_ai_deploy_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="veloxserver ai-deploy",
        description="Inspect a project and generate VeloxServer deployment files with AI-assisted guidance.",
    )
    parser.add_argument("--project", type=Path, default=Path.cwd(), help="Project directory to deploy.")
    parser.add_argument("--output-dir", type=Path, help="Where generated deployment files should be written.")
    parser.add_argument("--domain", default="server_domain_or_IP", help="Domain or IP for generated host routing.")
    parser.add_argument("--host", default="0.0.0.0", help="Generated VeloxServer bind host.")
    parser.add_argument("--port", type=int, default=8080, help="Generated VeloxServer bind port.")
    parser.add_argument("--app-port", type=int, default=8000, help="Upstream app port for ASGI/Node deployments.")
    parser.add_argument("--gunicorn-socket", default="/run/gunicorn.sock", help="Unix socket path for Django/Gunicorn deployments.")
    parser.add_argument("--model", default="gpt-4.1-mini", help="OpenAI model for deployment review.")
    parser.add_argument("--api-key-env", default="OPENAI_API_KEY", help="Environment variable holding the OpenAI API key.")
    parser.add_argument("--base-url", default="https://api.openai.com/v1", help="OpenAI-compatible API base URL.")
    parser.add_argument("--use-openai", action="store_true", help="Ask OpenAI to review and improve the generated deployment plan.")
    parser.add_argument("--write", action="store_true", help="Write generated files to disk.")
    parser.add_argument("--no-error-repair", action="store_true", help="Do not enable AI error repair in the generated config.")
    parser.add_argument("--auto-apply-repairs", action="store_true", help="Generate config with guarded repair auto-apply enabled.")
    return parser


def run_ai_deploy(argv: list[str]) -> int:
    args = build_ai_deploy_parser().parse_args(argv)
    planner = AIDeploymentPlanner(
        DeploymentSettings(
            project_path=args.project,
            output_dir=args.output_dir,
            domain=args.domain,
            host=args.host,
            port=args.port,
            app_port=args.app_port,
            gunicorn_socket=args.gunicorn_socket,
            model=args.model,
            api_key_env=args.api_key_env,
            base_url=args.base_url,
            use_openai=args.use_openai,
            write_files=args.write,
            enable_error_repair=not args.no_error_repair,
            auto_apply_repairs=args.auto_apply_repairs,
        )
    )
    plan = planner.build_plan()
    print(f"Detected project type: {plan.profile.kind} ({plan.profile.confidence})")
    print(f"Upstream: {plan.profile.upstream or 'none'}")
    print(f"Static root: {plan.profile.static_root or 'none'}")
    print("Generated files:")
    for name in sorted(plan.files):
        print(f"  {name}")
    if args.write:
        written = planner.write_plan(plan)
        print("Written:")
        for path in written:
            print(f"  {path}")
    else:
        print("Dry run only. Re-run with --write to create files.")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(amain(argv))
    except KeyboardInterrupt:
        return 130
