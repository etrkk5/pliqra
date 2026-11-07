"""Verify GHCR access without local Docker credentials or a GitHub token."""
import argparse
import hashlib
import json
import re
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def verify(repository: str, version: str, digest: str) -> dict:
    if not re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", repository):
        raise ValueError("Expected a lowercase owner/image repository")
    if not re.fullmatch(r"[a-zA-Z0-9_.-]+", version) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("Invalid image version or SHA-256 digest")
    query = urlencode({"service": "ghcr.io", "scope": f"repository:{repository}:pull"})
    with urlopen(f"https://ghcr.io/token?{query}", timeout=30) as response:
        token = json.load(response)["token"]
    request = Request(f"https://ghcr.io/v2/{repository}/manifests/{version}", headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json",
    })
    with urlopen(request, timeout=30) as response:
        raw = response.read()
        assert response.headers["Docker-Content-Digest"] == digest, "Registry digest mismatch"
    assert "sha256:" + hashlib.sha256(raw).hexdigest() == digest, "Manifest bytes do not match digest"
    manifests = json.loads(raw)["manifests"]
    platforms = {f"{m['platform']['os']}/{m['platform']['architecture']}" for m in manifests}
    assert {"linux/amd64", "linux/arm64"} <= platforms, platforms
    attestations = [m for m in manifests if m.get("annotations", {}).get("vnd.docker.reference.type") == "attestation-manifest"]
    assert len(attestations) >= 2, "Expected per-platform build attestations"
    result = {"image": f"ghcr.io/{repository}:{version}", "digest": digest,
              "anonymous_pull": True, "platforms": ["linux/amd64", "linux/arm64"], "attestation_manifests": len(attestations)}
    print(json.dumps(result))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository")
    parser.add_argument("version")
    parser.add_argument("digest")
    args = parser.parse_args()
    verify(args.repository, args.version, args.digest)
