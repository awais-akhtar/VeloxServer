from __future__ import annotations


def on_request(request):
    blocked = request.headers.get("x-block-me", "").lower() == "yes"
    if blocked:
        return {
            "allowed": False,
            "status": 403,
            "message": "Plugin blocked",
        }
    return True
