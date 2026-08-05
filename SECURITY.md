# Security

VeloxServer is alpha software. Do not expose it to hostile internet traffic without a reverse proxy, firewall policy, and monitoring until it has gone through independent review.

## Supported Reporting Path

Report security issues privately to the project owner before public disclosure. Include:

- affected version or commit
- exact config needed to reproduce
- request bytes or client command
- expected result and actual result

## Current Hardening

- path traversal protection for static files
- request header and body size limits
- optional TLS minimum version and cipher controls
- optional client certificate verification and CA trust configuration
- optional SNI certificate routing
- optional OCSP response loading where Python/OpenSSL supports it
- basic auth, HS256 JWT, and RS256 JWKS/OIDC route guards
- issuer, audience, and required-claim checks for bearer tokens
- external auth URLs and auth subrequests
- plugin hook for custom policy checks
- WAF and auth plugin hooks
- native dynamic module request hook ABI
- per-client rate limiting and connection limiting
- optional SQLite-backed shared zones across worker processes
- default security headers
- fuzz target for request parsing
- old/new generation health header for safer replacement rollouts
- optional Rust static-response path for eligible routes
- disk-backed proxy cache with purge, lock, stale, and size controls
- advanced rewrite conditions for method, host, header, and query

## Not Yet Claimed

- independent security audit
- long-running internet exposure history
- complete WAF ecosystem comparable to mature third-party modules
- verified OCSP behavior on every supported operating system
- verified HTTP/3 compatibility across all major browsers and mobile networks
- TLS session cache depth and ticket key rotation beyond exposed stdlib APIs
- formal memory-safety review of the native core
- proven kernel-level direct I/O or io_uring behavior across platforms
- independent third-party audit report
