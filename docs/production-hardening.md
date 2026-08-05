# Production Hardening

VeloxServer has production-facing controls, but it is still alpha software. Treat this as a hardening checklist before any public deployment.

## Runtime

- Run as a dedicated unprivileged user.
- Put writable state under `/run/veloxserver` and logs under `/var/log/veloxserver`.
- Enable `access_log_path`, `error_log_path`, and `log_rotate_bytes`.
- Use `shared_zone_path` when running more than one worker so rate and connection limits are shared.
- Use `upgrade_state_path` and `upgrade_ready_timeout` with `upgrade_command` so old/new generations coordinate before the old process drains.
- Use `open_file_cache_errors`, `open_file_cache_min_uses`, `open_file_cache_inactive`, and `open_file_cache_metadata` to tune static metadata/error caching for your filesystem.
- Use `proxy_cache_path`, `proxy_cache_lock`, `proxy_cache_stale_while_revalidate`, `proxy_cache_use_stale_on_error`, and `proxy_cache_purge` for cacheable upstream routes.
- Use `python tools/proxy_cache_manager.py <cache-dir> --purge-expired --max-bytes <bytes>` as an offline cache manager for large disk caches.
- Set `max_header_bytes`, `max_body_bytes`, `rate_limit_per_minute`, `connection_limit`, and `connection_limit_per_client`.
- Keep `file_io_backend = "auto"` unless benchmarking shows that `threaded` or `read` is better for your filesystem.
- Treat `directio_min_bytes` and `io_uring` as tuning requests, not proof of kernel-level direct I/O until tested on the target OS.
- Keep `directory_listing = false` unless intentionally serving an index.
- Put TLS private keys outside the application directory.

## TLS

- Set `tls_min_version = "TLSv1.2"` or newer.
- Disable tickets with `tls_session_tickets = false` if your environment requires that policy.
- Set `tls_client_verify` and `tls_client_ca_file` for mutual TLS.
- Set `tls_ecdh_curve`, `tls_alpn_protocols`, and `tls_keylog_file` only when you have a clear operational reason.
- Use `tls_sni` entries for hostname-specific certificates.
- Use `tls_ocsp_response_file` only with a known-fresh DER OCSP response from your certificate authority tooling.

## Deployment

- Start from `deploy/systemd/veloxserver.service` for Linux services.
- Start from `deploy/container/Dockerfile` for containers.
- Run `veloxserver validate --config <file>` before restarting a service.
- Run `veloxserver doctor --config <file>` when preparing a new host.
- Prefer a blue/green rollout or process supervisor for upgrades until the binary upgrade lifecycle is proven under real traffic.

## Verification

- Run the unit suite before release.
- Run `python fuzz/fuzz_http_parser.py 100000` for parser stress.
- Run `python fuzz/run_fuzz_campaign.py` to include parser and native ABI fuzzing where the native library is present.
- Run `python tools/http2_stress_matrix.py --port <tls-port> --insecure --streams 1000` against an HTTP/2 TLS listener.
- Run `python tools/tls_probe_matrix.py --port <tls-port> --insecure` for TLS/ALPN smoke output.
- Run benchmarks against your own traffic shape, not only synthetic static-file tests.
