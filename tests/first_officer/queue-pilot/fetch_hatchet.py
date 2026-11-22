"""Fetch and verify the pinned amd64 Hatchet Lite image without a container daemon."""
import hashlib
import json
from pathlib import Path
import urllib.request

REGISTRY = "https://ghcr.io"
REPOSITORY = "hatchet-dev/hatchet/hatchet-lite"
PIN = "sha256:29f215cc012d659ae6d90205398c6224c896ffcc00d350ee852ce377cd15abb1"
TAG = "v0.120.19"
DOCKER_MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"
OCI = "application/vnd.oci.image."

def fetch(path, registry_token):
    request = urllib.request.Request(
        f"{REGISTRY}/v2/{REPOSITORY}/{path}",
        headers={"Authorization": f"Bearer {registry_token}", "Accept": ", ".join(
            (OCI + "index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json", DOCKER_MANIFEST)
        )},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def store(blobs, value, digest):
    algorithm, expected = digest.split(":")
    if algorithm != "sha256" or hashlib.sha256(value).hexdigest() != expected:
        raise ValueError("Hatchet image digest mismatch")
    (blobs / expected).write_bytes(value)


def main():
    token_url = f"{REGISTRY}/token?scope=repository:{REPOSITORY}:pull"
    with urllib.request.urlopen(token_url, timeout=30) as response:
        registry_token = json.load(response)["token"]
    index = json.loads(fetch("manifests/" + TAG, registry_token))
    descriptor = next(item for item in index["manifests"] if item["platform"] == {
        "os": "linux", "architecture": "amd64"
    })
    if descriptor["digest"] != PIN:
        raise ValueError("The Hatchet image tag no longer matches the reviewed digest")
    layout = Path("hatchet-oci")
    blobs = layout / "blobs" / "sha256"
    blobs.mkdir(parents=True)
    manifest_bytes = fetch("manifests/" + PIN, registry_token)
    store(blobs, manifest_bytes, PIN)
    manifest = json.loads(manifest_bytes)
    for item in [manifest["config"], *manifest["layers"]]:
        ref = item["digest"]
        store(blobs, fetch("blobs/" + ref, registry_token), ref)

    # umoci requires OCI media types. Retain the verified image bytes alongside the translated metadata.
    manifest["mediaType"] = descriptor["mediaType"] = OCI + "manifest.v1+json"
    manifest["config"]["mediaType"] = OCI + "config.v1+json"
    for layer in manifest["layers"]:
        layer["mediaType"] = OCI + "layer.v1.tar+gzip"
    translated = json.dumps(manifest).encode()
    digest = "sha256:" + hashlib.sha256(translated).hexdigest()
    store(blobs, translated, digest)
    descriptor.update(digest=digest, size=len(translated), annotations={"org.opencontainers.image.ref.name": TAG})
    (layout / "oci-layout").write_text(json.dumps({"imageLayoutVersion": "1.0.0"}))
    (layout / "index.json").write_text(json.dumps({"schemaVersion": 2, "manifests": [descriptor]}))
    print(f"Verified Hatchet Lite {TAG} amd64 manifest {PIN}")


if __name__ == "__main__":
    main()
