from __future__ import annotations

import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from veloxserver.native import load_native_core


METHODS = ["GET", "POST", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS"]


def random_head() -> bytes:
    method = random.choice(METHODS)
    target = "/" + "".join(random.choice("abcxyz012345/%?=&") for _ in range(random.randint(0, 40)))
    lines = [f"{method} {target} HTTP/1.1", "Host: fuzz.local"]
    for index in range(random.randint(0, 12)):
        name = f"X-Fuzz-{index}"
        value = "".join(random.choice("abcxyz012345 -_") for _ in range(random.randint(0, 50)))
        lines.append(f"{name}: {value}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8", errors="ignore")


def main() -> int:
    iterations = int(sys.argv[1]) if len(sys.argv) > 1 else 10000
    status = load_native_core("rust")
    if not status.available or status.core is None:
        print("native core unavailable; skipped native ABI fuzz")
        return 0
    for _ in range(iterations):
        status.core.parse_request_head(random_head())
        status.core.build_cache_key("$scheme $method $host $uri $remote_addr", "GET", "http", "fuzz.local", "/", "127.0.0.1")
    print(f"fuzzed {iterations} native ABI inputs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
