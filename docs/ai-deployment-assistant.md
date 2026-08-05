# AI Deployment Assistant

`veloxserver ai-deploy` turns a project path into a ready-to-review VeloxServer deployment bundle.

It solves a common deployment problem: users know their app folder, but not the exact server config, proxy route, static route, systemd unit, Dockerfile, or error-repair setup. VeloxServer can detect the project shape, generate those files, and optionally ask OpenAI to review the plan.

## Basic Use

Dry run on Linux and macOS:

```bash
veloxserver ai-deploy --project /home/sammy/myprojectdir
```

Dry run on Windows PowerShell:

```powershell
veloxserver ai-deploy --project C:\Users\sammy\myprojectdir
```

Write generated files on Linux and macOS:

```bash
veloxserver ai-deploy --project /home/sammy/myprojectdir --write
```

Write generated files on Windows PowerShell:

```powershell
veloxserver ai-deploy --project C:\Users\sammy\myprojectdir --write
```

Generated files are written to:

```text
/home/sammy/myprojectdir/.veloxserver/generated/
```

## OpenAI Review

Linux and macOS:

```bash
export OPENAI_API_KEY="..."
veloxserver ai-deploy --project /home/sammy/myprojectdir --use-openai --write
```

Windows PowerShell:

```powershell
$env:OPENAI_API_KEY = "..."
veloxserver ai-deploy --project C:\Users\sammy\myprojectdir --use-openai --write
```

The OpenAI review is saved as:

```text
.veloxserver/generated/AI_DEPLOYMENT_REVIEW.md
```

## Full AI-Assisted Flow

Linux and macOS:

```bash
veloxserver ai-deploy \
  --project /home/sammy/myprojectdir \
  --domain example.com \
  --use-openai \
  --write

veloxserver --config /home/sammy/myprojectdir/.veloxserver/generated/veloxserver.toml
```

Windows PowerShell:

```powershell
veloxserver ai-deploy `
  --project C:\Users\sammy\myprojectdir `
  --domain example.com `
  --use-openai `
  --write

veloxserver --config C:\Users\sammy\myprojectdir\.veloxserver\generated\veloxserver.toml
```

The generated config enables AI error repair by default. When deployment errors happen, VeloxServer writes diagnosis and repair suggestions to:

```text
.veloxserver/repair-suggestions/
```

## Guarded Auto-Fix

For development or staging:

Linux and macOS:

```bash
veloxserver ai-deploy --project /home/sammy/myprojectdir --write --auto-apply-repairs
```

Windows PowerShell:

```powershell
veloxserver ai-deploy --project C:\Users\sammy\myprojectdir --write --auto-apply-repairs
```

This generates `ai_error_repair.apply = true`, allowing VeloxServer to apply complete-file fixes inside the project path only. Existing files are backed up before replacement.

Production default is suggestion-only mode.

## Detection

VeloxServer detects:

- Django/Gunicorn projects from `manage.py` or `wsgi.py`
- FastAPI/ASGI projects from `FastAPI(...)` imports
- Node HTTP projects from `package.json`
- static sites from `public`, `dist`, `build`, or `static`

Generated files include:

- `veloxserver.toml`
- `README.md`
- `APP_COMMAND.txt` when an upstream app is needed
- `systemd/veloxserver.service`
- `container/Dockerfile`
- `scripts/run-veloxserver.sh`
- `scripts/run-veloxserver.ps1`
- `deployment-plan.json`

## Safety

- Secrets are not read from environment files or embedded into generated config.
- OpenAI receives bounded context, not the whole project.
- The deployment assistant does not execute shell commands.
- Auto-apply is opt-in and restricted to the project path.
