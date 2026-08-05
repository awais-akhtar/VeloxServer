from __future__ import annotations

import asyncio
from typing import Any

from .server import Request, h2_response_headers


async def start_http3_server(app: Any) -> Any:
    from aioquic.asyncio import serve
    from aioquic.asyncio.protocol import QuicConnectionProtocol
    from aioquic.h3.connection import H3_ALPN, H3Connection
    from aioquic.h3.events import DataReceived, HeadersReceived
    from aioquic.quic.configuration import QuicConfiguration
    from aioquic.quic.events import ProtocolNegotiated

    class VeloxH3Protocol(QuicConnectionProtocol):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.http: H3Connection | None = None
            self.streams: dict[int, dict[str, Any]] = {}

        def quic_event_received(self, event: Any) -> None:
            if isinstance(event, ProtocolNegotiated):
                self.http = H3Connection(self._quic)
            if self.http is None:
                return
            for h3_event in self.http.handle_event(event):
                if isinstance(h3_event, HeadersReceived):
                    self.streams[h3_event.stream_id] = {
                        "headers": h3_event.headers,
                        "body": bytearray(),
                    }
                    if h3_event.stream_ended:
                        asyncio.create_task(self._answer(h3_event.stream_id))
                elif isinstance(h3_event, DataReceived):
                    state = self.streams.setdefault(
                        h3_event.stream_id,
                        {"headers": [], "body": bytearray()},
                    )
                    state["body"].extend(h3_event.data)
                    if h3_event.stream_ended:
                        asyncio.create_task(self._answer(h3_event.stream_id))

        async def _answer(self, stream_id: int) -> None:
            if self.http is None:
                return
            state = self.streams.pop(stream_id, {"headers": [], "body": bytearray()})
            request = h3_request_from_state(state)
            try:
                response = await app._build_http2_response(request, ("http3", 0))
            except Exception:
                response = app._build_error_http_response(500, "Internal Server Error")
            headers = [
                (name.encode("ascii"), value.encode("latin-1"))
                for name, value in h2_response_headers(response.status, response.headers)
            ]
            self.http.send_headers(stream_id=stream_id, headers=headers, end_stream=False)
            self.http.send_data(stream_id=stream_id, data=b"" if request.method == "HEAD" else response.body, end_stream=True)
            self.transmit()

    configuration = QuicConfiguration(
        is_client=False,
        alpn_protocols=H3_ALPN,
    )
    configuration.load_cert_chain(
        certfile=str(app.config.tls_certfile),
        keyfile=str(app.config.tls_keyfile),
    )
    return await serve(
        app.config.host,
        app.config.http3_port or app.config.port,
        configuration=configuration,
        create_protocol=VeloxH3Protocol,
    )


def h3_request_from_state(state: dict[str, Any]) -> Request:
    pseudo: dict[str, str] = {}
    headers: dict[str, str] = {}
    for raw_name, raw_value in state.get("headers", []):
        name = raw_name.decode("ascii").lower() if isinstance(raw_name, bytes) else str(raw_name).lower()
        value = raw_value.decode("latin-1") if isinstance(raw_value, bytes) else str(raw_value)
        if name.startswith(":"):
            pseudo[name] = value
        else:
            headers[name] = value
    if ":authority" in pseudo and "host" not in headers:
        headers["host"] = pseudo[":authority"]
    return Request(
        method=pseudo.get(":method", "GET").upper(),
        target=pseudo.get(":path", "/"),
        version="HTTP/3",
        headers=headers,
        body=bytes(state.get("body", bytearray())),
    )
