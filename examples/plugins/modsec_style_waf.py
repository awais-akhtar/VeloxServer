from __future__ import annotations

import re


RULES = [
    (r"(?i)(union\s+select|sleep\(|benchmark\()", 403, "SQL injection pattern"),
    (r"(?i)(<script|javascript:)", 403, "XSS pattern"),
    (r"(?i)(\.\./|\%2e\%2e)", 403, "Traversal pattern"),
]


def on_waf_request(request):
    target = request.target
    body = request.body.decode("utf-8", errors="ignore") if getattr(request, "body", b"") else ""
    headers = " ".join(f"{name}:{value}" for name, value in request.headers.items())
    haystack = f"{target}\n{headers}\n{body}"
    for pattern, status, message in RULES:
        if re.search(pattern, haystack):
            return {"allowed": False, "status": status, "message": message}
    return True
