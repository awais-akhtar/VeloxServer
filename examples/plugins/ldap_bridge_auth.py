from __future__ import annotations

import base64
import os
import urllib.error
import urllib.request


def on_auth_request(request):
    # Bridge pattern for LDAP/OIDC systems: run a hardened local auth gateway
    # that speaks LDAP/OIDC, and let this plugin call it with the user's basic
    # credentials. This avoids baking LDAP bind code into the hot server path.
    gateway = os.environ.get("VELOX_LDAP_AUTH_GATEWAY")
    if not gateway:
        return True
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        return {"allowed": False, "status": 401, "message": "Unauthorized"}
    try:
        raw = base64.b64decode(header.split(None, 1)[1]).decode("utf-8")
    except Exception:
        return {"allowed": False, "status": 401, "message": "Unauthorized"}
    body = raw.encode("utf-8")
    auth_request = urllib.request.Request(gateway, data=body, method="POST")
    try:
        with urllib.request.urlopen(auth_request, timeout=2.0) as response:
            return 200 <= response.status < 300
    except urllib.error.HTTPError as exc:
        return {"allowed": False, "status": exc.code, "message": "Unauthorized"}
    except Exception:
        return {"allowed": False, "status": 503, "message": "Auth unavailable"}
