#!/usr/bin/env python3
"""Combine native OCI archives into one image index without publishing them."""

from __future__ import annotations

import hashlib
import io
import json
import sys
import tarfile
from collections.abc import Sequence
from contextlib import ExitStack
from pathlib import Path

INDEX = "application/vnd.oci.image.index.v1+json"
MANIFEST = "application/vnd.oci.image.manifest.v1+json"


def merge(output: str | Path, inputs: Sequence[str | Path]) -> None:
    if not inputs:
        raise ValueError("No native image archives supplied")
    with ExitStack() as stack:
        archives = [stack.enter_context(tarfile.open(path)) for path in inputs]
        manifests = []
        platforms = set()
        for archive in archives:

            def read_json(
                name: str, current_archive: tarfile.TarFile = archive
            ) -> dict:
                return json.load(current_archive.extractfile(name))

            def visit(
                descriptor: dict, current_archive: tarfile.TarFile = archive
            ) -> None:
                digest = descriptor["digest"]
                algorithm, value = digest.split(":", 1)
                if algorithm != "sha256":
                    raise ValueError(f"Unsupported digest: {digest}")
                blob = f"blobs/sha256/{value}"
                data = read_json(blob)
                if "manifests" in data:
                    for child in data["manifests"]:
                        visit(child)
                    return
                config_digest = data["config"]["digest"].split(":", 1)[1]
                config = read_json(f"blobs/sha256/{config_digest}")
                platform = {
                    key: config[key]
                    for key in ("os", "architecture", "variant")
                    if key in config
                }
                key = (
                    platform.get("os"),
                    platform.get("architecture"),
                    platform.get("variant"),
                )
                if key[0] != "linux" or key[1] not in ("amd64", "arm64"):
                    raise ValueError(f"Unsupported native image platform: {platform}")
                if key in platforms:
                    raise ValueError(f"Duplicate native image platform: {platform}")
                platforms.add(key)
                manifests.append(
                    {
                        "mediaType": data.get("mediaType", MANIFEST),
                        "digest": digest,
                        "size": current_archive.getmember(blob).size,
                        "platform": platform,
                    }
                )

            for descriptor in read_json("index.json")["manifests"]:
                visit(descriptor)
        if not manifests:
            raise ValueError("No native image manifests found")
        index = json.dumps(
            {"schemaVersion": 2, "mediaType": INDEX, "manifests": manifests},
            separators=(",", ":"),
        ).encode()
        digest = hashlib.sha256(index).hexdigest()
        root = {
            "schemaVersion": 2,
            "mediaType": INDEX,
            "manifests": [
                {
                    "mediaType": INDEX,
                    "digest": f"sha256:{digest}",
                    "size": len(index),
                    "annotations": {"org.opencontainers.image.ref.name": "driver"},
                }
            ],
        }
        with tarfile.open(output, "w") as destination:
            seen = set()
            for archive in archives:
                for member in archive:
                    if (
                        member.name.startswith("blobs/sha256/")
                        and member.isfile()
                        and member.name not in seen
                    ):
                        destination.addfile(member, archive.extractfile(member))
                        seen.add(member.name)
            for name, data in [
                ("oci-layout", b'{"imageLayoutVersion":"1.0.0"}'),
                ("index.json", json.dumps(root).encode()),
                (f"blobs/sha256/{digest}", index),
            ]:
                if name in seen:
                    continue
                info = tarfile.TarInfo(name)
                info.size = len(data)
                destination.addfile(info, io.BytesIO(data))


if __name__ == "__main__":
    merge(sys.argv[1], sys.argv[2:])
