"""Optional Alexa Skills Kit request signature verification (production hardening)."""
from __future__ import annotations

import base64
import json
import os
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Mapping

MAX_CERT_CHAIN_BYTES = 65536


def _cert_url_allowed(url: str) -> bool:
    u = url.strip().lower()
    return u.startswith("https://s3.amazonaws.com/echo.api/")


class _RejectCertRedirects(urllib.request.HTTPRedirectHandler):
    """Do not follow redirects when fetching Alexa signing certificates."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise ValueError("alexa_cert_url_redirect")


def alexa_signature_verification_enabled() -> bool:
    return os.environ.get("ANDREA_ALEXA_VERIFY_SIGNATURES", "0").strip() == "1"


def verify_alexa_http_request(
    body_bytes: bytes,
    headers: Mapping[str, str],
    *,
    expected_application_id: str,
) -> None:
    """
    Validate Alexa POST: signature on raw body (per Amazon), then JSON checks.

    Requires: pip install cryptography
    Env: ANDREA_ALEXA_VERIFY_SIGNATURES=1 and ANDREA_ALEXA_SKILL_ID (application id).
    """
    if not alexa_signature_verification_enabled():
        return
    if not expected_application_id.strip():
        raise RuntimeError(
            "ANDREA_ALEXA_SKILL_ID is required when ANDREA_ALEXA_VERIFY_SIGNATURES=1"
        )

    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.x509 import DNSName
        from cryptography.x509.oid import ExtensionOID
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "Install the 'cryptography' package to enable ANDREA_ALEXA_VERIFY_SIGNATURES"
        ) from exc

    def _validate_leaf_certificate(leaf: x509.Certificate) -> None:
        now = datetime.now(timezone.utc)
        nb = leaf.not_valid_before_utc
        na = leaf.not_valid_after_utc
        if now < nb or now > na:
            raise ValueError("alexa_cert_outside_validity_window")
        try:
            san_ext = leaf.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
        except x509.ExtensionNotFound as exc:
            raise ValueError("alexa_cert_missing_san") from exc
        dns_names = san_ext.value.get_values_for_type(DNSName)
        if "echo-api.amazon.com" not in dns_names:
            raise ValueError("alexa_cert_san_mismatch")

    lower = {str(k).lower(): str(v).strip() for k, v in headers.items() if v is not None}
    cert_url = lower.get("signaturecertchainurl")
    signature_b64 = lower.get("signature")
    if not cert_url or not signature_b64:
        raise ValueError("alexa_missing_signature_headers")

    cert_url_s = str(cert_url).strip()
    if not _cert_url_allowed(cert_url_s):
        raise ValueError("alexa_untrusted_cert_url")

    ctx = ssl.create_default_context()
    opener = urllib.request.build_opener(
        _RejectCertRedirects(),
        urllib.request.HTTPSHandler(context=ctx),
    )
    try:
        with opener.open(cert_url_s, timeout=10) as resp:
            pem_chain = resp.read(MAX_CERT_CHAIN_BYTES + 1)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ValueError("alexa_cert_fetch_failed") from exc
    if len(pem_chain) > MAX_CERT_CHAIN_BYTES:
        raise ValueError("alexa_cert_chain_too_large")

    try:
        try:
            signature = base64.b64decode(signature_b64, validate=True)
        except TypeError:
            signature = base64.b64decode(signature_b64)
    except (TypeError, ValueError) as exc:
        raise ValueError("alexa_invalid_signature_base64") from exc

    certs = _split_pem_certs(pem_chain.decode("utf-8", errors="replace"))
    if not certs:
        raise ValueError("alexa_empty_certificate_chain")

    leaf = x509.load_pem_x509_certificate(certs[0].encode("utf-8"))
    _validate_leaf_certificate(leaf)
    public_key = leaf.public_key()

    try:
        public_key.verify(
            signature,
            body_bytes,
            padding.PKCS1v15(),
            hashes.SHA1(),
        )
    except Exception as exc:  # noqa: BLE001
        raise ValueError("alexa_signature_verification_failed") from exc

    try:
        body = json.loads(body_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("alexa_invalid_json_body") from exc
    if not isinstance(body, dict):
        raise ValueError("alexa_invalid_json_body")

    session = body.get("session") or {}
    context = body.get("context") or {}
    app = (session.get("application") or {}).get("applicationId") or (
        ((context.get("System") or {}).get("application") or {}).get("applicationId") or ""
    )
    if app != expected_application_id.strip():
        raise ValueError("alexa_application_id_mismatch")

    request_block = body.get("request") or {}
    ts = request_block.get("timestamp")
    if not ts:
        raise ValueError("alexa_missing_request_timestamp")
    try:
        req_time = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        skew = abs(time.time() - req_time.timestamp())
        if skew > 150:
            raise ValueError("alexa_timestamp_skew")
    except (TypeError, ValueError) as exc:
        if str(exc) == "alexa_timestamp_skew":
            raise
        raise ValueError("alexa_invalid_request_timestamp") from exc


def _split_pem_certs(pem_text: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    for line in pem_text.splitlines():
        if "BEGIN CERTIFICATE" in line:
            buf = [line]
        elif buf:
            buf.append(line)
            if "END CERTIFICATE" in line:
                parts.append("\n".join(buf) + "\n")
                buf = []
    return parts
