from __future__ import annotations

import argparse
import json
import socket
import ssl
import time
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class TlsProbe:
    host: str
    port: int
    server_name: str
    protocol: str
    cipher: str
    alpn: str | None
    reused: bool
    seconds: float


def probe(host: str, port: int, server_name: str, insecure: bool, alpn: list[str]) -> TlsProbe:
    context = ssl.create_default_context()
    context.set_alpn_protocols(alpn)
    if insecure:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    started = time.perf_counter()
    with socket.create_connection((host, port), timeout=10.0) as raw:
        with context.wrap_socket(raw, server_hostname=server_name) as sock:
            cipher = sock.cipher()
            return TlsProbe(
                host=host,
                port=port,
                server_name=server_name,
                protocol=sock.version() or "",
                cipher=cipher[0] if cipher else "",
                alpn=sock.selected_alpn_protocol(),
                reused=getattr(sock, "session_reused", False),
                seconds=time.perf_counter() - started,
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe VeloxServer TLS protocol, cipher, ALPN, and session reuse.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--server-name", default="localhost")
    parser.add_argument("--insecure", action="store_true")
    parser.add_argument("--alpn", default="h2,http/1.1")
    args = parser.parse_args()
    result = probe(args.host, args.port, args.server_name, args.insecure, [item.strip() for item in args.alpn.split(",") if item.strip()])
    print(json.dumps(asdict(result), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
