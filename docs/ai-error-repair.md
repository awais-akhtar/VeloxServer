# AI Error Repair

VeloxServer can watch server errors, send a bounded and redacted diagnostic package to the OpenAI API, and write repair suggestions back into the project.

The feature is off by default. In safe mode it never changes project files; it writes diagnosis records and optional patches under `.veloxserver/repair-suggestions`. If you explicitly set `apply = true`, VeloxServer may create or replace files inside `project_path`, with backups under `.veloxserver/backups`.

## Install

Linux, macOS, and Windows:

```bash
python -m pip install "veloxserver[ai-repair]"
```

The feature can also work without the OpenAI Python package by using VeloxServer's built-in HTTPS client.

Set your API key outside the config file:

Linux and macOS:

```bash
export OPENAI_API_KEY="..."
```

Windows PowerShell:

```powershell
$env:OPENAI_API_KEY = "..."
```

## Config

```toml
[ai_error_repair]
enabled = true
project_path = "/home/sammy/myprojectdir"
log_path = "/var/log/veloxserver/ai-repair.log"
suggestions_path = "/home/sammy/myprojectdir/.veloxserver/repair-suggestions"
model = "gpt-4.1-mini"
api_key_env = "OPENAI_API_KEY"
min_status = 500
statuses = [500, 502, 503, 504]
cooldown_seconds = 60
apply = false
context_files = [
  "veloxserver.toml",
  "pyproject.toml",
  "requirements.txt",
  "gunicorn.conf.py",
  "myproject/wsgi.py",
]
max_file_bytes = 32768
max_context_bytes = 98304
max_output_tokens = 1600
```

## CLI

Linux and macOS:

```bash
veloxserver --config veloxserver.toml --ai-error-repair --ai-error-repair-project /home/sammy/myprojectdir
```

Windows PowerShell:

```powershell
veloxserver --config veloxserver.toml --ai-error-repair --ai-error-repair-project C:\path\to\project
```

Allow file changes only when you want the server to apply model-proposed complete-file fixes:

```bash
veloxserver --config veloxserver.toml --ai-error-repair --ai-error-repair-apply
```

## What Gets Sent

VeloxServer sends:

- HTTP status, message, method, URL, protocol, and peer address
- redacted request headers
- exception type and traceback for internal exceptions
- project file tree, capped by `max_context_bytes`
- content from selected `context_files`, capped by `max_file_bytes`

VeloxServer redacts common secrets such as authorization headers, cookies, API keys, tokens, passwords, and OpenAI-style secret keys.

## What Gets Written

Each repair attempt writes a JSON record:

```text
.veloxserver/repair-suggestions/repair-<time>-<fingerprint>.json
```

If the model returns a unified diff, VeloxServer also writes:

```text
.veloxserver/repair-suggestions/repair-<time>-<fingerprint>.patch
```

The rolling log receives one JSON line per repair attempt:

```text
logs/ai-repair.log
```

## Auto Apply

`apply = false` is recommended for production. It gives you diagnosis, proposed file changes, operator steps, and tests without changing the app.

`apply = true` allows VeloxServer to write only model-proposed complete files under `project_path`. It refuses absolute paths and parent-directory escapes. Existing files are backed up first.

This mode is best for staging, development, and controlled internal deployments.

## Metrics

The metrics endpoint includes:

```text
veloxserver_ai_error_repairs_total
veloxserver_ai_error_repair_failures_total
```

## Notes

- The OpenAI call runs in the background so users do not wait on the repair assistant.
- Repeated identical errors are cooled down by `cooldown_seconds`.
- The server does not run shell commands from the model.
- Do not enable auto-apply on high-risk production systems until you have reviewed the workflow for your organization.
