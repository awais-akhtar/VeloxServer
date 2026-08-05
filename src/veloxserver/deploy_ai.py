from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .repair import extract_response_text, project_tree, redact_text, resolve_project_file


@dataclass(frozen=True)
class DeploymentSettings:
    project_path: Path
    output_dir: Path | None = None
    domain: str = "server_domain_or_IP"
    host: str = "0.0.0.0"
    port: int = 8080
    app_port: int = 8000
    gunicorn_socket: str = "/run/gunicorn.sock"
    model: str = "gpt-4.1-mini"
    api_key_env: str = "OPENAI_API_KEY"
    base_url: str = "https://api.openai.com/v1"
    timeout: float = 30.0
    use_openai: bool = False
    write_files: bool = False
    enable_error_repair: bool = True
    auto_apply_repairs: bool = False
    max_context_bytes: int = 96 * 1024

    def project(self) -> Path:
        return self.project_path.resolve()

    def output(self) -> Path:
        if self.output_dir is not None:
            return self.output_dir.resolve()
        return self.project() / ".veloxserver" / "generated"


@dataclass(frozen=True)
class ProjectProfile:
    kind: str
    app_command: str
    upstream: str
    static_root: str | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)
    confidence: str = "medium"


@dataclass(frozen=True)
class DeploymentPlan:
    project_path: Path
    profile: ProjectProfile
    files: dict[str, str]
    operator_steps: tuple[str, ...]
    ai_notes: str | None = None

    def summary(self) -> dict[str, object]:
        return {
            "project_path": str(self.project_path),
            "kind": self.profile.kind,
            "confidence": self.profile.confidence,
            "upstream": self.profile.upstream,
            "static_root": self.profile.static_root,
            "files": sorted(self.files),
            "operator_steps": list(self.operator_steps),
            "ai_notes": self.ai_notes,
        }


class AIDeploymentPlanner:
    def __init__(
        self,
        settings: DeploymentSettings,
        responder: Callable[[str], str] | None = None,
    ) -> None:
        self.settings = settings
        self.responder = responder

    def build_plan(self) -> DeploymentPlan:
        project = self.settings.project()
        profile = detect_project_profile(project, self.settings)
        files = build_deployment_files(project, profile, self.settings)
        steps = build_operator_steps(profile, self.settings)
        ai_notes = None
        if self.settings.use_openai:
            ai_notes = self._ask_openai(project, profile, files)
            files["AI_DEPLOYMENT_REVIEW.md"] = ai_notes
        return DeploymentPlan(project, profile, files, steps, ai_notes)

    def write_plan(self, plan: DeploymentPlan) -> list[Path]:
        output = self.settings.output()
        output.mkdir(parents=True, exist_ok=True)
        written = []
        for relative, content in plan.files.items():
            target = output / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            written.append(target)
        summary = output / "deployment-plan.json"
        summary.write_text(json.dumps(plan.summary(), indent=2), encoding="utf-8")
        written.append(summary)
        return written

    def _ask_openai(self, project: Path, profile: ProjectProfile, files: dict[str, str]) -> str:
        prompt = json.dumps(
            {
                "task": "review_and_improve_veloxserver_deployment_plan",
                "rules": [
                    "Return concise markdown.",
                    "Do not include secrets.",
                    "Do not suggest destructive commands.",
                    "Focus on deployment reliability, security, and easy recovery.",
                ],
                "project": {
                    "path": str(project),
                    "tree": project_tree(project),
                    "profile": {
                        "kind": profile.kind,
                        "app_command": profile.app_command,
                        "upstream": profile.upstream,
                        "static_root": profile.static_root,
                        "notes": list(profile.notes),
                    },
                    "context": collect_deploy_context(project, self.settings.max_context_bytes),
                },
                "generated_files": files,
            },
            indent=2,
        )
        if self.responder is not None:
            return self.responder(prompt)
        return call_openai_for_deploy(prompt, self.settings)


def detect_project_profile(project: Path, settings: DeploymentSettings) -> ProjectProfile:
    files = {path.name.lower(): path for path in project.iterdir() if path.is_file()} if project.exists() else {}
    tree = project_tree(project)
    lowered = {item.lower() for item in tree}

    if "manage.py" in files or any(item.endswith("/wsgi.py") for item in lowered):
        static_root = first_existing_dir(project, ["staticfiles", "static", "public"])
        return ProjectProfile(
            kind="django-gunicorn",
            app_command="gunicorn --workers 3 --bind unix:/run/gunicorn.sock myproject.wsgi:application",
            upstream=f"unix:{settings.gunicorn_socket}",
            static_root=static_root,
            notes=("Detected Django-style project. Update the WSGI module name if it is not myproject.wsgi.",),
            confidence="high",
        )

    if looks_like_fastapi(project, tree):
        static_root = first_existing_dir(project, ["static", "public"])
        return ProjectProfile(
            kind="fastapi-uvicorn",
            app_command=f"uvicorn main:app --host 127.0.0.1 --port {settings.app_port}",
            upstream=f"http://127.0.0.1:{settings.app_port}",
            static_root=static_root,
            notes=("Detected FastAPI/ASGI-style project. Update main:app if your application object differs.",),
            confidence="medium",
        )

    if "package.json" in files:
        static_root = first_existing_dir(project, ["dist", "build", "public", "static"])
        return ProjectProfile(
            kind="node-http",
            app_command=f"npm start -- --host 127.0.0.1 --port {settings.app_port}",
            upstream=f"http://127.0.0.1:{settings.app_port}",
            static_root=static_root,
            notes=("Detected Node project. Confirm the app accepts host/port arguments.",),
            confidence="medium",
        )

    static_root = first_existing_dir(project, ["public", "dist", "build", "static", "."])
    return ProjectProfile(
        kind="static",
        app_command="",
        upstream="",
        static_root=static_root or ".",
        notes=("Detected static or unknown project. VeloxServer will serve files directly.",),
        confidence="low",
    )


def looks_like_fastapi(project: Path, tree: list[str]) -> bool:
    candidates = [project / "main.py", project / "app.py"]
    candidates.extend(project / item for item in tree if item.endswith(".py") and len(item.split("/")) <= 2)
    for path in candidates[:30]:
        if not path.exists() or not path.is_file() or path.stat().st_size > 64 * 1024:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "FastAPI(" in text or "from fastapi" in text or "import fastapi" in text:
            return True
    return False


def first_existing_dir(project: Path, names: list[str]) -> str | None:
    for name in names:
        path = project / name
        if path.exists() and path.is_dir():
            return name
    return None


def build_deployment_files(project: Path, profile: ProjectProfile, settings: DeploymentSettings) -> dict[str, str]:
    files = {
        "veloxserver.toml": render_veloxserver_config(project, profile, settings),
        "README.md": render_deploy_readme(profile, settings),
        "systemd/veloxserver.service": render_systemd_service(settings),
        "container/Dockerfile": render_dockerfile(profile, settings),
        "scripts/run-veloxserver.sh": render_run_script(settings),
        "scripts/run-veloxserver.ps1": render_run_script_ps(settings),
    }
    if profile.app_command:
        files["APP_COMMAND.txt"] = profile.app_command + "\n"
    return files


def render_veloxserver_config(project: Path, profile: ProjectProfile, settings: DeploymentSettings) -> str:
    lines = [
        "[server]",
        f'host = "{settings.host}"',
        f"port = {settings.port}",
        "access_log = true",
        'log_format = "json"',
        'access_log_path = "logs/access.log"',
        'error_log_path = "logs/error.log"',
        "log_rotate_bytes = 10485760",
        "gzip = true",
        'health_path = "/healthz"',
        'metrics_path = "/metrics"',
        "proxy_timeout = 30.0",
        "",
    ]
    if settings.enable_error_repair:
        lines.extend(
            [
                "[ai_error_repair]",
                "enabled = true",
                f'project_path = "{toml_string(str(project))}"',
                'log_path = "logs/ai-repair.log"',
                'suggestions_path = ".veloxserver/repair-suggestions"',
                f'model = "{settings.model}"',
                f'api_key_env = "{settings.api_key_env}"',
                "statuses = [500, 502, 503, 504]",
                f"apply = {str(settings.auto_apply_repairs).lower()}",
                'context_files = ["veloxserver.toml", "pyproject.toml", "requirements.txt", "package.json"]',
                "",
            ]
        )
    if profile.static_root:
        lines.extend(
            [
                "[[routes]]",
                'path = "/static/"' if profile.kind != "static" else 'path = "/"',
                'kind = "static"',
                f'root = "{toml_string(str(project / profile.static_root))}"',
                "precompressed = true",
                "directory_listing = false",
                "",
            ]
        )
    if profile.upstream:
        lines.extend(
            [
                "[[routes]]",
                'path = "/"',
                'kind = "proxy"',
                f'hosts = ["{settings.domain}"]',
                f'upstreams = ["{profile.upstream}"]',
                "retries = 1",
                "circuit_failures = 3",
                "circuit_cooldown = 30.0",
                "",
            ]
        )
    return "\n".join(lines)


def render_deploy_readme(profile: ProjectProfile, settings: DeploymentSettings) -> str:
    steps = "\n".join(f"{index}. {step}" for index, step in enumerate(build_operator_steps(profile, settings), start=1))
    return f"""# VeloxServer AI Deployment

Detected project type: `{profile.kind}`  
Confidence: `{profile.confidence}`

## App Command

```bash
{profile.app_command or "No upstream app command needed for static deployment."}
```

## Run

```bash
veloxserver --config veloxserver.toml
```

## Steps

{steps}

## AI Repair

AI error repair is configured in `veloxserver.toml`. It writes suggestions to `.veloxserver/repair-suggestions` and uses `{settings.api_key_env}` for the OpenAI API key.
"""


def render_systemd_service(settings: DeploymentSettings) -> str:
    return f"""[Unit]
Description=VeloxServer
After=network.target

[Service]
Type=simple
WorkingDirectory={settings.output()}
Environment=OPENAI_API_KEY=
ExecStart=/usr/bin/env veloxserver --config {settings.output() / "veloxserver.toml"}
Restart=on-failure
RestartSec=2
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full

[Install]
WantedBy=multi-user.target
"""


def render_dockerfile(profile: ProjectProfile, settings: DeploymentSettings) -> str:
    app_note = f"# Start your upstream app separately: {profile.app_command}" if profile.app_command else "# Static deployment does not need an upstream app."
    return f"""FROM python:3.13-slim
WORKDIR /app
RUN python -m pip install --no-cache-dir veloxserver[ai-repair]
COPY . /app
{app_note}
EXPOSE {settings.port}
CMD ["veloxserver", "--config", ".veloxserver/generated/veloxserver.toml"]
"""


def render_run_script(settings: DeploymentSettings) -> str:
    return f"""#!/usr/bin/env sh
set -eu
veloxserver --config "{settings.output() / "veloxserver.toml"}"
"""


def render_run_script_ps(settings: DeploymentSettings) -> str:
    return f"""$ErrorActionPreference = "Stop"
veloxserver --config "{settings.output() / "veloxserver.toml"}"
"""


def build_operator_steps(profile: ProjectProfile, settings: DeploymentSettings) -> tuple[str, ...]:
    steps = [
        f"Set {settings.api_key_env} in the service environment if AI repair is enabled.",
        "Review generated veloxserver.toml and replace placeholder domain/module names.",
    ]
    if profile.app_command:
        steps.append(f"Start the upstream app with: {profile.app_command}")
    steps.extend(
        [
            "Run: veloxserver --config veloxserver.toml",
            f"Open http://127.0.0.1:{settings.port}/healthz",
            "If errors happen, review .veloxserver/repair-suggestions.",
        ]
    )
    return tuple(steps)


def collect_deploy_context(project: Path, max_bytes: int) -> dict[str, str]:
    result = {}
    budget = max_bytes
    for name in ["pyproject.toml", "requirements.txt", "package.json", "manage.py", "gunicorn.conf.py", "README.md"]:
        path = project / name
        if not path.exists() or not path.is_file() or path.stat().st_size > 64 * 1024:
            continue
        text = redact_text(path.read_text(encoding="utf-8", errors="replace"))
        size = len(text.encode("utf-8"))
        if size > budget:
            break
        result[name] = text
        budget -= size
    return result


def call_openai_for_deploy(prompt: str, settings: DeploymentSettings) -> str:
    api_key = os.environ.get(settings.api_key_env, "")
    if not api_key:
        return f"OpenAI review skipped because {settings.api_key_env} is not set.\n"
    try:
        from openai import OpenAI  # type: ignore[import-not-found]
    except ImportError:
        return call_openai_for_deploy_rest(prompt, settings, api_key)

    kwargs: dict[str, object] = {"api_key": api_key}
    if settings.base_url != "https://api.openai.com/v1":
        kwargs["base_url"] = settings.base_url
    client = OpenAI(**kwargs)
    response = client.responses.create(
        model=settings.model,
        instructions=DEPLOY_INSTRUCTIONS,
        input=prompt,
        max_output_tokens=1800,
    )
    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str) and output_text:
        return output_text
    if hasattr(response, "model_dump"):
        return extract_response_text(response.model_dump())
    return str(response)


def call_openai_for_deploy_rest(prompt: str, settings: DeploymentSettings, api_key: str) -> str:
    payload = {
        "model": settings.model,
        "instructions": DEPLOY_INSTRUCTIONS,
        "input": prompt,
        "max_output_tokens": 1800,
    }
    request = urllib.request.Request(
        f"{settings.base_url.rstrip('/')}/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "veloxserver-ai-deploy",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=settings.timeout) as response:
            body = response.read(4 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        detail = exc.read(64 * 1024).decode("utf-8", errors="replace")
        return f"OpenAI deployment review failed with HTTP {exc.code}: {detail}\n"
    return extract_response_text(json.loads(body.decode("utf-8")))


def toml_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


DEPLOY_INSTRUCTIONS = (
    "You are VeloxServer's deployment assistant. Review a generated deployment plan, "
    "spot missing production steps, and suggest safe improvements. Do not invent secrets. "
    "Do not suggest destructive commands."
)
