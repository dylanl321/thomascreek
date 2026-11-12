"""Device keys, challenge signatures and hub access tokens.

Each phone holds a P-256 key in its Secure Enclave and proves itself by signing
a one-time nonce from the hub. A second Secure Enclave key, created with
biometry required, signs secure-tier actions, so Face ID is enforced by the
phone's hardware and checked here, not merely shown in the UI.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from typing import Any

import jwt
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

ACCESS_AUDIENCE = "thomascreek-app"


class CryptoError(ValueError):
    pass


def load_p256_public_key(b64: str) -> ec.EllipticCurvePublicKey:
    """Accept X9.63 uncompressed points (SecKeyCopyExternalRepresentation) or DER SPKI."""
    try:
        raw = base64.b64decode(b64, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise CryptoError("public key is not base64") from exc
    try:
        if len(raw) == 65 and raw[0] == 0x04:
            return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
        key = serialization.load_der_public_key(raw)
    except Exception as exc:  # noqa: BLE001
        raise CryptoError("public key is not a P-256 key") from exc
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise CryptoError("public key is not a P-256 key")
    return key


def normalize_public_key(b64: str) -> str:
    """Canonical X9.63 form, so the same key always maps to one device row."""
    key = load_p256_public_key(b64)
    raw = key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return base64.b64encode(raw).decode()


def verify_signature(public_key_b64: str, message: bytes, signature_b64: str) -> bool:
    """ES256 verify. Accepts DER (what SecKeyCreateSignature returns) or raw r||s."""
    key = load_p256_public_key(public_key_b64)
    try:
        sig = base64.b64decode(signature_b64, validate=True)
    except Exception:  # noqa: BLE001
        return False
    if len(sig) == 64:
        sig = encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
    try:
        key.verify(sig, message, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError):
        return False


def new_nonce() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(24)).decode().rstrip("=")


def body_digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def confirm_message(nonce: str, body: bytes) -> bytes:
    return f"{nonce}.{body_digest(body)}".encode()


class TokenIssuer:
    def __init__(self, signing_key: str, issuer: str, ttl_s: int):
        self._key = signing_key
        self._iss = issuer
        self._ttl = ttl_s

    def issue(self, device_id: str, person_id: str | None, role: str) -> tuple[str, float]:
        now = time.time()
        exp = now + self._ttl
        token = jwt.encode(
            {
                "iss": self._iss,
                "aud": ACCESS_AUDIENCE,
                "sub": device_id,
                "per": person_id,
                "role": role,
                "iat": int(now),
                "exp": int(exp),
                "jti": secrets.token_hex(8),
            },
            self._key,
            algorithm="HS256",
        )
        return token, exp

    def issue_scoped(self, claims: dict[str, Any], ttl_s: int) -> tuple[str, float]:
        """Short-lived passes (camera stream, web page cookie)."""
        now = time.time()
        exp = now + ttl_s
        token = jwt.encode(
            {"iss": self._iss, "aud": claims.pop("aud"), "iat": int(now), "exp": int(exp), **claims},
            self._key,
            algorithm="HS256",
        )
        return token, exp

    def verify(self, token: str, audience: str = ACCESS_AUDIENCE) -> dict[str, Any]:
        try:
            return jwt.decode(token, self._key, algorithms=["HS256"], audience=audience, issuer=self._iss)
        except jwt.PyJWTError as exc:
            raise CryptoError("invalid or expired token") from exc
