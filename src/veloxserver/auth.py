from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class JwtClaims:
    valid: bool
    payload: dict[str, object]


def verify_hs256_jwt(token: str, secret: str) -> JwtClaims:
    try:
        header_b64, payload_b64, signature_b64 = token.split(".")
        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        expected = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
        actual = base64url_decode(signature_b64)
        if not hmac.compare_digest(expected, actual):
            return JwtClaims(False, {})
        header = json.loads(base64url_decode(header_b64))
        if header.get("alg") != "HS256":
            return JwtClaims(False, {})
        payload = json.loads(base64url_decode(payload_b64))
        if has_invalid_time_claims(payload):
            return JwtClaims(False, {})
        return JwtClaims(True, payload)
    except Exception:
        return JwtClaims(False, {})


def verify_rs256_jwt(token: str, jwks: dict[str, Any]) -> JwtClaims:
    try:
        header_b64, payload_b64, signature_b64 = token.split(".")
        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        header = json.loads(base64url_decode(header_b64))
        if header.get("alg") != "RS256":
            return JwtClaims(False, {})
        jwk = select_rsa_jwk(jwks, str(header.get("kid", "")))
        if jwk is None:
            return JwtClaims(False, {})

        from cryptography.hazmat.primitives.asymmetric import padding, rsa
        from cryptography.hazmat.primitives import hashes

        public_numbers = rsa.RSAPublicNumbers(
            e=jwk_int(str(jwk["e"])),
            n=jwk_int(str(jwk["n"])),
        )
        public_key = public_numbers.public_key()
        public_key.verify(
            base64url_decode(signature_b64),
            signing_input,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        payload = json.loads(base64url_decode(payload_b64))
        if has_invalid_time_claims(payload):
            return JwtClaims(False, {})
        return JwtClaims(True, payload)
    except Exception:
        return JwtClaims(False, {})


def claims_match(
    claims: dict[str, object],
    issuer: str | None = None,
    audience: str | None = None,
    required_claims: tuple[tuple[str, str], ...] = (),
) -> bool:
    if issuer is not None and claims.get("iss") != issuer:
        return False
    if audience is not None and not audience_matches(claims.get("aud"), audience):
        return False
    for name, expected in required_claims:
        if str(claims.get(name, "")) != expected:
            return False
    return True


def audience_matches(value: Any, expected: str) -> bool:
    if isinstance(value, str):
        return value == expected
    if isinstance(value, list):
        return expected in {str(item) for item in value}
    return False


def has_invalid_time_claims(payload: dict[str, object]) -> bool:
    return is_expired(payload) or is_not_yet_valid(payload)


def is_expired(payload: dict[str, object]) -> bool:
    exp = payload.get("exp")
    if exp is None:
        return False
    try:
        return float(exp) <= time.time()
    except (TypeError, ValueError):
        return True


def is_not_yet_valid(payload: dict[str, object]) -> bool:
    nbf = payload.get("nbf")
    if nbf is None:
        return False
    try:
        return float(nbf) > time.time()
    except (TypeError, ValueError):
        return True


def select_rsa_jwk(jwks: dict[str, Any], kid: str) -> dict[str, Any] | None:
    keys = jwks.get("keys", [])
    if not isinstance(keys, list):
        return None
    for key in keys:
        if not isinstance(key, dict):
            continue
        if key.get("kty") != "RSA":
            continue
        if key.get("use") not in {None, "sig"}:
            continue
        if kid and key.get("kid") != kid:
            continue
        if "n" in key and "e" in key:
            return key
    return None


def jwk_int(value: str) -> int:
    return int.from_bytes(base64url_decode(value), "big")


def base64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)
