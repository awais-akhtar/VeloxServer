# Security Audit Plan

This is the audit package for VeloxServer. It is not a substitute for an independent audit; it defines the scope and repeatable checks needed before one.

## Scope

- HTTP/1.1 parser and body handling
- static path resolution
- reverse proxy request and response handling
- proxy cache disk layout and purge behavior
- auth paths: basic auth, JWT claims, external auth, auth subrequests, plugin auth
- stream proxy TCP/UDP behavior
- TLS configuration surface
- dynamic Python and native module loading

## Required Checks

- run unit tests
- run `python fuzz/run_fuzz_campaign.py`
- run sanitizer-backed native fuzzing when Rust toolchains are installed
- run `python tools/tls_probe_matrix.py` against every TLS listener
- run `python tools/http2_stress_matrix.py` against every HTTP/2 listener
- run HTTP/3 client matrix when an HTTP/3 endpoint is active
- run repeated benchmark matrix and archive raw command output
- run proxy cache manager scans for every configured disk cache
- review every plugin/native module before enabling it in production
- verify file permissions for TLS keys, cache directories, logs, and shared-zone DBs

## Not Yet Completed

- independent third-party review
- sanitizer-backed Rust native-core fuzzing
- long-duration internet exposure test
- browser/mobile HTTP/3 compatibility lab
- formal threat model sign-off
