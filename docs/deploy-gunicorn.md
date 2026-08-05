# Deploy With Gunicorn

This guide shows how to run VeloxServer in front of a Gunicorn application.

VeloxServer does not use `sites-available` or `sites-enabled`. You create one TOML config file and run:

```bash
veloxserver --config /etc/veloxserver/myproject.toml
```

On Windows, Gunicorn Unix sockets are not available. Use the TCP upstream pattern shown below with an app server listening on `127.0.0.1:8000`.

## Gunicorn Socket

Run Gunicorn so it listens on a Unix socket:

```bash
gunicorn --workers 3 --bind unix:/run/gunicorn.sock myproject.wsgi:application
```

Or use your existing Gunicorn systemd service/socket.

## VeloxServer Config

```toml
[server]
host = "0.0.0.0"
port = 80
access_log = true
log_format = "json"
access_log_path = "/var/log/veloxserver/access.log"
error_log_path = "/var/log/veloxserver/error.log"
health_path = "/healthz"
metrics_path = "/metrics"
gzip = true
proxy_timeout = 30

[[routes]]
path = "/static/"
kind = "static"
root = "/home/sammy/myprojectdir/static"
precompressed = true
directory_listing = false

[[routes]]
path = "/"
kind = "proxy"
upstreams = ["unix:/run/gunicorn.sock"]
retries = 1
```

The same route can proxy to TCP instead:

```toml
[[routes]]
path = "/"
kind = "proxy"
upstreams = ["http://127.0.0.1:8000"]
```

Windows-friendly TCP config:

```toml
[server]
host = "127.0.0.1"
port = 8080
access_log = true
log_format = "json"

[[routes]]
path = "/"
kind = "proxy"
upstreams = ["http://127.0.0.1:8000"]
```

## Domain Routing

Use `hosts` on routes for server-name style routing:

```toml
[[routes]]
path = "/"
kind = "proxy"
hosts = ["example.com", "www.example.com"]
upstreams = ["unix:/run/gunicorn.sock"]
```

## Enable AI Error Repair

```toml
[ai_error_repair]
enabled = true
project_path = "/home/sammy/myprojectdir"
log_path = "/var/log/veloxserver/ai-repair.log"
suggestions_path = "/home/sammy/myprojectdir/.veloxserver/repair-suggestions"
model = "gpt-4.1-mini"
api_key_env = "OPENAI_API_KEY"
statuses = [500, 502, 503, 504]
apply = false
context_files = [
  "veloxserver.toml",
  "pyproject.toml",
  "requirements.txt",
  "gunicorn.conf.py",
  "myproject/wsgi.py",
]
```

When Gunicorn is down, the socket path is wrong, a route is misconfigured, or the app throws 500s, VeloxServer records a diagnosis and suggested fix under `.veloxserver/repair-suggestions`.

## Test Config

VeloxServer validates config on start:

```bash
veloxserver --config /etc/veloxserver/myproject.toml
```

Windows PowerShell:

```powershell
veloxserver --config C:\path\to\veloxserver.toml
```

For systemd, use the included service file as a starting point:

```text
deploy/systemd/veloxserver.service
```

## Firewall

If VeloxServer listens directly on port 80:

```bash
sudo ufw allow 80/tcp
sudo ufw delete allow 8000/tcp
```

For TLS:

```bash
sudo ufw allow 443/tcp
```

## Validate The Deployment

Start VeloxServer in staging or CI with the same config file before moving it to production. Use `/healthz`, `/metrics`, and the JSON access log to confirm traffic, upstream status, and error-repair behavior.
