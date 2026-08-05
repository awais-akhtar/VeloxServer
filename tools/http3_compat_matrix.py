from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class CompatResult:
    client: str
    command: list[str]
    available: bool
    ok: bool
    seconds: float
    output: str


def run_client(name: str, command: list[str], timeout: float) -> CompatResult:
    started = time.perf_counter()
    binary = shutil.which(command[0])
    if binary is None:
        return CompatResult(name, command, False, False, 0.0, "client binary not found")
    try:
        completed = subprocess.run(
            [binary, *command[1:]],
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return CompatResult(name, command, True, False, time.perf_counter() - started, f"timeout: {exc}")
    output = (completed.stdout + completed.stderr).strip()
    return CompatResult(name, command, True, completed.returncode == 0, time.perf_counter() - started, output[-4000:])


def main() -> int:
    parser = argparse.ArgumentParser(description="Run an HTTP/3 client compatibility smoke matrix.")
    parser.add_argument("url", help="HTTPS URL served by VeloxServer with HTTP/3 enabled.")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--insecure", action="store_true", help="Pass insecure TLS flags to supported clients.")
    args = parser.parse_args()

    curl = ["curl", "--http3-only", "--fail", "--silent", "--show-error", args.url]
    if args.insecure:
        curl.insert(1, "--insecure")

    # Browsers are intentionally not launched headlessly here: their HTTP/3 stacks
    # differ by platform and release channel. Record manual browser runs in the
    # same JSON shape next to this output.
    results = [
        run_client("curl-http3-only", curl, args.timeout),
    ]
    print(json.dumps([asdict(result) for result in results], indent=2))
    return 0 if all(result.ok or not result.available for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
