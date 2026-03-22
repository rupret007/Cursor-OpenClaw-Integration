"""Optional Alexa Skills Kit request signature verification (production hardening)."""
from __future__ import annotations

import base64
import json
import os
import ssl
import time
import urllib.error
import urllib.request
from typing import Mapping

def _cert_url_allowed(url: str) -> bool:
    u = url.strip().lower()
    return u.startswith("https://s3.amazonaws.com/echo.api/")


def alexa_signature_verification_enabled() -> bool:
    return os.environ.get("ANDREA_ALEXA_VERIFY_SIGNATURES", "0").strip() == "1"


def verify_alexa_http_request(
    body_bytes: bytes,
    headers: Mapping[str, str],
    *,
    expected_application_id: str,
) -> None:
    """
    Validate Alexa POST: timestamp freshness, applicationId, and request signature.

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
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "Install the 'cryptography' package to enable ANDREA_ALEXA_VERIFY_SIGNATURES"
        ) from exc

    try:
        body = json.loads(body_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid JSON body") from exc

    session = body.get("session") or {}
    app = (session.get("application") or {}).get("applicationId") or ""
    if app != expected_application_id.strip():
        raise ValueError("alexa applicationId mismatch")

    request_block = body.get("request") or {}
    ts = request_block.get("timestamp")
    if not ts:
        raise ValueError("missing request.timestamp")
    try:
        from datetime import datetime

        req_time = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        skew = abs(time.time() - req_time.timestamp())
        if skew > 150:
            raise ValueError("request timestamp outside allowed skew")
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid request.timestamp") from exc

    lower = {str(k).lower(): str(v).strip() for k, v in headers.items() if v is not None}
    cert_url = lower.get("signaturecertchainurl")
    signature_b64 = lower.get("signature")
    if not cert_url or not signature_b64:
        raise ValueError("missing Signature or SignatureCertChainUrl headers")

    cert_url_s = str(cert_url).strip()
    if not _cert_url_allowed(cert_url_s):
        raise ValueError("untrusted SignatureCertChainUrl")

    try:
        with urllib.request.urlopen(cert_url_s, timeout=10, context=ssl.create_default_context()) as resp:
            pem_chain = resp.read()
    except (urllib.error.URLError, OSError) as exc:
        raise ValueError(f"failed to download cert chain: {exc}") from exc

    try:
        try:
            signature = base64.b64decode(signature_b64, validate=True)
        except TypeError:
            signature = base64.b64decode(signature_b64)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid Signature base64") from exc

    certs = _split_pem_certs(pem_chain.decode("utf-8", errors="replace"))
    if not certs:
        raise ValueError("empty certificate chain")

    leaf_pem = certs[0].encode("utf-8")
    leaf = x509.load_pem_x509_certificate(leaf_pem)
    public_key = leaf.public_key()

    try:
        public_key.verify(
            signature,
            body_bytes,
            padding.PKCS1v15(),
            hashes.SHA1(),
        )
    except Exception as exc:  # noqa: BLE001
        raise ValueError("signature verification failed") from exc


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
