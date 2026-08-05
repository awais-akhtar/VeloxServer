from __future__ import annotations

import argparse
import asyncio
import statistics
import time
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Target:
    name: str
    url: str
    host: str
    port: int
    path: str


@dataclass(frozen=True)
class Result:
    name: str
    requests: int
    ok: int
    failed: int
    seconds: float
    latencies_ms: list[float]

    @property
    def rps(self) -> float:
        return self.requests / self.seconds if self.seconds else 0.0


def parse_target(value: str) -> Target:
    if "=" not in value:
        raise argparse.ArgumentTypeError("target must look like name=http://host:port/path")

    name, url = value.split("=", 1)
    parsed = urlsplit(url)
    if parsed.scheme != "http":
        raise argparse.ArgumentTypeError("only http:// URLs are supported")
    if not parsed.hostname:
        raise argparse.ArgumentTypeError("target URL must include a host")

    port = parsed.port or 80
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    return Target(name=name, url=url, host=parsed.hostname, port=port, path=path)


async def request_once(target: Target, timeout: float) -> tuple[bool, float]:
    started = time.perf_counter()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(target.host, target.port),
            timeout=timeout,
        )
        request = (
            f"GET {target.path} HTTP/1.1\r\n"
            f"Host: {target.host}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("ascii")
        writer.write(request)
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=timeout)
        status_ok = head.startswith(b"HTTP/1.1 200 ") or head.startswith(b"HTTP/1.0 200 ")
        while await asyncio.wait_for(reader.read(65536), timeout=timeout):
            pass
        writer.close()
        await writer.wait_closed()
        return status_ok, (time.perf_counter() - started) * 1000
    except Exception:
        return False, (time.perf_counter() - started) * 1000


async def run_target(target: Target, requests: int, concurrency: int, timeout: float) -> Result:
    latencies: list[float] = []
    ok = 0
    failed = 0
    queue = asyncio.Queue[int]()
    for item in range(requests):
        queue.put_nowait(item)

    async def worker() -> None:
        nonlocal ok, failed
        while True:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            success, latency = await request_once(target, timeout)
            latencies.append(latency)
            if success:
                ok += 1
            else:
                failed += 1
            queue.task_done()

    started = time.perf_counter()
    workers = [asyncio.create_task(worker()) for _ in range(concurrency)]
    await asyncio.gather(*workers)
    seconds = time.perf_counter() - started
    return Result(target.name, requests, ok, failed, seconds, latencies)


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = round((len(ordered) - 1) * pct)
    return ordered[index]


def print_results(results: list[Result]) -> None:
    print()
    print("target       requests  ok       failed   rps       avg_ms   p50_ms   p95_ms")
    print("-----------  --------  -------  -------  --------  -------  -------  -------")
    for result in results:
        avg = statistics.fmean(result.latencies_ms) if result.latencies_ms else 0.0
        p50 = percentile(result.latencies_ms, 0.50)
        p95 = percentile(result.latencies_ms, 0.95)
        print(
            f"{result.name[:11]:<11}  "
            f"{result.requests:>8}  "
            f"{result.ok:>7}  "
            f"{result.failed:>7}  "
            f"{result.rps:>8.1f}  "
            f"{avg:>7.2f}  "
            f"{p50:>7.2f}  "
            f"{p95:>7.2f}"
        )


async def amain() -> int:
    parser = argparse.ArgumentParser(description="Benchmark static HTTP targets.")
    parser.add_argument(
        "--target",
        action="append",
        type=parse_target,
        required=True,
        help="Benchmark target in the form name=http://host:port/path.",
    )
    parser.add_argument("--requests", type=int, default=1000)
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args()

    results = []
    for target in args.target:
        print(f"benchmarking {target.name}: {target.url}")
        results.append(
            await run_target(
                target,
                requests=args.requests,
                concurrency=args.concurrency,
                timeout=args.timeout,
            )
        )
    print_results(results)
    return 0


def main() -> int:
    return asyncio.run(amain())


if __name__ == "__main__":
    raise SystemExit(main())
