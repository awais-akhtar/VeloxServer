# Native Core

The Rust native core lives in `native/rust`. The Python package can look for a compiled shared library in these places:

- a configured `native_core_path`
- `src/veloxserver/native_libs` inside an installed wheel
- `native/rust/target/release` during local development

Expected library names:

- Windows: `veloxcore.dll`
- macOS: `libveloxcore.dylib`
- Linux: `libveloxcore.so`

## Build Locally

Linux and macOS:

```bash
cd native/rust
cargo build --release
cd ../..
veloxserver --native-core rust --native-core-path native/rust/target/release --root public
```

Windows PowerShell:

```powershell
Set-Location native\rust
cargo build --release
Set-Location ..\..
veloxserver --native-core rust --native-core-path native\rust\target\release --root public
```

The current Rust library exposes native ABI hooks for:

- `GET` and `HEAD`
- safe path resolution
- `index.html` handling
- response headers and body bytes
- default security headers when enabled
- HTTP/1.1 request-line/header parsing as JSON
- proxy cache key rendering for native-enabled routes

Python still handles the socket accept loop, TLS, HTTP/2, HTTP/3, auth, compression, precompressed assets, conditional requests, custom error pages, most proxy I/O, metrics, and logging. VeloxServer uses the Rust path only when `native_core = "rust"` is selected and the compiled shared library is available.

The next native milestones are:

1. perform metadata/open-file cache lookups in Rust
2. stream large files without reading the whole body into memory
3. move reverse-proxy response planning and upstream body framing into Rust
4. add sanitizer-backed native fuzz targets
5. benchmark the Rust path with the included VeloxServer benchmark tools

## Wheel Packaging

Wheels can include native libraries by copying the built library into `src/veloxserver/native_libs` before `python -m build`. `pyproject.toml` already includes that directory as package data.

The `wheels` GitHub Actions workflow builds the Rust library on Linux, macOS, and Windows, copies it into `src/veloxserver/native_libs`, and builds platform-tagged wheels.
