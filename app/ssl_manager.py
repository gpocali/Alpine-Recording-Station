"""SSL/TLS certificate management for the Remote Producer web interface.

Provides default self-signed X.509 v3 certificate generation (with SAN entries for
localhost and LAN IPv4 addresses) and seamless replacement with CA-issued certificates
via file paths or the /storage/ssl Samba drop directory.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import ipaddress
import logging
import os
import secrets
import shutil
import ssl
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from app.network_info import get_lan_ip_addresses

logger = logging.getLogger(__name__)

DEFAULT_CERT_FILENAME = "producer-cert.pem"
DEFAULT_KEY_FILENAME = "producer-key.pem"


@dataclass
class CertificateInfo:
    """Metadata and validation status for the active SSL certificate."""

    cert_path: Path
    key_path: Path
    exists: bool = False
    valid: bool = False
    is_self_signed: bool = True
    subject: str = ""
    issuer: str = ""
    not_before: str = ""
    not_after: str = ""
    fingerprint_sha256: str = ""
    san_entries: List[str] = field(default_factory=list)
    error: str = ""

    @property
    def status_summary(self) -> str:
        if not self.exists or not self.valid:
            return f"Invalid / Missing ({self.error or 'No certificate'})"
        kind = "Self-Signed Default Certificate" if self.is_self_signed else "Issued / Custom Certificate"
        exp = f" • Expires: {self.not_after}" if self.not_after else ""
        return f"{kind}{exp}"


# -----------------------------------------------------------------------------
# Pure-Python ASN.1 DER + RSA-2048 X.509v3 Self-Signed Certificate Generator
# -----------------------------------------------------------------------------
def _der_len(length: int) -> bytes:
    if length < 0x80:
        return bytes([length])
    encoded = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(encoded)]) + encoded


def _der_tlv(tag: int, content: bytes) -> bytes:
    return bytes([tag]) + _der_len(len(content)) + content


def _der_integer(value: int) -> bytes:
    if value == 0:
        return _der_tlv(0x02, b"\x00")
    bit_len = value.bit_length()
    byte_len = (bit_len + 7) // 8
    raw = value.to_bytes(byte_len, "big")
    if raw[0] & 0x80:
        raw = b"\x00" + raw
    return _der_tlv(0x02, raw)


def _der_sequence(items: Sequence[bytes]) -> bytes:
    return _der_tlv(0x30, b"".join(items))


def _der_set(items: Sequence[bytes]) -> bytes:
    return _der_tlv(0x31, b"".join(items))


def _der_oid(oid_nodes: Sequence[int]) -> bytes:
    if len(oid_nodes) < 2:
        raise ValueError("OID requires at least 2 nodes")
    out = bytearray([40 * oid_nodes[0] + oid_nodes[1]])
    for node in oid_nodes[2:]:
        if node == 0:
            out.append(0)
            continue
        chunks: List[int] = []
        v = node
        while v > 0:
            chunks.append(v & 0x7F)
            v >>= 7
        for chk in reversed(chunks[1:]):
            out.append(0x80 | chk)
        out.append(chunks[0])
    return _der_tlv(0x06, bytes(out))


def _der_utf8_string(text: str) -> bytes:
    return _der_tlv(0x0C, text.encode("utf-8"))


def _der_utc_time(dt: datetime.datetime) -> bytes:
    utc_dt = dt.astimezone(datetime.timezone.utc)
    return _der_tlv(0x17, utc_dt.strftime("%y%m%d%H%M%SZ").encode("ascii"))


def _der_bit_string(raw: bytes) -> bytes:
    return _der_tlv(0x03, b"\x00" + raw)


def _der_octet_string(raw: bytes) -> bytes:
    return _der_tlv(0x04, raw)


def _is_probable_prime(n: int, rounds: int = 16) -> bool:
    if n < 2:
        return False
    small_primes = (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47, 53)
    for sp in small_primes:
        if n == sp:
            return True
        if n % sp == 0:
            return False

    d = n - 1
    s = 0
    while (d & 1) == 0:
        d >>= 1
        s += 1

    for _ in range(rounds):
        a = 2 + secrets.randbelow(n - 3)
        x = pow(a, d, n)
        if x == 1 or x == n - 1:
            continue
        for _ in range(s - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _generate_prime(bits: int) -> int:
    while True:
        candidate = secrets.randbits(bits)
        candidate |= (1 << (bits - 1)) | (1 << (bits - 2)) | 1
        if _is_probable_prime(candidate):
            return candidate


def _encode_pem(label: str, der_bytes: bytes) -> str:
    b64 = base64.b64encode(der_bytes).decode("ascii")
    lines = [b64[i : i + 64] for i in range(0, len(b64), 64)]
    body = "\n".join(lines)
    return f"-----BEGIN {label}-----\n{body}\n-----END {label}-----\n"


def generate_self_signed_pem_pair(
    common_name: str = "Alpine Recording Station",
    organization: str = "Alpine Recording Station",
    san_ips: Optional[Sequence[str]] = None,
    san_dns: Optional[Sequence[str]] = None,
    validity_days: int = 825,
) -> Tuple[str, str]:
    """Generate a 2048-bit RSA self-signed X.509 v3 certificate and private key in PEM format."""
    e = 65537
    while True:
        p = _generate_prime(1024)
        q = _generate_prime(1024)
        if p == q:
            continue
        n = p * q
        phi = (p - 1) * (q - 1)
        try:
            d = pow(e, -1, phi)
            break
        except ValueError:
            continue

    dp = d % (p - 1)
    dq = d % (q - 1)
    qinv = pow(q, -1, p)

    # PKCS#1 RSAPrivateKey DER
    rsa_priv_der = _der_sequence(
        [
            _der_integer(0),
            _der_integer(n),
            _der_integer(e),
            _der_integer(d),
            _der_integer(p),
            _der_integer(q),
            _der_integer(dp),
            _der_integer(dq),
            _der_integer(qinv),
        ]
    )

    # SubjectPublicKeyInfo DER
    rsa_pub_der = _der_sequence([_der_integer(n), _der_integer(e)])
    oid_rsa_encryption = _der_oid([1, 2, 840, 113549, 1, 1, 1])
    der_null = _der_tlv(0x05, b"")
    spki_der = _der_sequence(
        [
            _der_sequence([oid_rsa_encryption, der_null]),
            _der_bit_string(rsa_pub_der),
        ]
    )

    # Name: O=organization, CN=common_name
    oid_org = _der_oid([2, 5, 4, 10])
    oid_cn = _der_oid([2, 5, 4, 3])
    name_der = _der_sequence(
        [
            _der_set([_der_sequence([oid_org, _der_utf8_string(organization)])]),
            _der_set([_der_sequence([oid_cn, _der_utf8_string(common_name)])]),
        ]
    )

    now = datetime.datetime.now(datetime.timezone.utc)
    not_before = now - datetime.timedelta(minutes=5)
    not_after = now + datetime.timedelta(days=max(1, int(validity_days)))
    validity_der = _der_sequence([_der_utc_time(not_before), _der_utc_time(not_after)])

    # X.509 v3 Extensions: basicConstraints (CA:FALSE), keyUsage, extendedKeyUsage, subjectAltName
    dns_list = list(dict.fromkeys(["localhost", *(san_dns or [])]))
    ip_list = list(dict.fromkeys(["127.0.0.1", *(san_ips or [])]))

    san_general_names: List[bytes] = []
    for dns_name in dns_list:
        if dns_name:
            # GeneralName dNSName [2] IMPLICIT IA5String (0x82)
            san_general_names.append(_der_tlv(0x82, dns_name.encode("ascii", errors="ignore")))
    for ip_str in ip_list:
        try:
            ip_obj = ipaddress.ip_address(ip_str)
            # GeneralName iPAddress [7] IMPLICIT OCTET STRING (0x87)
            san_general_names.append(_der_tlv(0x87, ip_obj.packed))
        except ValueError:
            continue

    ext_basic_constraints = _der_sequence(
        [
            _der_oid([2, 5, 29, 19]),
            _der_tlv(0x01, b"\xff"),  # critical = TRUE
            _der_octet_string(_der_sequence([])),  # CA = FALSE
        ]
    )
    # ExtendedKeyUsage: serverAuth (1.3.6.1.5.5.7.3.1)
    ext_eku = _der_sequence(
        [
            _der_oid([2, 5, 29, 37]),
            _der_octet_string(_der_sequence([_der_oid([1, 3, 6, 1, 5, 5, 7, 3, 1])])),
        ]
    )
    ext_san = _der_sequence(
        [
            _der_oid([2, 5, 29, 17]),
            _der_octet_string(_der_sequence(san_general_names)),
        ]
    )
    extensions_der = _der_tlv(
        0xA3,  # [3] EXPLICIT Extensions
        _der_sequence([ext_basic_constraints, ext_eku, ext_san]),
    )

    # sha256WithRSAEncryption (1.2.840.113549.1.1.11)
    sig_alg_der = _der_sequence([_der_oid([1, 2, 840, 113549, 1, 1, 11]), der_null])
    serial_num = secrets.randbits(120) | (1 << 119)

    tbs_cert_der = _der_sequence(
        [
            _der_tlv(0xA0, _der_integer(2)),  # version v3 (2)
            _der_integer(serial_num),
            sig_alg_der,
            name_der,  # issuer
            validity_der,
            name_der,  # subject
            spki_der,
            extensions_der,
        ]
    )

    # PKCS#1 v1.5 SHA-256 signature over tbs_cert_der
    digest = hashlib.sha256(tbs_cert_der).digest()
    digest_info_prefix = bytes.fromhex("3031300d060960864801650304020105000420")
    t_bytes = digest_info_prefix + digest
    k_bytes = 256  # 2048-bit modulus
    ps_len = k_bytes - len(t_bytes) - 3
    em = b"\x00\x01" + (b"\xff" * ps_len) + b"\x00" + t_bytes
    sig_int = pow(int.from_bytes(em, "big"), d, n)
    sig_bytes = sig_int.to_bytes(k_bytes, "big")

    cert_der = _der_sequence([tbs_cert_der, sig_alg_der, _der_bit_string(sig_bytes)])

    cert_pem = _encode_pem("CERTIFICATE", cert_der)
    key_pem = _encode_pem("RSA PRIVATE KEY", rsa_priv_der)
    return cert_pem, key_pem


class SslCertificateManager:
    """Manages default and custom/issued SSL certificates for the Remote Producer HTTPS server."""

    def __init__(self, config_manager) -> None:
        self.config_manager = config_manager

    @property
    def storage_ssl_dir(self) -> Path:
        return self.config_manager.storage_dir / ".alpine-recorder" / "ssl"

    @property
    def etc_ssl_dir(self) -> Path:
        return self.config_manager.etc_config_dir / "ssl"

    @property
    def samba_drop_ssl_dir(self) -> Path:
        """Public directory inside /storage where admins can drop issued SSL certs over Samba."""
        return self.config_manager.storage_dir / "ssl"

    def resolve_cert_and_key_paths(self) -> Tuple[Path, Path]:
        """Return the active (cert_path, key_path), respecting custom paths in settings if valid."""
        s = self.config_manager.settings
        custom_cert = getattr(s, "ssl_cert_path", "").strip()
        custom_key = getattr(s, "ssl_key_path", "").strip()

        if custom_cert and custom_key:
            cp = Path(custom_cert)
            kp = Path(custom_key)
            if cp.is_file() and kp.is_file():
                return cp, kp

        storage_cert = self.storage_ssl_dir / DEFAULT_CERT_FILENAME
        storage_key = self.storage_ssl_dir / DEFAULT_KEY_FILENAME
        etc_cert = self.etc_ssl_dir / DEFAULT_CERT_FILENAME
        etc_key = self.etc_ssl_dir / DEFAULT_KEY_FILENAME

        if storage_cert.is_file() and storage_key.is_file():
            return storage_cert, storage_key
        if etc_cert.is_file() and etc_key.is_file():
            return etc_cert, etc_key
        return storage_cert, storage_key

    def ensure_drop_dir_readme(self) -> None:
        """Create /storage/ssl/README-SSL.txt so users can easily drop in issued certificates."""
        try:
            self.samba_drop_ssl_dir.mkdir(parents=True, exist_ok=True)
            readme = self.samba_drop_ssl_dir / "README-SSL.txt"
            if not readme.is_file():
                readme.write_text(
                    "Alpine Recording Station - Custom / Issued SSL Certificate Drop Folder\n"
                    "======================================================================\n\n"
                    "To replace the default self-signed SSL certificate with an issued\n"
                    "certificate (e.g., from Let's Encrypt or your organization's CA):\n\n"
                    "1. Copy your PEM certificate (fullchain.pem, cert.pem, or producer-cert.pem)\n"
                    "   and unencrypted PEM private key (privkey.pem, key.pem, or producer-key.pem)\n"
                    "   into this folder (/storage/ssl).\n"
                    "2. In the Alpine Recording Station UI, open 'Remote Producer' (or Settings)\n"
                    "   and click 'Import Certificate from /storage/ssl' (or restart the station).\n",
                    encoding="utf-8",
                )
        except OSError:
            pass

    def ensure_certificates(self, force_regenerate: bool = False) -> Tuple[Path, Path]:
        """Ensure valid SSL certificate and key files exist, generating a default self-signed pair if needed."""
        self.ensure_drop_dir_readme()
        cert_path, key_path = self.resolve_cert_and_key_paths()

        if not force_regenerate and cert_path.is_file() and key_path.is_file():
            if self.validate_pair(cert_path, key_path)[0]:
                self._sync_to_mirror_dirs(cert_path, key_path)
                return cert_path, key_path

        # Check if user placed certificates in /storage/ssl drop folder
        if not force_regenerate:
            ok, _msg = self.import_from_drop_dir()
            if ok:
                return self.resolve_cert_and_key_paths()

        return self.generate_default_self_signed()

    def generate_default_self_signed(self) -> Tuple[Path, Path]:
        """Generate a fresh self-signed certificate including current LAN IPs and save to persistent paths."""
        lan_ips = get_lan_ip_addresses()
        cert_pem = ""
        key_pem = ""

        # Try openssl CLI first if available on POSIX/Alpine
        openssl_bin = shutil.which("openssl") if os.name == "posix" else None
        if openssl_bin:
            try:
                cert_pem, key_pem = self._generate_via_openssl_cli(openssl_bin, lan_ips)
            except Exception as exc:
                logger.debug("openssl CLI generation skipped, using built-in generator: %s", exc)

        if not cert_pem or not key_pem:
            cert_pem, key_pem = generate_self_signed_pem_pair(
                common_name="Alpine Recording Station",
                organization="Alpine Recording Station",
                san_ips=lan_ips,
                san_dns=["localhost", "alpine-recorder.local"],
            )

        self._write_pem_to_persistent_dirs(cert_pem, key_pem)
        # Clear any broken custom overrides so default paths are active
        self.config_manager.settings.ssl_cert_path = ""
        self.config_manager.settings.ssl_key_path = ""
        self.config_manager.save()
        return self.resolve_cert_and_key_paths()

    @staticmethod
    def _generate_via_openssl_cli(openssl_bin: str, lan_ips: Sequence[str]) -> Tuple[str, str]:
        import tempfile

        san_parts = ["DNS:localhost", "DNS:alpine-recorder.local", "IP:127.0.0.1"]
        for ip in lan_ips:
            if ip and ip not in ("127.0.0.1",):
                san_parts.append(f"IP:{ip}")
        san_str = ",".join(san_parts)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            c_path = tmp / "cert.pem"
            k_path = tmp / "key.pem"
            cnf_path = tmp / "openssl.cnf"
            cnf_path.write_text(
                "[req]\n"
                "distinguished_name = req_dn\n"
                "x509_extensions = v3_req\n"
                "prompt = no\n\n"
                "[req_dn]\n"
                "O = Alpine Recording Station\n"
                "CN = Alpine Recording Station\n\n"
                "[v3_req]\n"
                "basicConstraints = critical,CA:FALSE\n"
                "extendedKeyUsage = serverAuth\n"
                f"subjectAltName = {san_str}\n",
                encoding="utf-8",
            )
            res = subprocess.run(
                [
                    openssl_bin,
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-sha256",
                    "-days",
                    "825",
                    "-config",
                    str(cnf_path),
                    "-extensions",
                    "v3_req",
                    "-keyout",
                    str(k_path),
                    "-out",
                    str(c_path),
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            )
            _ = res
            return c_path.read_text(encoding="utf-8"), k_path.read_text(encoding="utf-8")

    def _write_pem_to_persistent_dirs(self, cert_pem: str, key_pem: str) -> None:
        for ssl_dir in (self.storage_ssl_dir, self.etc_ssl_dir):
            try:
                ssl_dir.mkdir(parents=True, exist_ok=True)
                c_file = ssl_dir / DEFAULT_CERT_FILENAME
                k_file = ssl_dir / DEFAULT_KEY_FILENAME
                c_tmp = c_file.with_suffix(".pem.tmp")
                k_tmp = k_file.with_suffix(".pem.tmp")
                c_tmp.write_text(cert_pem, encoding="utf-8")
                k_tmp.write_text(key_pem, encoding="utf-8")
                c_tmp.replace(c_file)
                k_tmp.replace(k_file)
                try:
                    os.chmod(c_file, 0o644)
                    os.chmod(k_file, 0o600)
                except OSError:
                    pass
            except OSError as exc:
                logger.warning("Could not write SSL certificate to %s: %s", ssl_dir, exc)

    def _sync_to_mirror_dirs(self, cert_path: Path, key_path: Path) -> None:
        try:
            cert_pem = cert_path.read_text(encoding="utf-8")
            key_pem = key_path.read_text(encoding="utf-8")
        except OSError:
            return
        for ssl_dir in (self.storage_ssl_dir, self.etc_ssl_dir):
            c_dest = ssl_dir / DEFAULT_CERT_FILENAME
            k_dest = ssl_dir / DEFAULT_KEY_FILENAME
            if not c_dest.is_file() or not k_dest.is_file():
                try:
                    ssl_dir.mkdir(parents=True, exist_ok=True)
                    c_dest.write_text(cert_pem, encoding="utf-8")
                    k_dest.write_text(key_pem, encoding="utf-8")
                except OSError:
                    pass

    @staticmethod
    def validate_pair(cert_path: Path, key_path: Path) -> Tuple[bool, str]:
        """Verify that cert_path and key_path exist and form a valid matching TLS certificate/key pair."""
        if not cert_path.is_file():
            return False, f"Certificate file not found: {cert_path}"
        if not key_path.is_file():
            return False, f"Private key file not found: {key_path}"
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
            return True, "Valid SSL certificate and private key pair."
        except Exception as exc:
            return False, f"SSL certificate/key validation failed: {exc}"

    def install_custom_certificate(
        self, cert_source: Path | str, key_source: Path | str, commit_lbu: bool = True
    ) -> Tuple[bool, str]:
        """Validate and install an issued certificate and private key."""
        cp = Path(cert_source)
        kp = Path(key_source)
        ok, msg = self.validate_pair(cp, kp)
        if not ok:
            return False, msg

        try:
            cert_pem = cp.read_text(encoding="utf-8")
            key_pem = kp.read_text(encoding="utf-8")
        except OSError as exc:
            return False, f"Failed to read certificate files: {exc}"

        self._write_pem_to_persistent_dirs(cert_pem, key_pem)
        self.config_manager.settings.ssl_cert_path = ""
        self.config_manager.settings.ssl_key_path = ""
        self.config_manager.save(commit_lbu=commit_lbu)
        return True, "Issued SSL certificate and private key installed successfully."

    def import_from_drop_dir(self, commit_lbu: bool = True) -> Tuple[bool, str]:
        """Search /storage/ssl for dropped issued certificate/key files and install them if valid."""
        drop_dir = self.samba_drop_ssl_dir
        if not drop_dir.is_dir():
            return False, f"Drop folder {drop_dir} does not exist."

        cert_candidates = (
            "producer-cert.pem",
            "fullchain.pem",
            "cert.pem",
            "server.crt",
            "certificate.crt",
        )
        key_candidates = (
            "producer-key.pem",
            "privkey.pem",
            "key.pem",
            "server.key",
            "private.key",
        )

        found_cert: Optional[Path] = None
        found_key: Optional[Path] = None

        for cname in cert_candidates:
            p = drop_dir / cname
            if p.is_file():
                found_cert = p
                break
        if found_cert is None:
            for p in sorted(drop_dir.glob("*.crt")) + sorted(drop_dir.glob("*-cert.pem")):
                if p.is_file():
                    found_cert = p
                    break

        for kname in key_candidates:
            p = drop_dir / kname
            if p.is_file():
                found_key = p
                break
        if found_key is None:
            for p in sorted(drop_dir.glob("*.key")) + sorted(drop_dir.glob("*-key.pem")):
                if p.is_file():
                    found_key = p
                    break

        if found_cert is None or found_key is None:
            return (
                False,
                f"No certificate/key pair found in {drop_dir}. "
                "Place cert.pem (or fullchain.pem) and key.pem (or privkey.pem) in /storage/ssl.",
            )

        return self.install_custom_certificate(found_cert, found_key, commit_lbu=commit_lbu)

    def create_ssl_context(self) -> ssl.SSLContext:
        """Create a configured TLS server SSLContext using the active certificate and private key."""
        cert_path, key_path = self.ensure_certificates()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
        return ctx

    def get_certificate_info(self) -> CertificateInfo:
        """Inspect the active SSL certificate for UI display."""
        cert_path, key_path = self.resolve_cert_and_key_paths()
        info = CertificateInfo(cert_path=cert_path, key_path=key_path)
        if not cert_path.is_file() or not key_path.is_file():
            info.error = "Certificate or key file does not exist"
            return info

        info.exists = True
        valid, err_msg = self.validate_pair(cert_path, key_path)
        info.valid = valid
        if not valid:
            info.error = err_msg
            return info

        try:
            pem_text = cert_path.read_text(encoding="utf-8")
            der_bytes = ssl.PEM_cert_to_DER_cert(pem_text)
            fp_hex = hashlib.sha256(der_bytes).hexdigest().upper()
            info.fingerprint_sha256 = ":".join(
                fp_hex[i : i + 2] for i in range(0, min(32, len(fp_hex)), 2)
            ) + "..."
        except Exception:
            pass

        try:
            decode_fn = getattr(ssl._ssl, "_test_decode_cert", None)
            if callable(decode_fn):
                decoded = decode_fn(str(cert_path))
                if isinstance(decoded, dict):
                    subj_parts = []
                    for rdn in decoded.get("subject", ()):
                        for k, v in rdn:
                            if k == "commonName":
                                subj_parts.append(f"CN={v}")
                            elif k == "organizationName":
                                subj_parts.append(f"O={v}")
                    info.subject = ", ".join(subj_parts) or "Alpine Recording Station"

                    iss_parts = []
                    for rdn in decoded.get("issuer", ()):
                        for k, v in rdn:
                            if k == "commonName":
                                iss_parts.append(f"CN={v}")
                            elif k == "organizationName":
                                iss_parts.append(f"O={v}")
                    info.issuer = ", ".join(iss_parts) or info.subject
                    info.is_self_signed = decoded.get("subject") == decoded.get("issuer")
                    info.not_before = str(decoded.get("notBefore", ""))
                    info.not_after = str(decoded.get("notAfter", ""))
                    sans = []
                    for stype, sval in decoded.get("subjectAltName", ()):
                        sans.append(f"{stype}:{sval}")
                    info.san_entries = sans
        except Exception as exc:
            logger.debug("Certificate metadata decode fallback: %s", exc)

        if not info.subject:
            info.subject = "CN=Alpine Recording Station"
        return info
