# HTTP/3 Compatibility

VeloxServer can run HTTP/3 through `aioquic` when `http3 = true` and TLS certificate files are configured.

Minimum local smoke check:

```bash
veloxserver --config examples/veloxserver.toml
```

Then test with a client that supports HTTP/3:

```bash
curl --http3-only --insecure https://127.0.0.1:8080/
```

VeloxServer also includes a JSON-producing smoke harness:

Linux and macOS:

```bash
python tools/http3_compat_matrix.py --insecure https://127.0.0.1:8080/
```

Windows PowerShell:

```powershell
python tools\http3_compat_matrix.py --insecure https://127.0.0.1:8080/
```

Production compatibility is not a one-time switch. Before claiming broad browser/client support, run:

- Chrome, Edge, Firefox, Safari
- curl with ngtcp2/quiche backend
- mobile networks with packet loss
- large streaming responses
- connection migration disabled and enabled
- TLS certificate renewal and server restart scenarios

Record the exact client versions with every compatibility report.

The harness proves only the clients actually present on the machine running it. Browser and mobile compatibility still need real devices or CI runners with those clients installed.

For a release report, store the JSON output from this tool next to:

- client version output
- network type
- packet-loss or proxy settings
- certificate details
- VeloxServer config and commit/tag
