# VeloxServer

VeloxServer is a clean-room, pip-installable HTTP gateway and web server for static sites, reverse proxying, stream proxying, TLS, metrics, authentication, caching, plugin hooks, AI model routes, and AI-assisted deployment repair.

It is configured with TOML, runs from a single command, and is designed to be easy to try, easy to deploy, and easy to extend.

> Status: VeloxServer is alpha software. It has production-facing controls, but public internet deployments should still be tested, benchmarked, and reviewed for your environment.

Source repository: [awais-akhtar/VeloxServer](https://github.com/awais-akhtar/VeloxServer)

## Install

PyPI package:

```bash
python -m pip install veloxserver
```

Optional AI features:

```bash
python -m pip install "veloxserver[ai-repair]"
python -m pip install "veloxserver[ai-llama]"
python -m pip install "veloxserver[ai-transformers]"
```

Install from a cloned Git checkout for development:

```bash
cd veloxserver
python -m pip install -e ".[ai-repair]"
```

Verify the command:

```bash
veloxserver --version
```

## Quick Start

Linux and macOS:

```bash
mkdir -p public
printf '<h1>VeloxServer works</h1>\n' > public/index.html
veloxserver --root public --host 127.0.0.1 --port 8080
```

Windows PowerShell:

```powershell
New-Item -ItemType Directory -Force public
Set-Content public/index.html "<h1>VeloxServer works</h1>"
veloxserver --root public --host 127.0.0.1 --port 8080
```

Open:

```text
http://127.0.0.1:8080/
```

Run with the included example config:

```bash
veloxserver --config examples/veloxserver.toml
```

## Common Commands

Serve static files:

```bash
veloxserver --root public --port 8080
```

Serve static files with gzip, access logs, health, and metrics:

```bash
veloxserver --root public --gzip --access-log --health-path /healthz --metrics-path /metrics
```

Reverse proxy `/api/` to an app server:

```bash
veloxserver --root public --proxy /api/=http://127.0.0.1:9000
```

Proxy to Gunicorn through a Unix socket on Linux/macOS:

```bash
veloxserver --proxy /=unix:/run/gunicorn.sock
```

Proxy to Gunicorn, Uvicorn, Node, or any HTTP app through TCP:

```bash
veloxserver --proxy /=http://127.0.0.1:8000
```

Enable TLS and HTTP/2:

```bash
veloxserver --root public --tls-certfile certs/fullchain.pem --tls-keyfile certs/privkey.pem --http2
```

Use worker processes on platforms with `SO_REUSEPORT`:

```bash
veloxserver --root public --workers 4 --shared-zone-path run/zones.sqlite3
```

Show all CLI options:

```bash
veloxserver --help
```

Validate a config file before starting the server:

```bash
veloxserver validate --config examples/veloxserver.toml
```

Check optional runtime support on the current machine:

```bash
veloxserver doctor
```

## Configuration

VeloxServer uses TOML. See [examples/veloxserver.toml](examples/veloxserver.toml) for a larger config.

Minimal static site:

```toml
[server]
host = "127.0.0.1"
port = 8080
access_log = true
metrics_path = "/metrics"
health_path = "/healthz"

[admin]
enabled = true
path = "/__veloxserver"
reload_path = "/reload"
status_path = "/status"

[[routes]]
path = "/"
kind = "static"
root = "public"
```

Reverse proxy with load balancing, retries, and cache controls:

```toml
[server]
host = "0.0.0.0"
port = 8080
access_log = true
log_format = "json"
access_log_path = "logs/access.log"
error_log_path = "logs/error.log"

[[routes]]
path = "/api/"
kind = "proxy"
upstreams = ["http://127.0.0.1:9000", "http://127.0.0.1:9001"]
load_balance = "least_connections"
retries = 1
proxy_cache = true
proxy_cache_ttl = 30
proxy_cache_path = "cache/api"
proxy_cache_lock = true
proxy_cache_stale_while_revalidate = 30
proxy_cache_use_stale_on_error = true
proxy_cache_purge = true
proxy_fallback_path = "cache/fallback.html"
```

Virtual host routing:

```toml
[[routes]]
path = "/"
kind = "static"
hosts = ["example.com", "www.example.com"]
root = "/srv/example.com/public"
```

Custom error pages:

```toml
[server]
error_pages = { 404 = "errors/404.html", 500 = "errors/500.html" }
```

## AI Deployment Assistant

VeloxServer can inspect a project path and generate a deployment bundle with:

- `veloxserver.toml`
- systemd service file
- Dockerfile
- run scripts
- deployment README
- AI error repair settings

Linux/macOS:

```bash
veloxserver ai-deploy --project /home/sammy/myprojectdir --domain example.com --write
```

Windows PowerShell:

```powershell
veloxserver ai-deploy --project C:\Users\sammy\myprojectdir --domain example.com --write
```

Ask OpenAI to review the deployment plan:

Linux/macOS:

```bash
export OPENAI_API_KEY="..."
veloxserver ai-deploy --project /home/sammy/myprojectdir --domain example.com --use-openai --write
```

Windows PowerShell:

```powershell
$env:OPENAI_API_KEY = "..."
veloxserver ai-deploy --project C:\Users\sammy\myprojectdir --domain example.com --use-openai --write
```

Generated files are written to:

```text
<project>/.veloxserver/generated/
```

More details: [docs/ai-deployment-assistant.md](docs/ai-deployment-assistant.md).

## AI Error Repair

AI error repair watches server errors such as `500`, `502`, `503`, and `504`, sends bounded and redacted diagnostic context to the configured OpenAI API, and writes repair suggestions into the project.

Install the optional dependency:

```bash
python -m pip install "veloxserver[ai-repair]"
```

Enable it in config:

```toml
[ai_error_repair]
enabled = true
project_path = "/home/sammy/myprojectdir"
log_path = "logs/ai-repair.log"
suggestions_path = ".veloxserver/repair-suggestions"
model = "gpt-4.1-mini"
api_key_env = "OPENAI_API_KEY"
statuses = [500, 502, 503, 504]
apply = false
context_files = ["veloxserver.toml", "pyproject.toml", "requirements.txt"]
```

Run with CLI flags:

Linux/macOS:

```bash
export OPENAI_API_KEY="..."
veloxserver --config veloxserver.toml --ai-error-repair --ai-error-repair-project /home/sammy/myprojectdir
```

Windows PowerShell:

```powershell
$env:OPENAI_API_KEY = "..."
veloxserver --config veloxserver.toml --ai-error-repair --ai-error-repair-project C:\Users\sammy\myprojectdir
```

By default, VeloxServer writes suggestions only. Guarded file changes require explicit opt-in:

```bash
veloxserver --config veloxserver.toml --ai-error-repair --ai-error-repair-apply
```

More details: [docs/ai-error-repair.md](docs/ai-error-repair.md).

## AI Model Routes

VeloxServer can expose local models through a browser chat UI and OpenAI-style API endpoints.

```toml
[[routes]]
path = "/ai/"
kind = "ai"
ai_backend = "auto"
ai_model_path = "models/local-model.gguf"
ai_model_name = "local-assistant"
ai_system_prompt = "You are the local assistant for this VeloxServer deployment."
ai_max_tokens = 512
ai_temperature = 0.7
ai_context_window = 4096
ai_chat_enabled = true
ai_api_enabled = true
```

Open the chat UI:

```text
http://127.0.0.1:8080/ai/
```

List models:

```bash
curl http://127.0.0.1:8080/ai/v1/models
```

Chat completion:

Linux/macOS:

```bash
curl -X POST http://127.0.0.1:8080/ai/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Hello"}]}'
```

Windows PowerShell:

```powershell
curl.exe -X POST http://127.0.0.1:8080/ai/v1/chat/completions `
  -H "Content-Type: application/json" `
  -d "{\"messages\":[{\"role\":\"user\",\"content\":\"Hello\"}]}"
```

Supported AI backends:

- `echo`: built-in deterministic backend for tests and development
- `llama_cpp`: local GGUF/GGML-style model files through `llama-cpp-python`
- `transformers`: local Hugging Face model folders or model ids through `transformers`
- `auto`: chooses a backend from the configured model path

More details: [docs/ai-model-routes.md](docs/ai-model-routes.md).

## Deployment Examples

Django or Gunicorn through a Unix socket:

```toml
[server]
host = "0.0.0.0"
port = 80
access_log = true
log_format = "json"
access_log_path = "/var/log/veloxserver/access.log"
error_log_path = "/var/log/veloxserver/error.log"

[[routes]]
path = "/static/"
kind = "static"
root = "/home/sammy/myprojectdir/static"

[[routes]]
path = "/"
kind = "proxy"
hosts = ["example.com", "www.example.com"]
upstreams = ["unix:/run/gunicorn.sock"]
```

FastAPI, Uvicorn, Node, or another TCP app:

```toml
[[routes]]
path = "/"
kind = "proxy"
hosts = ["example.com"]
upstreams = ["http://127.0.0.1:8000"]
```

More details: [docs/deploy-gunicorn.md](docs/deploy-gunicorn.md) and [docs/production-hardening.md](docs/production-hardening.md).

## Features

- HTTP/1.0 and HTTP/1.1
- HTTP/2 over TLS ALPN with `hyper-h2`
- HTTP/3 over QUIC with `aioquic`
- static file serving with safe path resolution
- ETag, Last-Modified, If-None-Match, and If-Modified-Since
- gzip compression and precompressed `.br` / `.gz` assets
- reverse proxy routes with upstream pools
- round-robin, first-available, least-connections, hash, IP-hash, and weighted balancing
- retries, passive circuit opening, and active health checks
- chunked and trailer-aware upstream response handling
- WebSocket-style HTTP/1.1 upgrade tunneling
- disk-backed proxy cache with keys, locks, purge, stale-while-revalidate, stale-on-error, and cleanup
- virtual hosts by `Host` header
- rewrite rules and advanced rewrite conditions
- WAF path blocks and WAF plugin hooks
- basic auth, HS256 JWT auth, RS256 JWKS/OIDC-style JWT auth
- external auth URL checks and internal auth subrequests
- Python plugin hooks and native dynamic module hook ABI
- AI model routes with API and web chat access
- AI deployment assistant for generated deployment bundles
- OpenAI-powered error diagnosis and guarded repair suggestions
- TCP, UDP, SMTP, IMAP, POP3, and DNS stream proxy naming
- TLS certificate reload polling
- TLS cipher, minimum-version, session-ticket, client-cert, ECDH, ALPN, keylog, SNI, and OCSP response knobs
- worker process mode on platforms with `SO_REUSEPORT`
- SQLite-backed shared rate-limit and connection-limit zones
- JSON or plain access logs with rotation
- `/healthz` health endpoint and Prometheus-style `/metrics`
- optional Rust native core for selected hot-path work
- graceful config reload for runtime-safe TOML changes

## Limitations

- VeloxServer is alpha.
- Broad native hot-path coverage is not complete.
- HTTP/3 compatibility must be tested against the browsers and clients you support.
- Independent security audit has not been completed.
- Benchmark claims should be generated with the included benchmark tools and your own environment.
- AI backends require model files and optional dependencies that are not bundled by default.
- AI repair sends bounded, redacted diagnostic context to the configured OpenAI-compatible API only when enabled.
- AI repair defaults to suggestion-only mode; file-changing auto-apply must be explicitly enabled.
- Native wheels with bundled Rust libraries require platform-specific CI builds.

## Development

Clone and install editable:

```bash
cd veloxserver
python -m pip install -e ".[ai-repair]"
```

Run tests:

```bash
python -B -c "import sys, unittest; sys.path.insert(0, 'src'); suite = unittest.defaultTestLoader.discover('tests'); result = unittest.TextTestRunner(verbosity=2).run(suite); raise SystemExit(0 if result.wasSuccessful() else 1)"
```

Run fuzz smoke tests:

```bash
python -B fuzz/run_fuzz_campaign.py
```

Build package artifacts:

```bash
python -m pip install build twine
python -m build
python -m twine check dist/*
```

PyPI releases can be published through GitHub Actions Trusted Publishing. Before the first upload for a new or recreated PyPI project, add a pending GitHub publisher with project `veloxserver`, owner `awais-akhtar`, repository `VeloxServer`, workflow `release.yml`, and environment `pypi`.

Contributor and release details live in [CONTRIBUTING.md](CONTRIBUTING.md) and [docs/publishing.md](docs/publishing.md).

## Documentation

- [AI deployment assistant](docs/ai-deployment-assistant.md)
- [AI error repair](docs/ai-error-repair.md)
- [AI model routes](docs/ai-model-routes.md)
- [Auth and WAF](docs/auth-and-waf.md)
- [Deploy with Gunicorn](docs/deploy-gunicorn.md)
- [Dynamic modules](docs/dynamic-modules.md)
- [HTTP/3 compatibility](docs/http3-compatibility.md)
- [Native core](docs/native-core.md)
- [Production hardening](docs/production-hardening.md)

## Security

Security policy: [SECURITY.md](SECURITY.md). Audit checklist: [SECURITY_AUDIT.md](SECURITY_AUDIT.md).

## License

VeloxServer is licensed under the MIT License. See [LICENSE](LICENSE).
