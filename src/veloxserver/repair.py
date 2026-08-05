from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping


SENSITIVE_HEADER_NAMES = {
    "authorization",
    "cookie",
    "proxy-authorization",
    "set-cookie",
    "x-api-key",
    "x-openai-api-key",
}
SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*=\s*['\"][^'\"]+['\"]"),
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),
]


@dataclass(frozen=True)
class ErrorRepairSettings:
    enabled: bool = False
    project_path: Path | None = None
    log_path: Path | None = None
    suggestions_path: Path | None = None
    apply_patches: bool = False
    model: str = "gpt-4.1-mini"
    api_key_env: str = "OPENAI_API_KEY"
    api_key: str | None = None
    base_url: str = "https://api.openai.com/v1"
    timeout: float = 30.0
    min_status: int = 500
    include_statuses: tuple[int, ...] = field(default_factory=tuple)
    context_files: tuple[Path, ...] = field(default_factory=tuple)
    max_file_bytes: int = 32 * 1024
    max_context_bytes: int = 96 * 1024
    cooldown_seconds: float = 60.0
    max_output_tokens: int = 1600

    def resolved_project_path(self) -> Path:
        return (self.project_path or Path.cwd()).resolve()

    def resolved_log_path(self) -> Path:
        if self.log_path is not None:
            return self.log_path
        return self.resolved_project_path() / ".veloxserver" / "ai-repair.log"

    def resolved_suggestions_path(self) -> Path:
        if self.suggestions_path is not None:
            return self.suggestions_path
        return self.resolved_project_path() / ".veloxserver" / "repair-suggestions"


@dataclass(frozen=True)
class ErrorRepairEvent:
    status: int
    message: str
    method: str = "-"
    target: str = "-"
    protocol: str = "HTTP/1.1"
    peer: str = "-"
    headers: Mapping[str, str] = field(default_factory=dict)
    route_kind: str = "-"
    exception: str | None = None
    traceback: str | None = None
    config_path: Path | None = None
    created_at: float = field(default_factory=time.time)

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "ts": self.created_at,
            "status": self.status,
            "message": self.message,
            "method": self.method,
            "target": self.target,
            "protocol": self.protocol,
            "peer": self.peer,
            "headers": dict(self.headers),
            "route_kind": self.route_kind,
        }
        if self.exception:
            payload["exception"] = self.exception
        if self.traceback:
            payload["traceback"] = self.traceback[-8000:]
        if self.config_path:
            payload["config_path"] = str(self.config_path)
        return payload


@dataclass(frozen=True)
class RepairResult:
    record_path: Path
    applied_files: tuple[Path, ...]
    parsed: dict[str, object]


class AIErrorRepairer:
    def __init__(
        self,
        settings: ErrorRepairSettings,
        responder: Callable[[str], str] | None = None,
    ) -> None:
        self.settings = settings
        self.responder = responder
        self._last_seen: dict[str, float] = {}

    def should_handle(self, status: int) -> bool:
        if not self.settings.enabled:
            return False
        if self.settings.include_statuses:
            return status in self.settings.include_statuses
        return status >= self.settings.min_status

    async def handle(self, event: ErrorRepairEvent) -> RepairResult | None:
        if not self.should_handle(event.status):
            return None
        fingerprint = repair_fingerprint(event)
        now = time.time()
        if now - self._last_seen.get(fingerprint, 0.0) < self.settings.cooldown_seconds:
            return None
        self._last_seen[fingerprint] = now

        try:
            prompt = self._build_prompt(event)
            if self.responder is not None:
                response_text = await asyncio.to_thread(self.responder, prompt)
            else:
                response_text = await asyncio.to_thread(self._call_openai, prompt)
            parsed = parse_repair_json(response_text)
            applied = self._apply_files(parsed) if self.settings.apply_patches else []
            record_path = self._write_record(event, fingerprint, response_text, parsed, applied)
            return RepairResult(record_path=record_path, applied_files=tuple(applied), parsed=parsed)
        except Exception as exc:
            self._write_failure(event, fingerprint, exc)
            return None

    def _build_prompt(self, event: ErrorRepairEvent) -> str:
        context = self._collect_context(event)
        payload = {
            "task": "diagnose_and_prepare_safe_repair",
            "rules": [
                "Return JSON only.",
                "Do not include secrets.",
                "Prefer the smallest safe fix.",
                "Only propose file changes under project_path.",
                "Use files[].content only when you can provide a complete replacement file.",
                "Use operator_steps for commands or manual deployment steps.",
            ],
            "expected_json_shape": {
                "summary": "short diagnosis",
                "probable_cause": "why this happened",
                "risk": "low|medium|high",
                "files": [
                    {
                        "path": "relative/path.py",
                        "action": "create|update",
                        "content": "complete file content",
                        "notes": "why",
                    }
                ],
                "patch": "optional unified diff",
                "operator_steps": ["safe command or manual step"],
                "tests": ["tests to run"],
            },
            "event": event.to_payload(),
            "context": context,
        }
        return json.dumps(payload, indent=2)

    def _collect_context(self, event: ErrorRepairEvent) -> dict[str, object]:
        project = self.settings.resolved_project_path()
        files = {}
        selected = list(self.settings.context_files)
        if event.config_path is not None:
            selected.append(event.config_path)
        if not selected:
            selected.extend(default_context_files(project))

        budget = self.settings.max_context_bytes
        for raw_path in selected:
            path = resolve_context_path(project, raw_path)
            if path is None or not path.exists() or not path.is_file():
                continue
            if path.stat().st_size > self.settings.max_file_bytes:
                continue
            rel = str(path.relative_to(project)).replace("\\", "/")
            text = redact_text(path.read_text(encoding="utf-8", errors="replace"))
            if len(text.encode("utf-8")) > budget:
                break
            files[rel] = text
            budget -= len(text.encode("utf-8"))

        return {
            "project_path": str(project),
            "tree": project_tree(project),
            "files": files,
        }

    def _call_openai(self, prompt: str) -> str:
        api_key = self.settings.api_key or os.environ.get(self.settings.api_key_env, "")
        if not api_key:
            raise RuntimeError(f"{self.settings.api_key_env} is not set")
        sdk_text = self._call_openai_sdk(prompt, api_key)
        if sdk_text is not None:
            return sdk_text
        return self._call_openai_rest(prompt, api_key)

    def _call_openai_sdk(self, prompt: str, api_key: str) -> str | None:
        try:
            from openai import OpenAI  # type: ignore[import-not-found]
        except ImportError:
            return None

        kwargs: dict[str, object] = {"api_key": api_key}
        if self.settings.base_url != "https://api.openai.com/v1":
            kwargs["base_url"] = self.settings.base_url
        client = OpenAI(**kwargs)
        response = client.responses.create(
            model=self.settings.model,
            instructions=REPAIR_INSTRUCTIONS,
            input=prompt,
            max_output_tokens=self.settings.max_output_tokens,
        )
        output_text = getattr(response, "output_text", None)
        if isinstance(output_text, str) and output_text:
            return output_text
        if hasattr(response, "model_dump"):
            return extract_response_text(response.model_dump())
        return str(response)

    def _call_openai_rest(self, prompt: str, api_key: str) -> str:
        payload = {
            "model": self.settings.model,
            "instructions": REPAIR_INSTRUCTIONS,
            "input": prompt,
            "max_output_tokens": self.settings.max_output_tokens,
        }
        request = urllib.request.Request(
            f"{self.settings.base_url.rstrip('/')}/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "veloxserver-ai-repair",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.settings.timeout) as response:
                body = response.read(4 * 1024 * 1024)
        except urllib.error.HTTPError as exc:
            detail = exc.read(64 * 1024).decode("utf-8", errors="replace")
            raise RuntimeError(f"OpenAI repair request failed with HTTP {exc.code}: {detail}") from exc
        return extract_response_text(json.loads(body.decode("utf-8")))

    def _apply_files(self, parsed: dict[str, object]) -> list[Path]:
        project = self.settings.resolved_project_path()
        backup_root = project / ".veloxserver" / "backups" / str(int(time.time()))
        applied: list[Path] = []
        raw_files = parsed.get("files", [])
        if not isinstance(raw_files, list):
            return applied
        for item in raw_files:
            if not isinstance(item, dict):
                continue
            if str(item.get("action", "update")) not in {"create", "update"}:
                continue
            content = item.get("content")
            if not isinstance(content, str):
                continue
            target = resolve_project_file(project, str(item.get("path", "")))
            if target is None:
                continue
            if target.exists() and target.is_file():
                backup = backup_root / target.relative_to(project)
                backup.parent.mkdir(parents=True, exist_ok=True)
                backup.write_bytes(target.read_bytes())
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            applied.append(target)
        return applied

    def _write_record(
        self,
        event: ErrorRepairEvent,
        fingerprint: str,
        response_text: str,
        parsed: dict[str, object],
        applied: list[Path],
    ) -> Path:
        project = self.settings.resolved_project_path()
        suggestions = self.settings.resolved_suggestions_path()
        suggestions.mkdir(parents=True, exist_ok=True)
        record_path = suggestions / f"repair-{int(time.time())}-{fingerprint[:12]}.json"
        record = {
            "type": "ai_error_repair",
            "fingerprint": fingerprint,
            "event": event.to_payload(),
            "openai_model": self.settings.model,
            "auto_apply": self.settings.apply_patches,
            "applied_files": [str(path.relative_to(project)).replace("\\", "/") for path in applied],
            "parsed": parsed,
            "raw_response": response_text,
        }
        record_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        patch = parsed.get("patch")
        if isinstance(patch, str) and patch.strip():
            record_path.with_suffix(".patch").write_text(patch, encoding="utf-8")
        append_json_line(self.settings.resolved_log_path(), record)
        return record_path

    def _write_failure(self, event: ErrorRepairEvent, fingerprint: str, exc: Exception) -> None:
        record = {
            "type": "ai_error_repair_failure",
            "fingerprint": fingerprint,
            "event": event.to_payload(),
            "error": f"{type(exc).__name__}: {exc}",
        }
        append_json_line(self.settings.resolved_log_path(), record)


REPAIR_INSTRUCTIONS = (
    "You are VeloxServer's deployment repair assistant. Diagnose HTTP/server/deployment errors, "
    "explain the probable cause, and prepare a minimal safe fix. Return valid JSON only. "
    "Never reveal or invent secrets. Do not propose destructive commands. Prefer config and "
    "small application fixes over broad rewrites."
)


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    redacted = {}
    for name, value in headers.items():
        lower = name.lower()
        redacted[name] = "[redacted]" if lower in SENSITIVE_HEADER_NAMES else value
    return redacted


def redact_text(text: str) -> str:
    result = text
    for pattern in SECRET_PATTERNS:
        result = pattern.sub(lambda match: match.group(0).split("=", 1)[0] + '="[redacted]"' if "=" in match.group(0) else "[redacted]", result)
    return result


def parse_repair_json(text: str) -> dict[str, object]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            try:
                parsed = json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                parsed = {"summary": cleaned, "files": [], "operator_steps": []}
        else:
            parsed = {"summary": cleaned, "files": [], "operator_steps": []}
    if isinstance(parsed, dict):
        parsed.setdefault("files", [])
        parsed.setdefault("operator_steps", [])
        return parsed
    return {"summary": cleaned, "files": [], "operator_steps": []}


def extract_response_text(payload: Mapping[str, object]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct:
        return direct
    parts: list[str] = []
    output = payload.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        text = part.get("text")
                        if isinstance(text, str):
                            parts.append(text)
    if parts:
        return "\n".join(parts)
    return json.dumps(payload)


def repair_fingerprint(event: ErrorRepairEvent) -> str:
    payload = f"{event.status}:{event.message}:{event.method}:{event.target}:{event.exception or ''}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def project_tree(project: Path, limit: int = 250) -> list[str]:
    if not project.exists() or not project.is_dir():
        return []
    ignored = {".git", ".venv", "__pycache__", ".pytest_cache", "node_modules", "dist", "build"}
    result: list[str] = []
    for path in project.rglob("*"):
        if len(result) >= limit:
            break
        rel_parts = path.relative_to(project).parts
        if any(part in ignored for part in rel_parts):
            continue
        if path.is_file():
            result.append(str(path.relative_to(project)).replace("\\", "/"))
    return result


def default_context_files(project: Path) -> list[Path]:
    names = [
        "veloxserver.toml",
        "pyproject.toml",
        "requirements.txt",
        "gunicorn.conf.py",
        "manage.py",
        "wsgi.py",
        "asgi.py",
        "app.py",
        "main.py",
        "README.md",
    ]
    return [project / name for name in names]


def resolve_context_path(project: Path, raw_path: Path) -> Path | None:
    path = raw_path if raw_path.is_absolute() else project / raw_path
    try:
        resolved = path.resolve()
        resolved.relative_to(project)
        return resolved
    except ValueError:
        return None


def resolve_project_file(project: Path, value: str) -> Path | None:
    if not value or "\x00" in value:
        return None
    path = Path(value)
    if path.is_absolute():
        return None
    try:
        resolved = (project / path).resolve()
        resolved.relative_to(project)
        return resolved
    except ValueError:
        return None


def append_json_line(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(payload, separators=(",", ":")))
        file.write("\n")
