"""FRIDAY's own home certificate, so phones and laptops connect over https.

Phones only allow the microphone on secure (https) pages, and https also
encrypts everything between the device and this computer.

How it works: FRIDAY makes a small certificate authority of its own once
("FRIDAY home"), which the owner installs on each device. FRIDAY then issues
this computer's certificate from it, and renews it by itself (for example when
the router gives the computer a new address); devices need nothing new.

The authority is *name-constrained*: it can only vouch for names ending in
``.local`` and for home-network addresses (192.168.x.x, 10.x.x.x, 172.16-31.x.x).
Even if its key were stolen, it could not be used to impersonate a bank or any
other website on the internet. Its key stays in a folder only the owner's user
can read.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

HOME_NETWORKS = (
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
)
CA_NAME = "FRIDAY home"
_SERVER_DAYS = 397  # browsers distrust longer server certificates
_RENEW_DAYS = 30


@dataclass(frozen=True, slots=True)
class HomeCertificate:
    ca_file: Path  # what devices install
    cert_file: Path  # this computer's certificate (with the authority after it)
    key_file: Path
    fingerprint: str  # of the authority, to compare on the phone


def _write(path: Path, data: bytes, private: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _key_bytes(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def fingerprint(cert: x509.Certificate) -> str:
    digest = cert.fingerprint(hashes.SHA256()).hex().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def _load_ca(folder: Path) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    ca_file, ca_key_file = folder / "friday-home-ca.crt", folder / "friday-home-ca.key"
    if ca_file.is_file() and ca_key_file.is_file():
        cert = x509.load_pem_x509_certificate(ca_file.read_bytes())
        key = serialization.load_pem_private_key(ca_key_file.read_bytes(), password=None)
        assert isinstance(key, ec.EllipticCurvePrivateKey)
        return cert, key
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CA_NAME)])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.NameConstraints(
                permitted_subtrees=[
                    x509.DNSName("local"),
                    *(x509.IPAddress(n) for n in HOME_NETWORKS),
                ],
                excluded_subtrees=None,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    _write(ca_key_file, _key_bytes(key), private=True)
    _write(ca_file, cert.public_bytes(serialization.Encoding.PEM), private=False)
    return cert, key


def _names_of(cert: x509.Certificate) -> tuple[set[str], set[str]]:
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return set(), set()
    return set(san.get_values_for_type(x509.DNSName)), {
        str(a) for a in san.get_values_for_type(x509.IPAddress)
    }


def _at_home(ip: str) -> bool:
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(address in n for n in HOME_NETWORKS)


def ensure(folder: Path, names: list[str], ips: list[str]) -> HomeCertificate:
    """The authority (made once) and a current certificate for these names."""
    names = sorted({n.lower() for n in names if n.lower().endswith(".local")})
    ips = sorted({ip for ip in ips if _at_home(ip)})
    ca, ca_key = _load_ca(folder)
    cert_file, key_file = folder / "friday-server.crt", folder / "friday-server.key"
    if cert_file.is_file() and key_file.is_file():
        current = x509.load_pem_x509_certificates(cert_file.read_bytes())[0]
        left = current.not_valid_after_utc - dt.datetime.now(dt.UTC)
        same_issuer = current.issuer == ca.subject
        if same_issuer and left.days > _RENEW_DAYS and _names_of(current) == (set(names), set(ips)):
            return HomeCertificate(
                folder / "friday-home-ca.crt", cert_file, key_file, fingerprint(ca)
            )
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.UTC)
    first = names[0] if names else (ips[0] if ips else "friday.local")
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, first)]))
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=_SERVER_DAYS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName(n) for n in names]
                + [x509.IPAddress(ipaddress.ip_address(ip)) for ip in ips]
            ),
            critical=False,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM) + ca.public_bytes(
        serialization.Encoding.PEM
    )
    _write(key_file, _key_bytes(key), private=True)
    _write(cert_file, pem, private=False)
    return HomeCertificate(folder / "friday-home-ca.crt", cert_file, key_file, fingerprint(ca))


def short_code(fp: str) -> str:
    """The first characters of the fingerprint, easy to compare by eye."""
    return fp.replace(":", "")[:8]


def ca_der(ca_file: Path) -> bytes:
    """The authority as a .crt file Android's installer accepts."""
    cert = x509.load_pem_x509_certificate(ca_file.read_bytes())
    return cert.public_bytes(serialization.Encoding.DER)
