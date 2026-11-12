"""TLS for the hub.

By default the hub makes its own self-signed certificate on first start and
advertises its SHA-256 fingerprint over Bonjour. The app pins that fingerprint
when it enrolls (trust on first use, on home Wi-Fi), so later connections,
including over the tunnel, are checked against it. If Dylan puts the hub behind
the reverse proxy with a real certificate, set tls_cert/tls_key instead.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import socket
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from .config import Config, data_dir


def ensure_tls(cfg: Config) -> tuple[Path, Path]:
    if cfg.hub.tls_cert and cfg.hub.tls_key:
        return Path(cfg.hub.tls_cert), Path(cfg.hub.tls_key)
    d = data_dir() / "tls"
    cert, key = d / "cert.pem", d / "key.pem"
    if cert.exists() and key.exists():
        return cert, key
    d.mkdir(parents=True, exist_ok=True)
    priv = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cfg.hub.name + " Hub")])
    now = dt.datetime.now(dt.timezone.utc)
    sans: list[x509.GeneralName] = [x509.DNSName("thomascreek-hub.local"), x509.DNSName(socket.gethostname())]
    sans.append(x509.IPAddress(ipaddress.ip_address("127.0.0.1")))
    c = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(priv.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .sign(priv, hashes.SHA256())
    )
    key.write_bytes(priv.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    key.chmod(0o600)
    cert.write_bytes(c.public_bytes(serialization.Encoding.PEM))
    return cert, key


def spki_fingerprint(cert_path: Path) -> str:
    """SHA-256 of the certificate's public key (what the app pins)."""
    c = x509.load_pem_x509_certificate(cert_path.read_bytes())
    spki = c.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(spki).hexdigest()
