from __future__ import annotations

import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from veloxserver.server import HEADER_END, HttpError, parse_request


SEEDS = [
    b"GET / HTTP/1.1\r\nHost: example\r\n\r\n",
    b"HEAD /x?y=1 HTTP/1.1\r\nHost: example\r\nConnection: close\r\n\r\n",
    b"POST / HTTP/1.1\r\nHost: example\r\nContent-Length: 3\r\n\r\nabc",
    b"GET /%2e%2e/secret HTTP/1.1\r\nHost: example\r\n\r\n",
]


def mutate(data: bytes) -> bytes:
    item = bytearray(data)
    for _ in range(random.randint(1, 12)):
        action = random.choice(("insert", "delete", "flip"))
        if action == "insert" or not item:
            index = random.randint(0, len(item))
            item[index:index] = bytes([random.randrange(256)])
        elif action == "delete":
            del item[random.randrange(len(item))]
        else:
            item[random.randrange(len(item))] ^= 1 << random.randrange(8)
    return bytes(item)


def run(iterations: int) -> None:
    for index in range(iterations):
        raw = mutate(random.choice(SEEDS + [os.urandom(random.randint(0, 128))]))
        if HEADER_END not in raw:
            raw += HEADER_END
        try:
            parse_request(raw.split(HEADER_END, 1)[0] + HEADER_END)
        except HttpError:
            continue
        except UnicodeDecodeError:
            continue
        except Exception as exc:
            raise AssertionError(f"unexpected parser crash at iteration {index}: {raw!r}") from exc


def main() -> int:
    iterations = int(sys.argv[1]) if len(sys.argv) > 1 else 10000
    run(iterations)
    print(f"fuzzed {iterations} parser inputs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
