from __future__ import annotations

import argparse
import asyncio
import json
import ssl
import time
from dataclasses import asdict, dataclass

from h2.connection import H2Connection
from h2.events import DataReceived, ResponseReceived, StreamEnded, WindowUpdated


@dataclass(frozen=True)
class H2StressResult:
    url: str
    streams: int
    body_bytes: int
    ok: bool
    seconds: float
    statuses: dict[str, int]
    errors: list[str]


async def run_h2_stress(host: str, port: int, path: str, streams: int, timeout: float, insecure: bool) -> H2StressResult:
    context = ssl.create_default_context()
    context.set_alpn_protocols(["h2"])
    if insecure:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    started = time.perf_counter()
    statuses: dict[str, int] = {}
    bodies: dict[int, int] = {}
    ended: set[int] = set()
    errors: list[str] = []

    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port, ssl=context, server_hostname=host),
        timeout=timeout,
    )
    try:
        conn = H2Connection()
        conn.initiate_connection()
        stream_ids = []
        for _index in range(streams):
            stream_id = conn.get_next_available_stream_id()
            stream_ids.append(stream_id)
            conn.send_headers(
                stream_id,
                [
                    (":method", "GET"),
                    (":scheme", "https"),
                    (":authority", host),
                    (":path", path),
                ],
                end_stream=True,
            )
        writer.write(conn.data_to_send())
        await writer.drain()

        deadline = time.perf_counter() + timeout
        while len(ended) < len(stream_ids) and time.perf_counter() < deadline:
            data = await asyncio.wait_for(reader.read(65535), timeout=max(0.1, deadline - time.perf_counter()))
            if not data:
                break
            for event in conn.receive_data(data):
                if isinstance(event, ResponseReceived):
                    headers = dict(event.headers)
                    statuses[str(headers.get(":status", "0"))] = statuses.get(str(headers.get(":status", "0")), 0) + 1
                elif isinstance(event, DataReceived):
                    bodies[event.stream_id] = bodies.get(event.stream_id, 0) + len(event.data)
                    conn.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                elif isinstance(event, StreamEnded):
                    ended.add(event.stream_id)
                elif isinstance(event, WindowUpdated):
                    pass
            writer.write(conn.data_to_send())
            await writer.drain()
        if len(ended) != len(stream_ids):
            errors.append(f"only {len(ended)} of {len(stream_ids)} streams ended")
    finally:
        writer.close()
        await writer.wait_closed()

    return H2StressResult(
        url=f"https://{host}:{port}{path}",
        streams=streams,
        body_bytes=sum(bodies.values()),
        ok=not errors and len(ended) == streams,
        seconds=time.perf_counter() - started,
        statuses=statuses,
        errors=errors,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run HTTP/2 concurrency and flow-control stress probes.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--path", default="/")
    parser.add_argument("--streams", type=int, default=100)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--insecure", action="store_true")
    args = parser.parse_args()

    result = asyncio.run(run_h2_stress(args.host, args.port, args.path, args.streams, args.timeout, args.insecure))
    print(json.dumps(asdict(result), indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
