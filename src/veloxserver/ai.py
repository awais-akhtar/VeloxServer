from __future__ import annotations

import asyncio
import html
import importlib.util
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class AIServiceError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class AIMessage:
    role: str
    content: str


@dataclass(frozen=True)
class AICompletion:
    text: str
    prompt_tokens: int
    completion_tokens: int


class AIModelManager:
    def __init__(self) -> None:
        self.runners: dict[str, BaseAIRunner] = {}
        self.lock = asyncio.Lock()

    async def complete_chat(
        self,
        route: Any,
        messages: list[AIMessage],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> AICompletion:
        runner = await self.runner_for(route)
        return await asyncio.to_thread(
            runner.complete_chat,
            messages,
            max_tokens or route.ai_max_tokens,
            route.ai_temperature if temperature is None else temperature,
        )

    async def runner_for(self, route: Any) -> "BaseAIRunner":
        key = route.path
        runner = self.runners.get(key)
        if runner is not None:
            return runner
        async with self.lock:
            runner = self.runners.get(key)
            if runner is None:
                runner = build_runner(route)
                self.runners[key] = runner
            return runner


class BaseAIRunner:
    def __init__(self, route: Any) -> None:
        self.route = route

    def complete_chat(self, messages: list[AIMessage], max_tokens: int, temperature: float) -> AICompletion:
        raise NotImplementedError

    def prompt_from_messages(self, messages: list[AIMessage]) -> str:
        parts = []
        if self.route.ai_system_prompt:
            parts.append(f"System: {self.route.ai_system_prompt}")
        for message in messages:
            role = message.role.capitalize()
            parts.append(f"{role}: {message.content}")
        parts.append("Assistant:")
        return "\n".join(parts)


class EchoAIRunner(BaseAIRunner):
    def complete_chat(self, messages: list[AIMessage], max_tokens: int, temperature: float) -> AICompletion:
        del temperature
        user_text = next((message.content for message in reversed(messages) if message.role == "user"), "")
        system = self.route.ai_system_prompt.strip()
        prefix = f"{system}\n" if system else ""
        text = (prefix + user_text).strip() or "Ready."
        words = text.split()
        if max_tokens > 0:
            words = words[:max_tokens]
            text = " ".join(words)
        return AICompletion(text=text, prompt_tokens=count_tokens(messages), completion_tokens=len(words))


class TransformersAIRunner(BaseAIRunner):
    def __init__(self, route: Any) -> None:
        super().__init__(route)
        if importlib.util.find_spec("transformers") is None:
            raise AIServiceError(503, "transformers backend requires the 'transformers' package")
        from transformers import pipeline

        model_path = model_path_or_name(route)
        self.pipeline = pipeline("text-generation", model=model_path)

    def complete_chat(self, messages: list[AIMessage], max_tokens: int, temperature: float) -> AICompletion:
        prompt = self.prompt_from_messages(messages)
        output = self.pipeline(
            prompt,
            max_new_tokens=max_tokens,
            temperature=temperature,
            do_sample=temperature > 0,
            return_full_text=False,
        )
        text = str(output[0].get("generated_text", "")).strip()
        return AICompletion(text=text, prompt_tokens=count_prompt_tokens(prompt), completion_tokens=count_prompt_tokens(text))


class LlamaCppAIRunner(BaseAIRunner):
    def __init__(self, route: Any) -> None:
        super().__init__(route)
        if importlib.util.find_spec("llama_cpp") is None:
            raise AIServiceError(503, "llama_cpp backend requires the 'llama-cpp-python' package")
        from llama_cpp import Llama

        if route.ai_model_path is None:
            raise AIServiceError(500, "llama_cpp backend requires ai_model_path")
        self.model = Llama(model_path=str(route.ai_model_path), n_ctx=route.ai_context_window)

    def complete_chat(self, messages: list[AIMessage], max_tokens: int, temperature: float) -> AICompletion:
        llama_messages = []
        if self.route.ai_system_prompt:
            llama_messages.append({"role": "system", "content": self.route.ai_system_prompt})
        llama_messages.extend({"role": message.role, "content": message.content} for message in messages)
        if hasattr(self.model, "create_chat_completion"):
            output = self.model.create_chat_completion(
                messages=llama_messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            text = str(output["choices"][0]["message"]["content"]).strip()
        else:
            output = self.model(
                self.prompt_from_messages(messages),
                max_tokens=max_tokens,
                temperature=temperature,
            )
            text = str(output["choices"][0]["text"]).strip()
        return AICompletion(text=text, prompt_tokens=count_tokens(messages), completion_tokens=count_prompt_tokens(text))


def build_runner(route: Any) -> BaseAIRunner:
    backend = select_backend(route)
    if backend == "echo":
        return EchoAIRunner(route)
    if backend == "transformers":
        return TransformersAIRunner(route)
    if backend == "llama_cpp":
        return LlamaCppAIRunner(route)
    raise AIServiceError(500, f"unsupported AI backend: {backend}")


def select_backend(route: Any) -> str:
    backend = route.ai_backend.lower()
    if backend != "auto":
        return backend
    if route.ai_model_path is None:
        return "echo"
    suffix = route.ai_model_path.suffix.lower()
    if suffix in {".gguf", ".ggml", ".bin"}:
        return "llama_cpp"
    return "transformers"


def model_path_or_name(route: Any) -> str:
    if route.ai_model_path is not None:
        return str(route.ai_model_path)
    return route.ai_model_name


def parse_chat_messages(payload: dict[str, Any]) -> list[AIMessage]:
    raw_messages = payload.get("messages")
    if not isinstance(raw_messages, list):
        prompt = payload.get("prompt")
        if isinstance(prompt, str):
            return [AIMessage("user", prompt)]
        raise AIServiceError(400, "AI request requires messages or prompt")
    messages = []
    for item in raw_messages:
        if not isinstance(item, dict):
            raise AIServiceError(400, "each message must be an object")
        role = str(item.get("role", "user")).lower()
        if role not in {"system", "user", "assistant", "tool"}:
            raise AIServiceError(400, f"unsupported message role: {role}")
        content = item.get("content", "")
        if isinstance(content, list):
            content = " ".join(str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in content)
        messages.append(AIMessage(role, str(content)))
    return messages


def chat_completion_payload(route: Any, completion: AICompletion) -> bytes:
    created = int(time.time())
    payload = {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": created,
        "model": route.ai_model_name,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": completion.text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": completion.prompt_tokens,
            "completion_tokens": completion.completion_tokens,
            "total_tokens": completion.prompt_tokens + completion.completion_tokens,
        },
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def text_completion_payload(route: Any, completion: AICompletion) -> bytes:
    payload = {
        "id": f"cmpl-{uuid.uuid4().hex}",
        "object": "text_completion",
        "created": int(time.time()),
        "model": route.ai_model_name,
        "choices": [{"index": 0, "text": completion.text, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": completion.prompt_tokens,
            "completion_tokens": completion.completion_tokens,
            "total_tokens": completion.prompt_tokens + completion.completion_tokens,
        },
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def sse_chat_payload(route: Any, completion: AICompletion) -> bytes:
    chunk = {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": route.ai_model_name,
        "choices": [{"index": 0, "delta": {"content": completion.text}, "finish_reason": None}],
    }
    done = {
        **chunk,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    return (
        f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n"
        f"data: {json.dumps(done, separators=(',', ':'))}\n\n"
        "data: [DONE]\n\n"
    ).encode("utf-8")


def models_payload(route: Any) -> bytes:
    payload = {
        "object": "list",
        "data": [
            {
                "id": route.ai_model_name,
                "object": "model",
                "created": 0,
                "owned_by": "veloxserver",
                "backend": select_backend(route),
                "path": str(route.ai_model_path) if route.ai_model_path is not None else "",
            }
        ],
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def health_payload(route: Any) -> bytes:
    payload = {
        "status": "ok",
        "model": route.ai_model_name,
        "backend": select_backend(route),
        "model_path": str(route.ai_model_path) if route.ai_model_path is not None else "",
        "api": route.ai_api_enabled,
        "chat": route.ai_chat_enabled,
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def error_payload(message: str) -> bytes:
    return json.dumps({"error": {"message": message}}, separators=(",", ":")).encode("utf-8")


def render_chat_page(route: Any) -> bytes:
    title = html.escape(route.ai_model_name)
    base = html.escape(route.path.rstrip("/") or "/")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{ color-scheme: light dark; font-family: Inter, Segoe UI, Arial, sans-serif; }}
body {{ margin: 0; min-height: 100vh; background: #f6f7f4; color: #17201d; }}
main {{ max-width: 880px; margin: 0 auto; padding: 28px 18px; }}
h1 {{ font-size: 24px; margin: 0 0 18px; font-weight: 700; }}
#log {{ min-height: 58vh; display: grid; gap: 10px; align-content: start; }}
.msg {{ padding: 12px 14px; border: 1px solid #d8ded6; border-radius: 8px; background: #fff; white-space: pre-wrap; line-height: 1.45; }}
.user {{ background: #e8f1ff; border-color: #c6dcff; }}
.assistant {{ background: #ffffff; }}
form {{ display: grid; grid-template-columns: 1fr auto; gap: 10px; margin-top: 16px; }}
textarea {{ min-height: 54px; resize: vertical; border: 1px solid #b9c4bd; border-radius: 8px; padding: 12px; font: inherit; }}
button {{ border: 0; border-radius: 8px; padding: 0 18px; background: #195b4a; color: white; font: inherit; font-weight: 700; }}
@media (prefers-color-scheme: dark) {{
  body {{ background: #101513; color: #eef5ef; }}
  .msg {{ background: #18211e; border-color: #2c3834; }}
  .user {{ background: #142a3b; border-color: #214966; }}
  textarea {{ background: #121a17; color: #eef5ef; border-color: #35433e; }}
}}
</style>
</head>
<body>
<main>
<h1>{title}</h1>
<section id="log"></section>
<form id="chat">
<textarea id="input" autocomplete="off" autofocus></textarea>
<button>Send</button>
</form>
</main>
<script>
const endpoint = "{base}/v1/chat/completions";
const messages = [];
const log = document.querySelector("#log");
function add(role, content) {{
  const node = document.createElement("div");
  node.className = "msg " + role;
  node.textContent = content;
  log.appendChild(node);
  node.scrollIntoView({{block: "end"}});
}}
document.querySelector("#chat").addEventListener("submit", async (event) => {{
  event.preventDefault();
  const input = document.querySelector("#input");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  messages.push({{role: "user", content: text}});
  add("user", text);
  const response = await fetch(endpoint, {{
    method: "POST",
    headers: {{"content-type": "application/json"}},
    body: JSON.stringify({{messages}})
  }});
  const data = await response.json();
  const reply = data.choices?.[0]?.message?.content || data.error?.message || "";
  messages.push({{role: "assistant", content: reply}});
  add("assistant", reply);
}});
</script>
</body>
</html>
""".encode("utf-8")


def count_tokens(messages: list[AIMessage]) -> int:
    return sum(count_prompt_tokens(message.content) for message in messages)


def count_prompt_tokens(text: str) -> int:
    return len(text.split())
