# Auth And WAF

Implemented auth controls:

- basic auth
- HS256 bearer JWT validation
- RS256 bearer JWT validation from local JWKS files or cached JWKS URLs
- issuer, audience, and required-claim checks for OIDC-style JWTs
- external auth URL checks
- internal auth subrequests
- Python auth plugins
- native request policy modules

OIDC can use `jwt_jwks_url` or `jwt_jwks_file` with `jwt_issuer`, `jwt_audience`, and `jwt_required_claims`. LDAP is supported through the external-auth/plugin bridge pattern. Run a hardened LDAP gateway next to VeloxServer, then configure `external_auth_url` or use `examples/plugins/ldap_bridge_auth.py`.

Implemented WAF controls:

- regex path blocks
- `on_waf_request` plugin hook
- example ModSecurity-style regex plugin at `examples/plugins/modsec_style_waf.py`
- native module ABI for in-process request policy checks

This is not yet a ModSecurity-compatible rule engine. It is a module ecosystem foundation that can host WAF logic without copying another project.
