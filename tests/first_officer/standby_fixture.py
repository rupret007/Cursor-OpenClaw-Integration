"""Ephemeral certificate fixture for the local standby rehearsals."""

import subprocess


def _openssl(directory, *args):
    subprocess.run(["openssl", *args], cwd=directory, check=True, capture_output=True, timeout=8)


def certificates(directory, names=("broker", "nuc", "mac")):
    directory.mkdir(mode=0o700, exist_ok=True)
    key_type = ("-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes")
    _openssl(directory, "req", "-new", "-x509", *key_type, "-subj", "/CN=standby-fixture-ca",
             "-days", "1", "-addext", "keyUsage=critical,keyCertSign,cRLSign", "-keyout", "ca.key", "-out", "ca.pem")
    for name in names:
        ext = directory / (name + ".ext")
        purpose = ("subjectAltName=IP:127.0.0.1\nextendedKeyUsage=serverAuth\n" if name == "broker"
                   else "extendedKeyUsage=clientAuth\n")
        ext.write_text("basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n"
                       "authorityKeyIdentifier=keyid\n" + purpose)
        _openssl(directory, "req", "-new", *key_type, "-subj", f"/CN={name}", "-keyout", f"{name}.key",
                 "-out", f"{name}.csr")
        _openssl(directory, "x509", "-req", "-in", f"{name}.csr", "-CA", "ca.pem", "-CAkey", "ca.key",
                 "-CAcreateserial", "-days", "1", "-extfile", ext.name, "-out", f"{name}.pem")
