# AI Model Routes

VeloxServer can expose a local model as a first-class route:

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

Backends:

- `echo`: built-in deterministic development backend
- `llama_cpp`: local GGUF/GGML-style model files through `llama-cpp-python`
- `transformers`: local Hugging Face model directories or model ids through `transformers`
- `auto`: chooses `llama_cpp` for `.gguf`, `.ggml`, or `.bin`; otherwise chooses `transformers`; falls back to `echo` when no model path is configured

Optional installs:

Linux, macOS, and Windows:

```bash
python -m pip install "veloxserver[ai-llama]"
python -m pip install "veloxserver[ai-transformers]"
```

Example API call on Linux and macOS:

```bash
curl -X POST http://127.0.0.1:8080/ai/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Explain this deployment"}]}'
```

Example API call on Windows PowerShell:

```powershell
curl.exe -X POST http://127.0.0.1:8080/ai/v1/chat/completions `
  -H "Content-Type: application/json" `
  -d "{\"messages\":[{\"role\":\"user\",\"content\":\"Explain this deployment\"}]}"
```

Endpoints under the route:

- `GET /ai/` web chat UI
- `GET /ai/health` route health and backend metadata
- `GET /ai/v1/models` OpenAI-style model list
- `POST /ai/v1/chat/completions` OpenAI-style chat completion
- `POST /ai/v1/completions` OpenAI-style text completion
- `POST /ai/chat` compact web-chat JSON endpoint

Chat request:

```json
{
  "messages": [
    {"role": "user", "content": "Explain this deployment"}
  ],
  "max_tokens": 256,
  "temperature": 0.7
}
```

Streaming requests with `"stream": true` return Server-Sent Events in the OpenAI chunk shape. The first implementation emits complete generated text as a chunk; token-by-token streaming can be added per backend.

AI routes still pass through the normal VeloxServer controls: host routing, TLS, basic/JWT/external auth, WAF hooks, rate limits, connection limits, logs, and metrics.
