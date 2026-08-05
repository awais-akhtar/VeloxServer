from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import gzip
import io
import json
import os
import socket
import tempfile
import time
import unittest
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from veloxserver.cli import run_doctor, run_validate
from veloxserver.config import load_config
from veloxserver.deploy_ai import AIDeploymentPlanner, DeploymentSettings, detect_project_profile
from veloxserver.native import load_native_core
from veloxserver.repair import AIErrorRepairer, ErrorRepairEvent, ErrorRepairSettings, parse_repair_json
from veloxserver.server import (
    HttpError,
    RouteConfig,
    ServerConfig,
    VeloxServer,
    is_h2_available,
    parse_upstream,
    parse_request,
    render_headers,
    resolve_target,
)
from veloxserver.shared import SharedZones
from veloxserver.stream import StreamProxyConfig, StreamProxyManager, select_udp_upstream, stream_upstream_key


async def send_request(app: VeloxServer, request: bytes) -> bytes:
    server = await asyncio.start_server(
        app._handle_client,
        host="127.0.0.1",
        port=0,
    )
    sock = server.sockets[0]
    host, port = sock.getsockname()[:2]

    try:
        reader, writer = await asyncio.open_connection(host, port)
        writer.write(request)
        await writer.drain()
        response = await reader.read()
        writer.close()
        await writer.wait_closed()
        return response
    finally:
        server.close()
        await server.wait_closed()


async def send_h2_requests(app: VeloxServer, targets: list[str]) -> dict[int, tuple[int, bytes]]:
    if not is_h2_available():
        raise unittest.SkipTest("h2 is not installed")

    from h2.config import H2Configuration
    from h2.connection import H2Connection
    from h2.events import DataReceived, ResponseReceived, StreamEnded

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await app._handle_http2_client(reader, writer, writer.get_extra_info("peername"))

    server = await asyncio.start_server(handle, host="127.0.0.1", port=0)
    host, port = server.sockets[0].getsockname()[:2]
    responses: dict[int, tuple[int, bytearray]] = {}
    ended: set[int] = set()

    try:
        reader, writer = await asyncio.open_connection(host, port)
        conn = H2Connection(config=H2Configuration(client_side=True, header_encoding="utf-8"))
        conn.initiate_connection()
        stream_ids = []
        for target in targets:
            stream_id = conn.get_next_available_stream_id()
            stream_ids.append(stream_id)
            conn.send_headers(
                stream_id,
                [
                    (":method", "GET"),
                    (":scheme", "http"),
                    (":authority", "test"),
                    (":path", target),
                ],
                end_stream=True,
            )
        writer.write(conn.data_to_send())
        await writer.drain()

        while set(stream_ids) != ended:
            data = await asyncio.wait_for(reader.read(65535), timeout=5)
            if not data:
                break
            for event in conn.receive_data(data):
                if isinstance(event, ResponseReceived):
                    status = int(dict(event.headers)[":status"])
                    responses[event.stream_id] = (status, bytearray())
                elif isinstance(event, DataReceived):
                    responses[event.stream_id][1].extend(event.data)
                    conn.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                elif isinstance(event, StreamEnded):
                    ended.add(event.stream_id)
            writer.write(conn.data_to_send())
            await writer.drain()

        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()

    return {stream_id: (status, bytes(body)) for stream_id, (status, body) in responses.items()}


def unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class RequestParsingTests(unittest.TestCase):
    def test_parse_get_request(self) -> None:
        request = parse_request(
            b"GET /index.html HTTP/1.1\r\nHost: example.test\r\nConnection: close\r\n\r\n"
        )

        self.assertEqual(request.method, "GET")
        self.assertEqual(request.target, "/index.html")
        self.assertEqual(request.version, "HTTP/1.1")
        self.assertEqual(request.headers["host"], "example.test")
        self.assertFalse(request.wants_keep_alive)

    def test_rejects_bad_request_line(self) -> None:
        with self.assertRaises(HttpError) as raised:
            parse_request(b"GET /only-two-parts\r\n\r\n")

        self.assertEqual(raised.exception.status, 400)


class PathResolutionTests(unittest.TestCase):
    def test_resolves_inside_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            resolved = resolve_target(root, "/assets/app.css?cache=1")

        self.assertEqual(resolved, root / "assets" / "app.css")

    def test_rejects_parent_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            with self.assertRaises(HttpError) as raised:
                resolve_target(root, "/../secret.txt")

        self.assertEqual(raised.exception.status, 403)

    def test_rejects_encoded_parent_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            with self.assertRaises(HttpError) as raised:
                resolve_target(root, "/%2e%2e/secret.txt")

        self.assertEqual(raised.exception.status, 403)


class ResponseRenderingTests(unittest.TestCase):
    def test_render_headers_uses_crlf(self) -> None:
        rendered = render_headers(200, [("Content-Length", "0")])

        self.assertTrue(rendered.startswith(b"HTTP/1.1 200 OK\r\n"))
        self.assertTrue(rendered.endswith(b"\r\n\r\n"))
        self.assertIn(b"Content-Length: 0\r\n", rendered)


class StaticServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_serves_index_html(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "index.html").write_text("hello from veloxserver", encoding="utf-8")
            app = VeloxServer(ServerConfig(root=root))
            response = await send_request(
                app,
                b"GET / HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
            )

        self.assertIn(b"HTTP/1.1 200 OK\r\n", response)
        self.assertTrue(response.endswith(b"hello from veloxserver"))

    @unittest.skipUnless(is_h2_available(), "h2 is not installed")
    async def test_http2_serves_multiple_static_streams(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "one.txt").write_bytes(b"one")
            (root / "two.txt").write_bytes(b"two")
            app = VeloxServer(ServerConfig(root=root, http2=True))
            responses = await send_h2_requests(app, ["/one.txt", "/two.txt"])

        bodies = sorted(body for _status, body in responses.values())
        statuses = {status for status, _body in responses.values()}
        self.assertEqual(statuses, {200})
        self.assertEqual(bodies, [b"one", b"two"])

    async def test_virtual_hosts_select_different_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            first = base / "first"
            second = base / "second"
            first.mkdir()
            second.mkdir()
            (first / "index.html").write_bytes(b"first host")
            (second / "index.html").write_bytes(b"second host")
            app = VeloxServer(
                ServerConfig(
                    routes=(
                        RouteConfig(path="/", kind="static", hosts=("a.test",), root=first),
                        RouteConfig(path="/", kind="static", hosts=("b.test",), root=second),
                    )
                )
            )
            response = await send_request(
                app,
                b"GET / HTTP/1.1\r\nHost: b.test\r\nConnection: close\r\n\r\n",
            )

        self.assertTrue(response.endswith(b"second host"))

    async def test_directory_listing_can_be_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "asset.txt").write_bytes(b"asset")
            app = VeloxServer(
                ServerConfig(
                    routes=(
                        RouteConfig(path="/", kind="static", root=root, directory_listing=True),
                    )
                )
            )
            response = await send_request(
                app,
                b"GET / HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
            )

        self.assertIn(b"HTTP/1.1 200 OK\r\n", response)
        self.assertIn(b"asset.txt", response)

    async def test_basic_auth_protects_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "index.html").write_bytes(b"secret")
            app = VeloxServer(
                ServerConfig(
                    routes=(
                        RouteConfig(
                            path="/",
                            kind="static",
                            root=root,
                            basic_auth=(("admin", "s3cret"),),
                        ),
                    )
                )
            )
            denied = await send_request(
                app,
                b"GET / HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
            )
            token = base64.b64encode(b"admin:s3cret")
            allowed = await send_request(
                app,
                b"GET / HTTP/1.1\r\nHost: test\r\nAuthorization: Basic "
                + token
                + b"\r\nConnection: close\r\n\r\n",
            )

        self.assertIn(b"HTTP/1.1 401 Unauthorized\r\n", denied)
        self.assertTrue(allowed.endswith(b"secret"))

    async def test_jwt_hs256_auth_protects_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "index.html").write_bytes(b"jwt secret")
            app = VeloxServer(
                ServerConfig(
                    routes=(
                        RouteConfig(path="/", kind="static", root=root, jwt_hs256_secret="secret"),
                    )
                )
            )
            token = make_hs256_jwt({"sub": "awais"}, "secret")
            denied = await send_request(
                app,
                b"GET / HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
            )
            allowed = await send_request(
                app,
                b"GET / HTTP/1.1\r\nHost: test\r\nAuthorization: Bearer "
                + token.encode("ascii")
                + b"\r\nConnection: close\r\n\r\n",
            )

        self.assertIn(b"HTTP/1.1 401 Unauthorized\r\n", denied)
        self.assertTrue(allowed.endswith(b"jwt secret"))

    async def test_jwt_claims_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "index.html").write_bytes(b"claims ok")
            app = VeloxServer(
                ServerConfig(
                    routes=(
                        RouteConfig(
                            path="/",
                            kind="static",
                            root=root,
                            jwt_hs256_secret="secret",
                            jwt_issuer="https://issuer.test",
                            jwt_audience="velox",
                            jwt_required_claims=(("role", "admin"),),
                        ),
                    )
                )
            )
            bad_token = make_hs256_jwt({"iss": "https://issuer.test", "aud": "velox", "role": "user"}, "secret")
            good_token = make_hs256_jwt({"iss": "https://issuer.test", "aud": "velox", "role": "admin"}, "secret")
            denied = await send_request(
                app,
                f"GET / HTTP/1.1\r\nHost: test\r\nAuthorization: Bearer {bad_token}\r\nConnection: close\r\n\r\n".encode(),
            )
            allowed = await send_request(
                app,
                f"GET / HTTP/1.1\r\nHost: test\r\nAuthorization: Bearer {good_token}\r\nConnection: close\r\n\r\n".encode(),
            )

        self.assertIn(b"HTTP/1.1 401 Unauthorized\r\n", denied)
        self.assertTrue(allowed.endswith(b"claims ok"))

    async def test_rs256_jwks_file_enforces_oidc_claims(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "index.html").write_bytes(b"jwks ok")
            private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            jwks_path = root / "jwks.json"
            jwks_path.write_text(
                json.dumps({"keys": [rsa_public_jwk(private_key.public_key(), "kid-1")]}),
                encoding="utf-8",
            )
            app = VeloxServer(
                ServerConfig(
                    routes=(
                        RouteConfig(
                            path="/",
                            kind="static",
                            root=root,
                            jwt_jwks_file=jwks_path,
                            jwt_issuer="https://issuer.test",
                            jwt_audience="velox",
                            jwt_required_claims=(("scope", "read"),),
                        ),
                    )
                )
            )
            base_claims = {
                "iss": "https://issuer.test",
                "aud": "velox",
                "exp": int(time.time() + 60),
            }
            bad_token = make_rs256_jwt({**base_claims, "scope": "write"}, private_key, "kid-1")
            good_token = make_rs256_jwt({**base_claims, "scope": "read"}, private_key, "kid-1")
            denied = await send_request(
                app,
                f"GET / HTTP/1.1\r\nHost: test\r\nAuthorization: Bearer {bad_token}\r\nConnection: close\r\n\r\n".encode(),
            )
            allowed = await send_request(
                app,
                f"GET / HTTP/1.1\r\nHost: test\r\nAuthorization: Bearer {good_token}\r\nConnection: close\r\n\r\n".encode(),
            )

        self.assertIn(b"HTTP/1.1 401 Unauthorized\r\n", denied)
        self.assertTrue(allowed.endswith(b"jwks ok"))

    async def test_external_auth_url_allows_request(self) -> None:
        async def auth_app(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "index.html").write_bytes(b"external ok")
            auth_server = await asyncio.start_server(auth_app, host="127.0.0.1", port=0)
            auth_host, auth_port = auth_server.sockets[0].getsockname()[:2]
            app = VeloxServer(
                ServerConfig(
                    routes=(
                        RouteConfig(
                            path="/",
                            kind="static",
                            root=root,
                            external_auth_url=f"http://{auth_host}:{auth_port}/auth",
                        ),
                    )
                )
            )
            try:
                response = await send_request(
                    app,
                    b"GET / HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
                )
            finally:
                auth_server.close()
                await auth_server.wait_closed()

        self.assertTrue(response.endswith(b"external ok"))

    async def test_auth_request_subrequest_allows_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            auth_root = Path(tmp) / "auth"
            auth_root.mkdir()
            (root / "index.html").write_bytes(b"subrequest ok")
            (auth_root / "allow").write_bytes(b"ok")
            app = VeloxServer(
                ServerConfig(
                    routes=(
                        RouteConfig(path="/auth/", kind="static", root=auth_root),
                        RouteConfig(path="/", kind="static", root=root, auth_request="/auth/allow"),
                    )
                )
            )
            response = await send_request(
                app,
                b"GET / HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
            )

        self.assertTrue(response.endswith(b"subrequest ok"))

    async def test_plugin_can_block_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            plugin_path = root / "plugin.py"
            (root / "index.html").write_bytes(b"ok")
            plugin_path.write_text(
                "def on_request(request):\n"
                "    if request.target == '/blocked':\n"
                "        return {'allowed': False, 'status': 403, 'message': 'Plugin blocked'}\n"
                "    return True\n",
                encoding="utf-8",
            )
            app = VeloxServer(ServerConfig(root=root, plugin_paths=(plugin_path,)))
            blocked = await send_request(
                app,
                b"GET /blocked HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
            )

        self.assertIn(b"HTTP/1.1 403 Forbidden\r\n", blocked)
        self.assertTrue(blocked.endswith(b"403 Plugin blocked\n"))

    async def test_waf_plugin_can_block_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            plugin_path = root / "waf.py"
            (root / "index.html").write_bytes(b"ok")
            plugin_path.write_text(
                "def on_waf_request(request):\n"
                "    if 'attack' in request.target:\n"
                "        return {'allowed': False, 'status': 403, 'message': 'WAF blocked'}\n"
                "    return True\n",
                encoding="utf-8",
            )
            app = VeloxServer(ServerConfig(root=root, plugin_paths=(plugin_path,)))
            blocked = await send_request(
                app,
                b"GET /?q=attack HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
            )

        self.assertIn(b"HTTP/1.1 403 Forbidden\r\n", blocked)
        self.assertTrue(blocked.endswith(b"403 WAF blocked\n"))

    async def test_serves_precompressed_brotli_asset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "app.css").write_bytes(b"body{}")
            (root / "app.css.br").write_bytes(b"brotli bytes")
            app = VeloxServer(ServerConfig(root=root))
            response = await send_request(
                app,
                (
                    b"GET /app.css HTTP/1.1\r\n"
                    b"Host: test\r\n"
                    b"Accept-Encoding: br, gzip\r\n"
                    b"Connection: close\r\n\r\n"
                ),
            )

        self.assertIn(b"Content-Encoding: br\r\n", response)
        self.assertTrue(response.endswith(b"brotli bytes"))

    async def test_custom_error_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            error_page = root / "404.html"
            error_page.write_bytes(b"<h1>missing</h1>")
            app = VeloxServer(ServerConfig(root=root, error_pages=((404, error_page),)))
            response = await send_request(
                app,
                b"GET /none HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
            )

        self.assertIn(b"HTTP/1.1 404 Not Found\r\n", response)
        self.assertTrue(response.endswith(b"<h1>missing</h1>"))

    async def test_rewrite_and_waf_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "new.html").write_bytes(b"rewritten")
            app = VeloxServer(
                ServerConfig(
                    root=root,
                    rewrite_rules=((r"^/old$", "/new.html"),),
                    waf_block_path_patterns=(r"/blocked",),
                )
            )
            rewritten = await send_request(
                app,
                b"GET /old HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
            )
            blocked = await send_request(
                app,
                b"GET /blocked HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
            )

        self.assertTrue(rewritten.endswith(b"rewritten"))
        self.assertIn(b"HTTP/1.1 403 Forbidden\r\n", blocked)

    async def test_advanced_rewrite_matches_host_header_and_query(self) -> None:
        from veloxserver.server import AdvancedRewriteRule

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "new").mkdir()
            (root / "new" / "page.txt").write_bytes(b"advanced rewrite")
            app = VeloxServer(
                ServerConfig(
                    root=root,
                    advanced_rewrite_rules=(
                        AdvancedRewriteRule(
                            pattern=r"^/old/(.*)$",
                            replacement="/new/$1?seen=$arg_x",
                            methods=("GET",),
                            hosts=("test",),
                            header=("x-mode", "on"),
                        ),
                    ),
                )
            )
            response = await send_request(
                app,
                b"GET /old/page.txt?x=1 HTTP/1.1\r\n"
                b"Host: test\r\n"
                b"X-Mode: on\r\n"
                b"Connection: close\r\n\r\n",
            )

        self.assertIn(b"HTTP/1.1 200 OK\r\n", response)
        self.assertTrue(response.endswith(b"advanced rewrite"))

    async def test_gzips_text_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            script = b"const value = 42;\n" * 100
            (root / "app.js").write_bytes(script)
            app = VeloxServer(ServerConfig(root=root, gzip=True, gzip_min_bytes=1))
            response = await send_request(
                app,
                (
                    b"GET /app.js HTTP/1.1\r\n"
                    b"Host: test\r\n"
                    b"Accept-Encoding: gzip\r\n"
                    b"Connection: close\r\n"
                    b"\r\n"
                ),
            )

        _head, _sep, body = response.partition(b"\r\n\r\n")
        self.assertIn(b"Content-Encoding: gzip\r\n", response)
        self.assertEqual(gzip.decompress(body), script)

    async def test_health_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = VeloxServer(ServerConfig(root=Path(tmp)))
            response = await send_request(
                app,
                b"GET /healthz HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
            )

        self.assertIn(b"HTTP/1.1 200 OK\r\n", response)
        self.assertTrue(response.endswith(b'{"status":"ok"}\n'))

    async def test_health_endpoint_reports_generation_header(self) -> None:
        previous = os.environ.get("VELOXSERVER_GENERATION")
        os.environ["VELOXSERVER_GENERATION"] = "7"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                app = VeloxServer(ServerConfig(root=Path(tmp)))
                response = await send_request(
                    app,
                    b"GET /healthz HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
                )
        finally:
            if previous is None:
                os.environ.pop("VELOXSERVER_GENERATION", None)
            else:
                os.environ["VELOXSERVER_GENERATION"] = previous

        self.assertIn(b"X-VeloxServer-Generation: 7\r\n", response)

    async def test_metrics_endpoint_reports_requests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "index.html").write_bytes(b"ok")
            app = VeloxServer(ServerConfig(root=root))
            await send_request(
                app,
                b"GET / HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
            )
            response = await send_request(
                app,
                b"GET /metrics HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
            )

        self.assertIn(b"HTTP/1.1 200 OK\r\n", response)
        self.assertIn(b"veloxserver_requests_total 1", response)

    async def test_ai_route_serves_chat_api_and_web_ui(self) -> None:
        app = VeloxServer(
            ServerConfig(
                routes=(
                    RouteConfig(
                        path="/ai/",
                        kind="ai",
                        ai_backend="echo",
                        ai_model_name="local-test-model",
                        ai_system_prompt="Answer like a VeloxServer test model.",
                        ai_max_tokens=32,
                    ),
                )
            )
        )
        payload = json.dumps(
            {
                "messages": [{"role": "user", "content": "hello model"}],
                "max_tokens": 20,
            }
        ).encode("utf-8")

        api_response = await send_request(
            app,
            b"POST /ai/v1/chat/completions HTTP/1.1\r\n"
            b"Host: test\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(payload)}\r\n".encode("ascii")
            + b"Connection: close\r\n\r\n"
            + payload,
        )
        models_response = await send_request(
            app,
            b"GET /ai/v1/models HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
        )
        page_response = await send_request(
            app,
            b"GET /ai/ HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
        )

        body = json.loads(api_response.split(b"\r\n\r\n", 1)[1])
        models = json.loads(models_response.split(b"\r\n\r\n", 1)[1])
        self.assertEqual(body["model"], "local-test-model")
        self.assertIn("hello model", body["choices"][0]["message"]["content"])
        self.assertEqual(models["data"][0]["id"], "local-test-model")
        self.assertIn(b"text/html; charset=utf-8", page_response)
        self.assertIn(b"local-test-model", page_response)

    async def test_ai_route_supports_sse_stream_shape(self) -> None:
        app = VeloxServer(
            ServerConfig(
                routes=(
                    RouteConfig(
                        path="/ai/",
                        kind="ai",
                        ai_backend="echo",
                        ai_model_name="stream-model",
                    ),
                )
            )
        )
        payload = json.dumps(
            {
                "messages": [{"role": "user", "content": "stream please"}],
                "stream": True,
            }
        ).encode("utf-8")

        response = await send_request(
            app,
            b"POST /ai/v1/chat/completions HTTP/1.1\r\n"
            b"Host: test\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(payload)}\r\n".encode("ascii")
            + b"Connection: close\r\n\r\n"
            + payload,
        )

        self.assertIn(b"Content-Type: text/event-stream; charset=utf-8", response)
        self.assertIn(b"data: ", response)
        self.assertTrue(response.endswith(b"data: [DONE]\n\n"))

    async def test_rate_limit_blocks_excess_requests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "index.html").write_bytes(b"ok")
            app = VeloxServer(
                ServerConfig(
                    root=root,
                    rate_limit_per_minute=1,
                    rate_limit_burst=1,
                )
            )
            first = await send_request(
                app,
                b"GET / HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
            )
            second = await send_request(
                app,
                b"GET / HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
            )

        self.assertIn(b"HTTP/1.1 200 OK\r\n", first)
        self.assertIn(b"HTTP/1.1 429 Too Many Requests\r\n", second)


class ProxyServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_reverse_proxy_strips_prefix(self) -> None:
        seen: list[bytes] = []

        async def upstream(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            seen.append(await reader.readuntil(b"\r\n\r\n"))
            body = b"proxy works"
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                + f"Content-Length: {len(body)}\r\n".encode("ascii")
                + b"Connection: close\r\n"
                + b"\r\n"
                + body
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        upstream_server = await asyncio.start_server(upstream, host="127.0.0.1", port=0)
        upstream_host, upstream_port = upstream_server.sockets[0].getsockname()[:2]
        app = VeloxServer(
            ServerConfig(
                proxy_timeout=0.2,
                routes=(
                    RouteConfig(
                        path="/api/",
                        kind="proxy",
                        upstream=f"http://{upstream_host}:{upstream_port}",
                        strip_prefix=True,
                    ),
                )
            )
        )

        try:
            response = await send_request(
                app,
                b"GET /api/users?x=1 HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
            )
        finally:
            upstream_server.close()
            await upstream_server.wait_closed()

        self.assertIn(b"HTTP/1.1 200 OK\r\n", response)
        self.assertTrue(response.endswith(b"proxy works"))
        self.assertIn(b"GET /users?x=1 HTTP/1.1\r\n", seen[0])

    async def test_proxy_forwards_chunked_response_with_trailer(self) -> None:
        async def upstream(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Transfer-Encoding: chunked\r\n"
                b"Trailer: X-Trace\r\n"
                b"Connection: keep-alive\r\n"
                b"\r\n"
                b"4\r\nWiki\r\n5\r\npedia\r\n0\r\nX-Trace: ok\r\n\r\n"
            )
            await writer.drain()
            await asyncio.sleep(0.25)
            writer.close()
            await writer.wait_closed()

        upstream_server = await asyncio.start_server(upstream, host="127.0.0.1", port=0)
        upstream_host, upstream_port = upstream_server.sockets[0].getsockname()[:2]
        app = VeloxServer(
            ServerConfig(
                proxy_timeout=1.0,
                routes=(
                    RouteConfig(
                        path="/api/",
                        kind="proxy",
                        upstream=f"http://{upstream_host}:{upstream_port}",
                    ),
                )
            )
        )

        try:
            response = await send_request(
                app,
                b"GET /api/chunked HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
            )
        finally:
            upstream_server.close()
            await upstream_server.wait_closed()

        self.assertIn(b"HTTP/1.1 200 OK\r\n", response)
        self.assertIn(b"Transfer-Encoding: chunked\r\n", response)
        self.assertIn(b"X-Trace: ok\r\n", response)
        self.assertTrue(response.endswith(b"4\r\nWiki\r\n5\r\npedia\r\n0\r\nX-Trace: ok\r\n\r\n"))

    @unittest.skipUnless(is_h2_available(), "h2 is not installed")
    async def test_http2_proxy_decodes_chunked_upstream_body(self) -> None:
        async def upstream(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Transfer-Encoding: chunked\r\n"
                b"Connection: keep-alive\r\n"
                b"\r\n"
                b"4\r\nWiki\r\n5\r\npedia\r\n0\r\n\r\n"
            )
            await writer.drain()
            await asyncio.sleep(0.25)
            writer.close()
            await writer.wait_closed()

        upstream_server = await asyncio.start_server(upstream, host="127.0.0.1", port=0)
        upstream_host, upstream_port = upstream_server.sockets[0].getsockname()[:2]
        app = VeloxServer(
            ServerConfig(
                http2=True,
                proxy_timeout=1.0,
                routes=(
                    RouteConfig(
                        path="/api/",
                        kind="proxy",
                        upstream=f"http://{upstream_host}:{upstream_port}",
                    ),
                ),
            )
        )

        try:
            responses = await send_h2_requests(app, ["/api/chunked"])
        finally:
            upstream_server.close()
            await upstream_server.wait_closed()

        status, body = next(iter(responses.values()))
        self.assertEqual(status, 200)
        self.assertEqual(body, b"Wikipedia")

    async def test_proxy_retries_next_upstream(self) -> None:
        async def upstream(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            await reader.readuntil(b"\r\n\r\n")
            body = b"healthy upstream"
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                + f"Content-Length: {len(body)}\r\n".encode("ascii")
                + b"Connection: close\r\n"
                + b"\r\n"
                + body
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        bad_port = unused_port()
        upstream_server = await asyncio.start_server(upstream, host="127.0.0.1", port=0)
        upstream_host, upstream_port = upstream_server.sockets[0].getsockname()[:2]
        app = VeloxServer(
            ServerConfig(
                proxy_timeout=0.2,
                routes=(
                    RouteConfig(
                        path="/api/",
                        kind="proxy",
                        upstreams=(
                            f"http://127.0.0.1:{bad_port}",
                            f"http://{upstream_host}:{upstream_port}",
                        ),
                        retries=1,
                    ),
                )
            )
        )

        try:
            response = await send_request(
                app,
                b"GET /api/users HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
            )
        finally:
            upstream_server.close()
            await upstream_server.wait_closed()

        self.assertIn(b"HTTP/1.1 200 OK\r\n", response)
        self.assertTrue(response.endswith(b"healthy upstream"))
        self.assertEqual(app.metrics.proxy_retries_total, 1)

    async def test_proxy_opens_circuit_after_failure(self) -> None:
        app = VeloxServer(
            ServerConfig(
                routes=(
                    RouteConfig(
                        path="/api/",
                        kind="proxy",
                        upstream=f"http://127.0.0.1:{unused_port()}",
                        retries=0,
                        circuit_failures=1,
                        circuit_cooldown=30,
                    ),
                )
            )
        )

        first = await send_request(
            app,
            b"GET /api/users HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
        )
        second = await send_request(
            app,
            b"GET /api/users HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
        )

        self.assertIn(b"HTTP/1.1 502 Bad Gateway\r\n", first)
        self.assertIn(b"HTTP/1.1 503 Service Unavailable\r\n", second)

    async def test_proxy_serves_fallback_when_all_upstreams_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fallback_file = Path(tmp) / "fallback.txt"
            fallback_file.write_text("fallback content", encoding="utf-8")
            app = VeloxServer(
                ServerConfig(
                    routes=(
                        RouteConfig(
                            path="/api/",
                            kind="proxy",
                            upstream=f"http://127.0.0.1:{unused_port()}",
                            retries=0,
                            circuit_failures=1,
                            proxy_fallback_path=fallback_file,
                        ),
                    )
                )
            )
            response = await send_request(
                app,
                b"GET /api/users HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
            )

        self.assertIn(b"HTTP/1.1 200 OK\r\n", response)
        self.assertTrue(response.endswith(b"fallback content"))

    async def test_admin_reload_endpoint_reloads_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "veloxserver.toml"
            root = Path(tmp) / "root"
            root.mkdir()
            (root / "index.html").write_text("first", encoding="utf-8")
            config_path.write_text(
                """
[admin]
enabled = true

[[routes]]
path = "/"
kind = "static"
root = "root"
""".strip(),
                encoding="utf-8",
            )
            app = VeloxServer(load_config(config_path))
            first = await send_request(
                app,
                b"GET / HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
            )
            (root / "index.html").write_text("second", encoding="utf-8")
            reload_response = await send_request(
                app,
                b"POST /__veloxserver/reload HTTP/1.1\r\nHost: test\r\nConnection: close\r\nContent-Length: 0\r\n\r\n",
            )
            second = await send_request(
                app,
                b"GET / HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
            )

        self.assertIn(b"HTTP/1.1 200 OK\r\n", first)
        self.assertIn(b"HTTP/1.1 200 OK\r\n", reload_response)
        self.assertIn(b"HTTP/1.1 200 OK\r\n", second)
        self.assertTrue(second.endswith(b"second"))

    async def test_proxy_cache_serves_second_get_without_upstream(self) -> None:
        calls = 0

        async def upstream(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            nonlocal calls
            calls += 1
            await reader.readuntil(b"\r\n\r\n")
            body = b"cache me"
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                + f"Content-Length: {len(body)}\r\n".encode("ascii")
                + b"Connection: close\r\n\r\n"
                + body
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        upstream_server = await asyncio.start_server(upstream, host="127.0.0.1", port=0)
        upstream_host, upstream_port = upstream_server.sockets[0].getsockname()[:2]
        app = VeloxServer(
            ServerConfig(
                routes=(
                    RouteConfig(
                        path="/api/",
                        kind="proxy",
                        upstream=f"http://{upstream_host}:{upstream_port}",
                        proxy_cache=True,
                        proxy_cache_ttl=60,
                    ),
                )
            )
        )

        try:
            first = await send_request(
                app,
                b"GET /api/item HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
            )
            second = await send_request(
                app,
                b"GET /api/item HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
            )
        finally:
            upstream_server.close()
            await upstream_server.wait_closed()

        self.assertTrue(first.endswith(b"cache me"))
        self.assertTrue(second.endswith(b"cache me"))
        self.assertEqual(calls, 1)
        self.assertEqual(app.metrics.proxy_cache_hits_total, 1)

    async def test_proxy_disk_cache_and_purge(self) -> None:
        calls = 0

        async def upstream(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            nonlocal calls
            calls += 1
            await reader.readuntil(b"\r\n\r\n")
            body = b"disk cache"
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                + f"Content-Length: {len(body)}\r\n".encode("ascii")
                + b"Connection: close\r\n\r\n"
                + body
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "proxy-cache"
            upstream_server = await asyncio.start_server(upstream, host="127.0.0.1", port=0)
            upstream_host, upstream_port = upstream_server.sockets[0].getsockname()[:2]
            app = VeloxServer(
                ServerConfig(
                    routes=(
                        RouteConfig(
                            path="/api/",
                            kind="proxy",
                            upstream=f"http://{upstream_host}:{upstream_port}",
                            proxy_cache=True,
                            proxy_cache_ttl=60,
                            proxy_cache_path=cache_dir,
                            proxy_cache_purge=True,
                        ),
                    )
                )
            )

            try:
                first = await send_request(
                    app,
                    b"GET /api/item HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
                )
            finally:
                upstream_server.close()
                await upstream_server.wait_closed()

            app.route_runtimes["/api/"].cache.clear()
            second = await send_request(
                app,
                b"GET /api/item HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
            )
            purged = await send_request(
                app,
                b"PURGE /api/item HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
            )

        self.assertTrue(first.endswith(b"disk cache"))
        self.assertTrue(second.endswith(b"disk cache"))
        self.assertIn(b"HTTP/1.1 200 OK\r\n", purged)
        self.assertEqual(calls, 1)

    async def test_proxy_tunnels_websocket_upgrade(self) -> None:
        seen: list[bytes] = []

        async def upstream(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            seen.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(
                b"HTTP/1.1 101 Switching Protocols\r\n"
                b"Upgrade: websocket\r\n"
                b"Connection: Upgrade\r\n"
                b"\r\n"
            )
            await writer.drain()
            payload = await reader.readexactly(4)
            writer.write(payload.upper())
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        upstream_server = await asyncio.start_server(upstream, host="127.0.0.1", port=0)
        upstream_host, upstream_port = upstream_server.sockets[0].getsockname()[:2]
        app = VeloxServer(
            ServerConfig(
                proxy_timeout=1.0,
                routes=(
                    RouteConfig(
                        path="/ws/",
                        kind="proxy",
                        upstream=f"http://{upstream_host}:{upstream_port}",
                    ),
                ),
            )
        )
        server = await asyncio.start_server(app._handle_client, host="127.0.0.1", port=0)
        host, port = server.sockets[0].getsockname()[:2]

        try:
            reader, writer = await asyncio.open_connection(host, port)
            writer.write(
                b"GET /ws/chat HTTP/1.1\r\n"
                b"Host: test\r\n"
                b"Connection: Upgrade\r\n"
                b"Upgrade: websocket\r\n"
                b"Sec-WebSocket-Key: test\r\n"
                b"\r\n"
            )
            await writer.drain()
            response_head = await reader.readuntil(b"\r\n\r\n")
            writer.write(b"ping")
            await writer.drain()
            tunneled = await reader.readexactly(4)
            writer.close()
            await writer.wait_closed()
        finally:
            server.close()
            await server.wait_closed()
            upstream_server.close()
            await upstream_server.wait_closed()

        self.assertIn(b"HTTP/1.1 101 Switching Protocols\r\n", response_head)
        self.assertEqual(tunneled, b"PING")
        self.assertIn(b"Connection: Upgrade\r\n", seen[0])
        self.assertIn(b"Upgrade: websocket\r\n", seen[0])


class StreamProxyTests(unittest.IsolatedAsyncioTestCase):
    def test_stream_proxy_selects_least_connections_and_skips_down_upstream(self) -> None:
        proxy = StreamProxyConfig(
            name="stream",
            protocol="tcp",
            listen_host="127.0.0.1",
            listen_port=2525,
            upstream_host="127.0.0.1",
            upstream_port=25,
            upstreams=(("127.0.0.1", 25), ("127.0.0.1", 26)),
            load_balance="least_connections",
            max_fails=1,
            fail_timeout=60,
        )
        manager = StreamProxyManager((proxy,))
        manager.upstream_active[stream_upstream_key(proxy, "127.0.0.1", 25)] = 5

        self.assertEqual(manager._select_upstream(proxy, ("127.0.0.1", 1000)), ("127.0.0.1", 26))

        manager._record_failure(proxy, stream_upstream_key(proxy, "127.0.0.1", 26))
        self.assertEqual(manager._select_upstream(proxy, ("127.0.0.1", 1000)), ("127.0.0.1", 25))

    def test_udp_proxy_round_robins_upstreams(self) -> None:
        proxy = StreamProxyConfig(
            name="dns",
            protocol="udp",
            listen_host="127.0.0.1",
            listen_port=5353,
            upstream_host="127.0.0.1",
            upstream_port=53,
            upstreams=(("127.0.0.1", 53), ("127.0.0.1", 54)),
        )

        first = select_udp_upstream(proxy, ("127.0.0.1", 1000), 0)
        second = select_udp_upstream(proxy, ("127.0.0.1", 1000), 1)

        self.assertEqual(first, ("127.0.0.1", 53))
        self.assertEqual(second, ("127.0.0.1", 54))

    async def test_tcp_stream_proxy_forwards_bytes(self) -> None:
        async def upstream(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            data = await reader.read(100)
            writer.write(data.upper())
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        upstream_server = await asyncio.start_server(upstream, host="127.0.0.1", port=0)
        upstream_host, upstream_port = upstream_server.sockets[0].getsockname()[:2]
        listen_port = unused_port()
        manager = StreamProxyManager(
            (
                StreamProxyConfig(
                    name="test",
                    protocol="tcp",
                    listen_host="127.0.0.1",
                    listen_port=listen_port,
                    upstream_host=upstream_host,
                    upstream_port=upstream_port,
                ),
            )
        )

        try:
            await manager.start()
            reader, writer = await asyncio.open_connection("127.0.0.1", listen_port)
            writer.write(b"hello")
            await writer.drain()
            self.assertEqual(await reader.read(5), b"HELLO")
            writer.close()
            await writer.wait_closed()
        finally:
            await manager.close()
            upstream_server.close()
            await upstream_server.wait_closed()

    async def test_stream_proxy_can_send_proxy_protocol(self) -> None:
        seen: list[bytes] = []

        async def upstream(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            seen.append(await reader.readline())
            data = await reader.read(4)
            writer.write(data.upper())
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        upstream_server = await asyncio.start_server(upstream, host="127.0.0.1", port=0)
        upstream_host, upstream_port = upstream_server.sockets[0].getsockname()[:2]
        listen_port = unused_port()
        manager = StreamProxyManager(
            (
                StreamProxyConfig(
                    name="mail",
                    protocol="smtp",
                    listen_host="127.0.0.1",
                    listen_port=listen_port,
                    upstream_host=upstream_host,
                    upstream_port=upstream_port,
                    proxy_protocol=True,
                ),
            )
        )

        try:
            await manager.start()
            reader, writer = await asyncio.open_connection("127.0.0.1", listen_port)
            writer.write(b"mail")
            await writer.drain()
            self.assertEqual(await reader.read(4), b"MAIL")
            writer.close()
            await writer.wait_closed()
        finally:
            await manager.close()
            upstream_server.close()
            await upstream_server.wait_closed()

        self.assertTrue(seen[0].startswith(b"PROXY TCP"))


class ConfigTests(unittest.TestCase):
    def test_validate_accepts_basic_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            public = root / "public"
            public.mkdir()
            config_path = root / "veloxserver.toml"
            config_path.write_text(
                """
[[routes]]
path = "/"
kind = "static"
root = "public"
""".strip(),
                encoding="utf-8",
            )
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = run_validate(["--config", str(config_path), "--json"])

        self.assertEqual(code, 0)
        diagnostics = json.loads(out.getvalue())
        self.assertTrue(any(item["level"] == "ok" for item in diagnostics))

    def test_validate_reports_missing_static_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "veloxserver.toml"
            config_path.write_text(
                """
[[routes]]
path = "/"
kind = "static"
root = "missing"
""".strip(),
                encoding="utf-8",
            )
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = run_validate(["--config", str(config_path)])

        self.assertEqual(code, 1)
        self.assertIn("static root does not exist", out.getvalue())

    def test_doctor_outputs_json(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = run_doctor(["--json"])

        self.assertEqual(code, 0)
        diagnostics = json.loads(out.getvalue())
        self.assertTrue(any("Python" in item["message"] for item in diagnostics))

    def test_rejects_unsupported_protocol_switches(self) -> None:
        if not is_h2_available():
            with self.assertRaisesRegex(RuntimeError, "HTTP/2"):
                VeloxServer(ServerConfig(http2=True))
        with self.assertRaisesRegex((RuntimeError, ValueError), "HTTP/3"):
            VeloxServer(ServerConfig(http3=True))
        with self.assertRaisesRegex(RuntimeError, "native core library not built"):
            VeloxServer(ServerConfig(native_core="rust"))

    def test_loads_toml_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "veloxserver.toml"
            config_path.write_text(
                """
[server]
host = "0.0.0.0"
port = 9090
workers = 2
reuse_port = true
upgrade_command = "python -m veloxserver --config veloxserver.toml"
upgrade_ready_timeout = 12
upgrade_state_path = "run/upgrade.json"
file_io_backend = "threaded"
sendfile = false
aio_threads = 2
directio_min_bytes = 1048576
io_uring = false
open_file_cache_entries = 64
open_file_cache_errors = true
open_file_cache_min_uses = 2
open_file_cache_inactive = 30
open_file_cache_metadata = true
shared_zone_path = "run/zones.sqlite3"
plugin_paths = ["plugins/waf.py"]
gzip = true
access_log = true
log_format = "json"
metrics_path = "/internal/metrics"
rate_limit_per_minute = 120
connection_limit = 50
connection_limit_per_client = 5
access_log_path = "logs/access.log"
log_rotate_bytes = 1000
waf_block_path_patterns = ["/blocked"]
rewrite_rules = [{ pattern = "^/old$", replacement = "/new" }]
advanced_rewrite_rules = [{ pattern = "^/v(?P<version>[0-9]+)/(.*)$", replacement = "/api/$version/$2", methods = ["GET"], hosts = ["example.test"], header = { name = "x-rewrite", pattern = "on" } }]
error_pages = { 404 = "errors/404.html" }
tls_certfile = "cert.pem"
tls_keyfile = "key.pem"
tls_ciphers = "ECDHE+AESGCM"
tls_ciphersuites = "TLS_AES_256_GCM_SHA384"
tls_min_version = "TLSv1.2"
tls_session_tickets = false
tls_client_verify = "off"
tls_ecdh_curve = "prime256v1"
tls_keylog_file = "logs/tls.keys"
tls_alpn_protocols = ["h2", "http/1.1"]
tls_ocsp_required = false
tls_ocsp_response_file = "ocsp.der"
http3 = false
http3_port = 9443
native_core = "python"
native_core_path = "native"

[ai_error_repair]
enabled = true
project_path = "."
log_path = "logs/ai-repair.log"
suggestions_path = ".veloxserver/repair-suggestions"
model = "gpt-4.1-mini"
api_key_env = "OPENAI_API_KEY"
min_status = 500
statuses = [500, 502, 503]
cooldown_seconds = 5
apply = false
context_files = ["veloxserver.toml", "pyproject.toml"]
max_file_bytes = 4096
max_context_bytes = 8192
max_output_tokens = 600

[[routes]]
path = "/"
kind = "static"
root = "public"
hosts = ["example.test"]
directory_listing = true
basic_auth = { admin = "secret" }
jwt_hs256_secret = "jwt-secret"
jwt_issuer = "https://issuer.test"
jwt_audience = "velox"
jwt_required_claims = { role = "admin" }
jwt_jwks_file = "jwks.json"
oidc_jwks_url = "https://issuer.test/.well-known/jwks.json"
jwt_jwks_cache_ttl = 120
external_auth_url = "http://127.0.0.1:9002/auth"
auth_request = "/auth/check"

[[routes]]
path = "/api/"
kind = "proxy"
upstreams = ["http://127.0.0.1:9000", "http://127.0.0.1:9001"]
upstream_weights = [2, 1]
strip_prefix = true
retries = 2
circuit_failures = 1
active_health_interval = 10
proxy_cache = true
proxy_cache_ttl = 30
proxy_cache_path = "cache/api"
proxy_cache_key = "$protocol $method $host $uri"
proxy_cache_methods = ["GET", "HEAD"]
proxy_cache_lock = true
proxy_cache_stale_while_revalidate = 15
proxy_cache_use_stale_on_error = true
proxy_cache_purge = true

[[routes]]
path = "/ai/"
kind = "ai"
ai_backend = "echo"
ai_model_path = "models/tiny.gguf"
ai_model_name = "tiny-local"
ai_system_prompt = "Answer only about VeloxServer."
ai_max_tokens = 128
ai_temperature = 0.2
ai_context_window = 2048
ai_chat_enabled = true
ai_api_enabled = true

[[streams]]
name = "smtp"
protocol = "tcp"
listen_host = "127.0.0.1"
listen_port = 2525
upstream_host = "127.0.0.1"
upstream_port = 25
upstreams = ["127.0.0.1:25", { host = "127.0.0.1", port = 26 }]
load_balance = "ip_hash"
proxy_protocol = true
max_connections = 100
max_fails = 2
fail_timeout = 5
""".strip(),
                encoding="utf-8",
            )

            config = load_config(config_path)

        self.assertEqual(config.host, "0.0.0.0")
        self.assertEqual(config.port, 9090)
        self.assertEqual(config.workers, 2)
        self.assertTrue(config.reuse_port)
        self.assertEqual(config.upgrade_ready_timeout, 12)
        self.assertEqual(config.upgrade_state_path, root / "run" / "upgrade.json")
        self.assertEqual(config.file_io_backend, "threaded")
        self.assertFalse(config.sendfile)
        self.assertEqual(config.aio_threads, 2)
        self.assertEqual(config.directio_min_bytes, 1048576)
        self.assertFalse(config.io_uring)
        self.assertEqual(config.open_file_cache_entries, 64)
        self.assertTrue(config.open_file_cache_errors)
        self.assertEqual(config.open_file_cache_min_uses, 2)
        self.assertEqual(config.open_file_cache_inactive, 30)
        self.assertTrue(config.open_file_cache_metadata)
        self.assertEqual(config.shared_zone_path, root / "run" / "zones.sqlite3")
        self.assertEqual(config.plugin_paths, (root / "plugins" / "waf.py",))
        self.assertTrue(config.gzip)
        self.assertEqual(config.log_format, "json")
        self.assertEqual(config.metrics_path, "/internal/metrics")
        self.assertEqual(config.rate_limit_per_minute, 120)
        self.assertEqual(config.connection_limit, 50)
        self.assertEqual(config.connection_limit_per_client, 5)
        self.assertEqual(config.access_log_path, root / "logs" / "access.log")
        self.assertEqual(config.log_rotate_bytes, 1000)
        self.assertEqual(config.waf_block_path_patterns, ("/blocked",))
        self.assertEqual(config.rewrite_rules, ((r"^/old$", "/new"),))
        self.assertEqual(len(config.advanced_rewrite_rules), 1)
        self.assertEqual(config.advanced_rewrite_rules[0].header, ("x-rewrite", "on"))
        self.assertEqual(config.error_pages, ((404, root / "errors" / "404.html"),))
        self.assertEqual(config.tls_certfile, root / "cert.pem")
        self.assertEqual(config.tls_keyfile, root / "key.pem")
        self.assertEqual(config.tls_ciphers, "ECDHE+AESGCM")
        self.assertEqual(config.tls_ciphersuites, "TLS_AES_256_GCM_SHA384")
        self.assertEqual(config.tls_ecdh_curve, "prime256v1")
        self.assertEqual(config.tls_keylog_file, root / "logs" / "tls.keys")
        self.assertEqual(config.tls_alpn_protocols, ("h2", "http/1.1"))
        self.assertFalse(config.tls_session_tickets)
        self.assertEqual(config.tls_ocsp_response_file, root / "ocsp.der")
        self.assertEqual(config.http3_port, 9443)
        self.assertEqual(config.native_core_path, root / "native")
        self.assertTrue(config.ai_error_repair_enabled)
        self.assertEqual(config.ai_error_repair_project_path, root)
        self.assertEqual(config.ai_error_repair_log_path, root / "logs" / "ai-repair.log")
        self.assertEqual(config.ai_error_repair_suggestions_path, root / ".veloxserver" / "repair-suggestions")
        self.assertEqual(config.ai_error_repair_model, "gpt-4.1-mini")
        self.assertEqual(config.ai_error_repair_statuses, (500, 502, 503))
        self.assertEqual(config.ai_error_repair_context_files, (root / "veloxserver.toml", root / "pyproject.toml"))
        self.assertEqual(config.ai_error_repair_max_file_bytes, 4096)
        self.assertEqual(config.ai_error_repair_max_context_bytes, 8192)
        self.assertEqual(config.ai_error_repair_max_output_tokens, 600)
        self.assertEqual(len(config.stream_proxies), 1)
        self.assertEqual(config.stream_proxies[0].listen_port, 2525)
        self.assertEqual(config.stream_proxies[0].upstreams, (("127.0.0.1", 25), ("127.0.0.1", 26)))
        self.assertEqual(config.stream_proxies[0].load_balance, "ip_hash")
        self.assertTrue(config.stream_proxies[0].proxy_protocol)
        self.assertEqual(config.stream_proxies[0].max_connections, 100)
        self.assertEqual(config.stream_proxies[0].max_fails, 2)
        self.assertEqual(config.stream_proxies[0].fail_timeout, 5)
        self.assertEqual(len(config.routes), 3)
        self.assertEqual(config.routes[0].root, root / "public")
        self.assertEqual(config.routes[0].hosts, ("example.test",))
        self.assertTrue(config.routes[0].directory_listing)
        self.assertEqual(config.routes[0].basic_auth, (("admin", "secret"),))
        self.assertEqual(config.routes[0].jwt_issuer, "https://issuer.test")
        self.assertEqual(config.routes[0].jwt_audience, "velox")
        self.assertEqual(config.routes[0].jwt_required_claims, (("role", "admin"),))
        self.assertEqual(config.routes[0].jwt_jwks_file, root / "jwks.json")
        self.assertEqual(config.routes[0].jwt_jwks_url, "https://issuer.test/.well-known/jwks.json")
        self.assertEqual(config.routes[0].jwt_jwks_cache_ttl, 120)
        self.assertEqual(config.routes[0].external_auth_url, "http://127.0.0.1:9002/auth")
        self.assertEqual(config.routes[0].auth_request, "/auth/check")
        self.assertTrue(config.routes[1].strip_prefix)
        self.assertEqual(config.routes[1].upstreams, ("http://127.0.0.1:9000", "http://127.0.0.1:9001"))
        self.assertEqual(config.routes[1].upstream_weights, (2, 1))
        self.assertEqual(config.routes[1].retries, 2)
        self.assertTrue(config.routes[1].proxy_cache)
        self.assertEqual(config.routes[1].proxy_cache_path, root / "cache" / "api")
        self.assertTrue(config.routes[1].proxy_cache_lock)
        self.assertEqual(config.routes[1].proxy_cache_stale_while_revalidate, 15)
        self.assertTrue(config.routes[1].proxy_cache_use_stale_on_error)
        self.assertTrue(config.routes[1].proxy_cache_purge)
        self.assertEqual(config.routes[2].kind, "ai")
        self.assertEqual(config.routes[2].ai_backend, "echo")
        self.assertEqual(config.routes[2].ai_model_path, root / "models" / "tiny.gguf")
        self.assertEqual(config.routes[2].ai_model_name, "tiny-local")
        self.assertEqual(config.routes[2].ai_system_prompt, "Answer only about VeloxServer.")
        self.assertEqual(config.routes[2].ai_max_tokens, 128)
        self.assertEqual(config.routes[2].ai_temperature, 0.2)
        self.assertEqual(config.routes[2].ai_context_window, 2048)
        self.assertTrue(config.routes[2].ai_chat_enabled)
        self.assertTrue(config.routes[2].ai_api_enabled)


class AIErrorRepairTests(unittest.IsolatedAsyncioTestCase):
    async def test_ai_error_repair_writes_suggestion_without_applying(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
            repairer = AIErrorRepairer(
                ErrorRepairSettings(
                    enabled=True,
                    project_path=root,
                    log_path=root / "logs" / "ai-repair.log",
                    suggestions_path=root / ".veloxserver" / "repair-suggestions",
                    apply_patches=False,
                    context_files=(root / "pyproject.toml",),
                    cooldown_seconds=0,
                ),
                responder=lambda _prompt: json.dumps(
                    {
                        "summary": "Gunicorn socket missing",
                        "probable_cause": "The upstream socket path is wrong.",
                        "risk": "low",
                        "files": [{"path": "fixed.txt", "action": "create", "content": "created by repair\n"}],
                        "operator_steps": ["restart gunicorn"],
                    }
                ),
            )

            result = await repairer.handle(ErrorRepairEvent(status=502, message="Bad Gateway", method="GET", target="/"))

            self.assertIsNotNone(result)
            self.assertFalse((root / "fixed.txt").exists())
            self.assertTrue((root / "logs" / "ai-repair.log").exists())
            self.assertEqual(len(list((root / ".veloxserver" / "repair-suggestions").glob("repair-*.json"))), 1)

    async def test_ai_error_repair_can_apply_guarded_file_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repairer = AIErrorRepairer(
                ErrorRepairSettings(enabled=True, project_path=root, apply_patches=True, cooldown_seconds=0),
                responder=lambda _prompt: json.dumps(
                    {
                        "summary": "Create missing config",
                        "files": [{"path": "settings/app.conf", "action": "create", "content": "ok=true\n"}],
                    }
                ),
            )

            result = await repairer.handle(ErrorRepairEvent(status=500, message="Internal Server Error"))

            self.assertIsNotNone(result)
            self.assertEqual((root / "settings" / "app.conf").read_text(encoding="utf-8"), "ok=true\n")

    def test_repair_json_accepts_fenced_model_output(self) -> None:
        parsed = parse_repair_json('```json\n{"summary":"ok"}\n```')
        self.assertEqual(parsed["summary"], "ok")
        self.assertEqual(parsed["files"], [])


class AIDeploymentPlannerTests(unittest.TestCase):
    def test_detects_django_and_generates_repair_enabled_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "manage.py").write_text("# django\n", encoding="utf-8")
            (root / "static").mkdir()
            settings = DeploymentSettings(project_path=root, domain="example.test")

            profile = detect_project_profile(root, settings)
            plan = AIDeploymentPlanner(settings).build_plan()

            self.assertEqual(profile.kind, "django-gunicorn")
            self.assertIn("unix:/run/gunicorn.sock", plan.files["veloxserver.toml"])
            self.assertIn("[ai_error_repair]", plan.files["veloxserver.toml"])
            self.assertIn('hosts = ["example.test"]', plan.files["veloxserver.toml"])

    def test_ai_deploy_write_creates_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "public").mkdir()
            (root / "public" / "index.html").write_text("ok", encoding="utf-8")
            output = root / "out"
            planner = AIDeploymentPlanner(DeploymentSettings(project_path=root, output_dir=output))

            written = planner.write_plan(planner.build_plan())

            self.assertIn(output / "veloxserver.toml", written)
            self.assertTrue((output / "deployment-plan.json").exists())
            self.assertTrue((output / "scripts" / "run-veloxserver.sh").exists())


class UpstreamParsingTests(unittest.TestCase):
    def test_parse_unix_socket_upstream(self) -> None:
        upstream = parse_upstream("unix:/run/gunicorn.sock")
        self.assertEqual(upstream.unix_socket, Path("/run/gunicorn.sock"))
        self.assertEqual(upstream.authority, "localhost")


class SharedZoneTests(unittest.TestCase):
    def test_shared_rate_limit_uses_sqlite_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "zones.sqlite3"
            first = SharedZones(path, rate_limit_per_minute=1, rate_limit_burst=1, connection_limit=0, connection_limit_per_client=0)
            second = SharedZones(path, rate_limit_per_minute=1, rate_limit_burst=1, connection_limit=0, connection_limit_per_client=0)

            self.assertTrue(first.allow_request("client"))
            self.assertFalse(second.allow_request("client"))

    def test_native_core_reports_missing_library(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status = load_native_core("rust", Path(tmp))

        self.assertFalse(status.available)
        self.assertIn("not built", status.message)


class ReloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_reload_runtime_config_updates_routes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            first_root = base / "first"
            second_root = base / "second"
            first_root.mkdir()
            second_root.mkdir()
            (first_root / "index.html").write_bytes(b"first")
            (second_root / "index.html").write_bytes(b"second")
            config_path = base / "veloxserver.toml"
            config_path.write_text(
                """
[[routes]]
path = "/"
kind = "static"
root = "first"
""".strip(),
                encoding="utf-8",
            )
            app = VeloxServer(load_config(config_path))
            before = await send_request(
                app,
                b"GET / HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
            )

            config_path.write_text(
                """
[[routes]]
path = "/"
kind = "static"
root = "second"
""".strip(),
                encoding="utf-8",
            )
            self.assertTrue(app.reload_runtime_config())
            after = await send_request(
                app,
                b"GET / HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
            )

        self.assertTrue(before.endswith(b"first"))
        self.assertTrue(after.endswith(b"second"))


if __name__ == "__main__":
    unittest.main()


def make_hs256_jwt(payload: dict[str, object], secret: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    header_b64 = base64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_b64 = base64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    signature = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{header_b64}.{payload_b64}.{base64url(signature)}"


def make_rs256_jwt(payload: dict[str, object], private_key: rsa.RSAPrivateKey, kid: str) -> str:
    header = {"alg": "RS256", "typ": "JWT", "kid": kid}
    header_b64 = base64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_b64 = base64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{header_b64}.{payload_b64}.{base64url(signature)}"


def rsa_public_jwk(public_key: rsa.RSAPublicKey, kid: str) -> dict[str, str]:
    numbers = public_key.public_numbers()
    return {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": kid,
        "n": int_base64url(numbers.n),
        "e": int_base64url(numbers.e),
    }


def int_base64url(value: int) -> str:
    length = max(1, (value.bit_length() + 7) // 8)
    return base64url(value.to_bytes(length, "big"))


def base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")
