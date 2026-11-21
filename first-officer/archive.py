#!/usr/bin/env python3
"""Pack the reviewable First Officer lane into one checksummed archive.

The archive is the checkout Jeff can unpack and test. It is not the gateway
release. Packing does not read the private prior bundle, call systemctl, or
activate anything. A file the secret scan rejects is left out, and no archive
is written.
"""

import argparse
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import stat
import sys
import tarfile


def _scanner():
    path = Path(__file__).resolve().parent / "privacy" / "scan.py"
    spec = importlib.util.spec_from_file_location("fo_privacy_scan", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_scan = _scanner()
findings_in_text = _scan.findings_in_text
refuse_live = _scan.refuse_live


DOCS = ("CAPTAIN.md", "INSTALL.md", "LIVE-TEST.md", "TEST-NOW.md", "JEFF-ACTIONS.md")
TREES = ("first-officer", "tests/first_officer")
SKIP = {"__pycache__", ".pytest_cache"}
MODE = stat.S_IRUSR | stat.S_IWUSR


def _repo(path):
    refuse_live(path)
    root = Path(path).resolve()
    if not root.is_dir() or root.is_symlink():
        raise ValueError("The archive source must be a directory")
    return root


def _destination(path, repo):
    refuse_live(path)
    destination = Path(path).resolve()
    if destination == repo or repo in destination.parents or destination in repo.parents:
        raise ValueError("The archive must be written outside the checkout")
    if destination.is_symlink():
        raise ValueError("The archive directory must be a private directory")
    destination.mkdir(mode=0o700, exist_ok=True)
    return destination


def iter_files(repo):
    paths = []
    for name in DOCS:
        path = repo / name
        if not path.is_file() or path.is_symlink():
            raise ValueError("The review archive is missing a required document")
        paths.append(path)
    for tree in TREES:
        root = repo / tree
        if not root.is_dir() or root.is_symlink():
            raise ValueError("The review archive is missing a required tree")
        for path in root.rglob("*"):
            if any(part in SKIP for part in path.relative_to(repo).parts):
                continue
            if path.is_symlink():
                raise ValueError("The review archive cannot follow a symlink")
            if path.is_file():
                paths.append(path)
    if not any(path.name == "scan.py" for path in paths):
        raise ValueError("The review archive is missing the secret scan")
    return tuple(sorted(paths, key=lambda item: item.relative_to(repo).as_posix()))


def _manifest(repo, paths):
    lines = []
    for path in paths:
        payload = path.read_bytes()
        if b"\0" in payload:
            raise ValueError("The review archive cannot include a binary file")
        text = payload.decode("utf-8", "replace")
        relative = path.relative_to(repo).as_posix()
        if findings_in_text(text, path=relative):
            raise ValueError("The review archive contains a secret or personal data")
        digest = hashlib.sha256(payload).hexdigest()
        lines.append(f"{digest}  {relative}")
    manifest = ("\n".join(lines) + "\n").encode("utf-8")
    return manifest, hashlib.sha256(manifest).hexdigest()


def pack(repo, destination):
    repo = _repo(repo)
    destination = _destination(destination, repo)
    paths = iter_files(repo)
    manifest, digest = _manifest(repo, paths)
    folder = f"first-officer-review-{digest}"
    archive = destination / f"{folder}.tar"
    if archive.exists() or archive.is_symlink():
        raise ValueError("The review archive already exists")
    temporary = archive.with_suffix(".tar.partial")
    if temporary.exists() or temporary.is_symlink():
        raise ValueError("The review archive already exists")
    try:
        with tarfile.open(temporary, "w", format=tarfile.PAX_FORMAT) as bundle:
            _add(bundle, folder, "SHA256SUMS", manifest)
            for path in paths:
                _add(bundle, folder, path.relative_to(repo).as_posix(), path.read_bytes())
        os.chmod(temporary, MODE)
        os.replace(temporary, archive)
        os.chmod(archive, MODE)
    finally:
        if temporary.exists():
            temporary.unlink()
    return {"digest": digest, "archive": archive, "files": len(paths)}


def _add(bundle, folder, name, payload):
    if name.startswith("/") or ".." in Path(name).parts:
        raise ValueError("The review archive refused a path")
    info = tarfile.TarInfo(f"{folder}/{name}")
    info.size = len(payload)
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.mode = 0o600
    info.type = tarfile.REGTYPE
    bundle.addfile(info, io.BytesIO(payload))


def verify(archive):
    refuse_live(archive)
    path = Path(archive).resolve()
    if not path.is_file() or path.is_symlink():
        raise ValueError("The review archive must be a file")
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("The review archive must be a private owner file")
    members = {}
    folder = ""
    with tarfile.open(path, "r:") as bundle:
        for member in bundle.getmembers():
            name = member.name
            if member.issym() or member.islnk() or not member.isfile():
                raise ValueError("The review archive refused a path")
            parts = Path(name).parts
            if name.startswith("/") or ".." in parts or len(parts) < 2:
                raise ValueError("The review archive refused a path")
            if not folder:
                folder = parts[0]
            if parts[0] != folder or not folder.startswith("first-officer-review-"):
                raise ValueError("The review archive refused a path")
            relative = "/".join(parts[1:])
            if relative in members:
                raise ValueError("The review archive refused a path")
            extracted = bundle.extractfile(member)
            if extracted is None:
                raise ValueError("The review archive could not be read")
            members[relative] = extracted.read()
    manifest = members.get("SHA256SUMS")
    if manifest is None:
        raise ValueError("The review archive is missing its checksums")
    digest = hashlib.sha256(manifest).hexdigest()
    if folder != f"first-officer-review-{digest}":
        raise ValueError("The review archive digest does not match its checksums")
    expected = {}
    for line in manifest.decode("utf-8").splitlines():
        checksum, separator, name = line.partition("  ")
        if separator != "  " or not re_sha(checksum) or not name or name == "SHA256SUMS":
            raise ValueError("The review archive checksums are unreadable")
        if name.startswith("/") or ".." in Path(name).parts:
            raise ValueError("The review archive refused a path")
        expected[name] = checksum
    present = {name for name in members if name != "SHA256SUMS"}
    if present != set(expected):
        raise ValueError("The review archive files do not match its checksums")
    for name, checksum in expected.items():
        if hashlib.sha256(members[name]).hexdigest() != checksum:
            raise ValueError("The review archive files do not match its checksums")
    return {"digest": digest, "files": len(expected)}


def re_sha(value):
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def main(argv=None):
    repo = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    packed = commands.add_parser("pack")
    packed.add_argument("destination", type=Path)
    checked = commands.add_parser("verify")
    checked.add_argument("archive", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "pack":
            result = pack(repo, args.destination)
            print(f"Review archive {result['digest']}")
            print(result["archive"])
            print("The gateway was not touched.")
            return 0
        result = verify(args.archive)
        print(f"Review archive {result['digest']} verified. The gateway was not touched.")
        return 0
    except (OSError, ValueError, tarfile.TarError) as error:
        message = str(error) if type(error) is ValueError else "The review archive failed"
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
