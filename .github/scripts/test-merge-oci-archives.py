#!/usr/bin/env python3
"""Exercise offline OCI assembly with the real pinned regctl, without Docker."""

import hashlib
import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

REGCTL = os.environ.get("REGCTL", "regctl")
SCRIPT = Path(__file__).with_name("merge-oci-archives.sh")
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_INDEX = "application/vnd.oci.image.index.v1+json"


def write_oci_archive(path: Path, arch: str, nested: bool = False) -> tuple[str, bytes]:
    """Create content-addressed OCI data in both single-image and Buildx shapes."""
    blobs = {}

    def add_blob(data: bytes, media_type: str) -> dict[str, object]:
        digest = hashlib.sha256(data).hexdigest()
        blobs[f"blobs/sha256/{digest}"] = data
        return {
            "mediaType": media_type,
            "digest": f"sha256:{digest}",
            "size": len(data),
        }

    layer_buffer = io.BytesIO()
    with tarfile.open(fileobj=layer_buffer, mode="w") as layer:
        data = f"native {arch}\n".encode()
        entry = tarfile.TarInfo("architecture.txt")
        entry.size = len(data)
        layer.addfile(entry, io.BytesIO(data))
    layer_data = layer_buffer.getvalue()
    config = add_blob(
        json.dumps(
            {
                "architecture": arch,
                "os": "linux",
                "config": {},
                "rootfs": {
                    "type": "layers",
                    "diff_ids": [f"sha256:{hashlib.sha256(layer_data).hexdigest()}"],
                },
            }
        ).encode(),
        "application/vnd.oci.image.config.v1+json",
    )
    manifest = add_blob(
        json.dumps(
            {
                "schemaVersion": 2,
                "mediaType": OCI_MANIFEST,
                "config": config,
                "layers": [
                    add_blob(layer_data, "application/vnd.oci.image.layer.v1.tar")
                ],
            }
        ).encode(),
        OCI_MANIFEST,
    )
    image_digest = manifest["digest"]
    if nested:
        manifest["platform"] = {"os": "linux", "architecture": arch}
        manifest = add_blob(
            json.dumps(
                {
                    "schemaVersion": 2,
                    "mediaType": OCI_INDEX,
                    "manifests": [manifest],
                }
            ).encode(),
            OCI_INDEX,
        )
    blobs["index.json"] = json.dumps(
        {"schemaVersion": 2, "manifests": [manifest]}
    ).encode()
    blobs["oci-layout"] = b'{"imageLayoutVersion":"1.0.0"}'
    with tarfile.open(path, "w") as archive:
        for name, data in blobs.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
    return image_digest, layer_data


class MergeArchivesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.output = self.root / "combined.tar"
        # These tests use only local OCI data and need no Docker credentials.
        docker_config = self.root / "docker-config"
        docker_config.mkdir()
        self.env = {**os.environ, "REGCTL": REGCTL, "DOCKER_CONFIG": str(docker_config)}

    def merge(self, *sources: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(SCRIPT), str(self.output), *sources],
            env=self.env,
            capture_output=True,
            check=False,
            text=True,
        )

    def assert_combined_archive(self, expected: dict[str, tuple[str, bytes]]) -> None:
        ref = f"ocidir://{self.root}/imported:combined"
        subprocess.run(
            [REGCTL, "image", "import", ref, str(self.output)], env=self.env, check=True
        )
        manifest = json.loads(
            subprocess.check_output(
                [
                    REGCTL,
                    "manifest",
                    "get",
                    ref,
                    "--format",
                    "raw-body",
                ],
                env=self.env,
            )
        )
        self.assertEqual(
            {
                descriptor["platform"]["architecture"]: descriptor["digest"]
                for descriptor in manifest["manifests"]
            },
            {arch: digest for arch, (digest, _) in expected.items()},
        )
        with tarfile.open(self.output) as archive:
            for _, layer in expected.values():
                name = f"blobs/sha256/{hashlib.sha256(layer).hexdigest()}"
                self.assertEqual(archive.extractfile(name).read(), layer)

    def test_merge_preserves_native_platforms_digests_and_layers(self) -> None:
        amd64 = write_oci_archive(self.root / "amd64.tar", "amd64")
        arm64 = write_oci_archive(self.root / "arm64.tar", "arm64", nested=True)
        result = self.merge(
            f"amd64={self.root}/amd64.tar", f"arm64={self.root}/arm64.tar"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_combined_archive({"amd64": amd64, "arm64": arm64})

    def test_amd64_only_archive(self) -> None:
        amd64 = write_oci_archive(self.root / "amd64.tar", "amd64", nested=True)
        result = self.merge(f"amd64={self.root}/amd64.tar")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_combined_archive({"amd64": amd64})

    def test_missing_architecture_fails_without_output(self) -> None:
        write_oci_archive(self.root / "amd64.tar", "amd64")
        result = self.merge(
            f"amd64={self.root}/amd64.tar", f"arm64={self.root}/missing.tar"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())

    def test_wrong_platform_fails_without_output(self) -> None:
        write_oci_archive(self.root / "arm64.tar", "arm64", nested=True)
        result = self.merge(f"amd64={self.root}/arm64.tar")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())

    def test_duplicate_platform_fails_without_output(self) -> None:
        write_oci_archive(self.root / "amd64.tar", "amd64")
        result = self.merge(
            f"amd64={self.root}/amd64.tar", f"amd64={self.root}/amd64.tar"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("do not match", result.stderr)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
