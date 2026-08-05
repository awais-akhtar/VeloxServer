# VeloxServer Native Core

This directory is the beginning of the native performance core. The Python server still runs the product surface today. The native core is meant to take over the hot path later: parsing, routing, file metadata cache, sendfile/io_uring/kqueue style backends, and stream forwarding.

Current status:

- Rust crate scaffold exists.
- Python can locate and load the compiled shared library through `ctypes`.
- The native library can build complete HTTP/1.1 static responses for eligible routes.
- Python still owns the socket loop, TLS, HTTP/2, HTTP/3, auth, compression, logging, and proxying.
- Building requires a Rust toolchain.

The public package keeps `native_core = "python"` by default. Use `native_core = "rust"` only after building the shared library and benchmarking it in your environment.
